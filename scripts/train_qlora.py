"""QLoRA fine-tune Qwen3-1.7B into the Vigor fitness coach.

Tuned for an RTX 4070 Ti (Ada, sm_89, 12GB). Ada has native bf16, which removes
every workaround the T4 needed:

  - bf16 has fp32's dynamic range, so training needs no GradScaler. The
    "_amp_foreach_non_finite_check_and_unscale_cuda not implemented for
    BFloat16" error cannot occur.
  - No manual fp32 casting of LoRA params, so the standard PEFT path works and
    peft_config goes to SFTTrainer as intended.
  - Ada's bf16 tensor cores are roughly 3x a T4's throughput here.

Checkpoints are written once per epoch and load_best_model_at_end is off - test
each checkpoint and push the one that behaves. A short generation runs in
process right after training, before saving, so a bad save can be told apart
from bad training.

    python scripts/train_qlora.py

Env overrides: TRAIN_PATH, VAL_PATH, ADAPTER_OUTPUT_DIR, HF_REPO_ID,
               PUSH_TO_HUB, HF_TOKEN
"""

import os
from pathlib import Path

import torch
from datasets import load_dataset
from dotenv import load_dotenv
from peft import LoraConfig
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

import sys
print("python  :", sys.executable)
print("torch   :", torch.__version__)
print("cuda ver:", torch.version.cuda)
print("available:", torch.cuda.is_available())

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_MODEL = "Qwen/Qwen3-1.7B"

_PROJECT = Path(__file__).resolve().parent.parent
TRAIN_PATH = os.getenv("TRAIN_PATH", str(_PROJECT / "data" / "processed" / "train.jsonl"))
VAL_PATH = os.getenv("VAL_PATH", str(_PROJECT / "data" / "processed" / "validation.jsonl"))
ADAPTER_OUTPUT_DIR = os.getenv("ADAPTER_OUTPUT_DIR", str(_PROJECT / "outputs" / "adapter"))

HF_REPO_ID = os.getenv("HF_REPO_ID", "Krsmanovicc/vigor-coach-qlora")
PUSH_TO_HUB = os.getenv("PUSH_TO_HUB", "false").lower() == "true"

# LoRA. Rank 16 is affordable here - the 4070 Ti has the headroom the T4 did
# not, and more capacity suits a dataset with this much format to learn.
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]

# Last run's eval bottomed out around epoch 0.9 (loss 0.80) then degraded to
# 1.01 by epoch 2, with entropy climbing 1.57 -> 2.19. Two epochs is the
# ceiling, and epoch 1 may well be the better checkpoint.
EPOCHS = 2
BATCH_SIZE = 2                  # 12GB at 4096 context; drop to 1 if OOM
GRAD_ACCUM = 8                  # effective batch 16
LEARNING_RATE = 2e-4
MAX_LENGTH = 4096               # token report: median 1749, p90 2390, max 4973
EVAL_EVERY = 10
WARMUP_STEPS = 5
SEED = 42

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN") or None

if not torch.cuda.is_available():
    raise SystemExit("No CUDA GPU detected.")

major, minor = torch.cuda.get_device_capability()
USE_BF16 = torch.cuda.is_bf16_supported() and major >= 8
DTYPE = torch.bfloat16 if USE_BF16 else torch.float16

print(f"GPU: {torch.cuda.get_device_name(0)} (sm_{major}{minor}) | "
      f"{'native bf16' if USE_BF16 else 'fp16'}")

if not USE_BF16:
    print("  WARNING: this script assumes native bf16. On a pre-Ampere card the "
          "fp16 path needs the GradScaler workarounds from the Colab version.")

# ---------------------------------------------------------------------------
# Data - loaded first so a bad path fails before the model download
# ---------------------------------------------------------------------------
train_ds = load_dataset("json", data_files=TRAIN_PATH, split="train")
val_ds = load_dataset("json", data_files=VAL_PATH, split="train")

if "text" not in train_ds.column_names:
    raise SystemExit("No 'text' column. Run scripts/build_training_data.py first.")

# Only the pre-rendered string. If "messages" survives, TRL re-applies the chat
# template without enable_thinking=False and the training prompts stop matching
# what the app produces at inference.
train_ds = train_ds.select_columns(["text"])
val_ds = val_ds.select_columns(["text"])
print(f"Train: {len(train_ds)} | Validation: {len(val_ds)}")

sample = train_ds[0]["text"]
print(f"\nexample 0 starts: {sample[:90]!r}")
print(f"empty think block present: {'<think>' in sample}\n")

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=DTYPE,
    bnb_4bit_quant_type="nf4",
)

tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=HF_TOKEN)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL,
    quantization_config=quant_config,
    dtype=DTYPE,
    device_map={"": 0},             # single GPU, no offload
    token=HF_TOKEN,
)
model.config.use_cache = False

peft_config = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    target_modules=TARGET_MODULES,
    bias="none",
    task_type="CAUSAL_LM",
)

# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------
sft_config = SFTConfig(
    output_dir=ADAPTER_OUTPUT_DIR,
    dataset_text_field="text",
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=BATCH_SIZE,
    per_device_eval_batch_size=BATCH_SIZE,
    gradient_accumulation_steps=GRAD_ACCUM,
    learning_rate=LEARNING_RATE,
    max_length=MAX_LENGTH,
    warmup_steps=WARMUP_STEPS,
    lr_scheduler_type="cosine",
    optim="paged_adamw_8bit",
    bf16=USE_BF16,
    fp16=not USE_BF16,
    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": False},
    logging_steps=5,
    eval_strategy="steps",
    eval_steps=EVAL_EVERY,
    save_strategy="epoch",              # one checkpoint per epoch
    save_total_limit=3,
    load_best_model_at_end=False,       # suspected cause of the corrupted adapter
    seed=SEED,
    report_to="none",
    push_to_hub=PUSH_TO_HUB,
    hub_model_id=HF_REPO_ID if PUSH_TO_HUB else None,
    hub_token=HF_TOKEN if PUSH_TO_HUB else None,
)

# Standard path: SFTTrainer wraps the model with peft_config itself. No manual
# get_peft_model, no fp32 casting - bf16 needs neither.
trainer = SFTTrainer(
    model=model,
    args=sft_config,
    train_dataset=train_ds,
    eval_dataset=val_ds,
    processing_class=tokenizer,
    peft_config=peft_config,
)

steps = max(1, len(train_ds) // (BATCH_SIZE * GRAD_ACCUM)) * EPOCHS
print(f"~{steps} total steps, evaluating every {EVAL_EVERY}, saving each epoch\n")

trainer.train()

# ---------------------------------------------------------------------------
# Sanity check - generate from the model still in memory, before saving
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("SANITY CHECK - generating from the in-memory model")
print("=" * 70)

trainer.model.config.use_cache = True
trainer.model.eval()

probe = [
    {"role": "system", "content": "You are a knowledgeable personal fitness coach."},
    {"role": "user", "content": "how much protein should I eat"},
]
try:
    text = tokenizer.apply_chat_template(
        probe, tokenize=False, add_generation_prompt=True, enable_thinking=False)
except TypeError:
    text = tokenizer.apply_chat_template(probe, tokenize=False, add_generation_prompt=True)

inputs = tokenizer([text], return_tensors="pt").to(trainer.model.device)
with torch.no_grad():
    out = trainer.model.generate(**inputs, max_new_tokens=150, do_sample=False)
print(tokenizer.decode(out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip())

print("=" * 70)
print("Coherent here means training is fine and any breakage is in save/load.")
print("Word salad here means the problem is training itself.\n")

trainer.model.config.use_cache = False

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
trainer.save_model(ADAPTER_OUTPUT_DIR)
tokenizer.save_pretrained(ADAPTER_OUTPUT_DIR)
print(f"Adapter saved to {ADAPTER_OUTPUT_DIR}")
print("Per-epoch checkpoints are in checkpoint-* subfolders. Test each with "
      "test_model.py before pushing.")

if PUSH_TO_HUB:
    trainer.push_to_hub()
    print(f"Pushed to https://huggingface.co/{HF_REPO_ID}")