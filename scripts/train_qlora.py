"""QLoRA fine-tune Qwen3-1.7B into the Vigor fitness coach.

Trains on the pre-rendered strings from build_training_data.py. Only the "text"
column is passed to the trainer - the JSONL also has "messages" and "tools", and
TRL would re-apply the chat template to those without enable_thinking=False,
making the training prompts differ from what the app produces at inference.

    python train_qlora.py

Env overrides: TRAIN_PATH, VAL_PATH, ADAPTER_OUTPUT_DIR, HF_REPO_ID,
               PUSH_TO_HUB, HF_TOKEN
"""

import os
from pathlib import Path

import torch
from datasets import load_dataset
from dotenv import load_dotenv
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

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

LORA_R = 8
LORA_ALPHA = 16
LORA_DROPOUT = 0.05
TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]

EPOCHS = 2
BATCH_SIZE = 1
GRAD_ACCUM = 16                 # effective batch 16
LEARNING_RATE = 2e-4
MAX_LENGTH = 2048
EVAL_EVERY = 10
WARMUP_STEPS = 5
SEED = 42

# Compute dtype for the quantized base model. The T4 has fp16 tensor cores and
# only emulated bf16, so fp16 is the right choice on Turing.
COMPUTE_DTYPE = torch.float16

# ---------------------------------------------------------------------------
# Precision note
#
# Mixed precision (fp16=True) wraps training in a GradScaler, and a GradScaler
# cannot unscale bf16 gradients - the source of
#   "_amp_foreach_non_finite_check_and_unscale_cuda not implemented for BFloat16"
# Rather than chase which tensor is still bf16, autocast is disabled entirely:
# both fp16 and bf16 are False below.
#
# This costs very little. The heavy matmuls still run through bitsandbytes at
# bnb_4bit_compute_dtype (fp16), and the only weights trained in fp32 are the
# ~8.7M LoRA params, which are a rounding error next to the 1.7B base.
# ---------------------------------------------------------------------------
load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN") or None

if not torch.cuda.is_available():
    raise SystemExit("No CUDA GPU detected. Run this on Colab or your home machine.")

print(f"GPU: {torch.cuda.get_device_name(0)} | compute dtype: {COMPUTE_DTYPE} | "
      f"autocast: off")

# ---------------------------------------------------------------------------
# Data - loaded first so a bad path fails before the model download
# ---------------------------------------------------------------------------
train_ds = load_dataset("json", data_files=TRAIN_PATH, split="train")
val_ds = load_dataset("json", data_files=VAL_PATH, split="train")

if "text" not in train_ds.column_names:
    raise SystemExit("No 'text' column. Run scripts/build_training_data.py first.")

train_ds = train_ds.select_columns(["text"])
val_ds = val_ds.select_columns(["text"])
print(f"Train: {len(train_ds)} | Validation: {len(val_ds)}")

sample = train_ds[0]["text"]
print("\nFirst 160 chars of example 0:")
print(sample[:160].replace("\n", "\\n"))
print(f"empty think block present: {'<think>' in sample}\n")

# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=COMPUTE_DTYPE,
    bnb_4bit_quant_type="nf4",
)

tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=HF_TOKEN)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL,
    quantization_config=quant_config,
    dtype=COMPUTE_DTYPE,
    device_map="auto",
    token=HF_TOKEN,
)
model.config.use_cache = False
model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

peft_config = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    target_modules=TARGET_MODULES,
    bias="none",
    task_type="CAUSAL_LM",
)

# Attach the adapter here rather than letting SFTTrainer do it, so the trainable
# params can be forced to fp32 before training starts.
model = get_peft_model(model, peft_config)
for _name, _param in model.named_parameters():
    if _param.requires_grad:
        _param.data = _param.data.float()

n_bad = sum(1 for _, p in model.named_parameters()
            if p.requires_grad and p.dtype != torch.float32)
print(f"trainable params not in fp32: {n_bad}")
model.print_trainable_parameters()

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
    bf16=False,                 # see the precision note above - both are off
    fp16=False,                 # on purpose, to avoid the GradScaler entirely
    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": False},
    logging_steps=5,
    eval_strategy="steps",
    eval_steps=EVAL_EVERY,
    save_strategy="steps",
    save_steps=EVAL_EVERY,
    save_total_limit=2,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
    greater_is_better=False,
    seed=SEED,
    report_to="none",
    push_to_hub=PUSH_TO_HUB,
    hub_model_id=HF_REPO_ID if PUSH_TO_HUB else None,
    hub_token=HF_TOKEN if PUSH_TO_HUB else None,
)

# peft_config is deliberately NOT passed - the model is already a PeftModel.
trainer = SFTTrainer(
    model=model,
    args=sft_config,
    train_dataset=train_ds,
    eval_dataset=val_ds,
    processing_class=tokenizer,
)

steps = max(1, len(train_ds) // (BATCH_SIZE * GRAD_ACCUM)) * EPOCHS
print(f"\n~{steps} total steps, evaluating every {EVAL_EVERY}\n")

trainer.train()

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
trainer.save_model(ADAPTER_OUTPUT_DIR)
tokenizer.save_pretrained(ADAPTER_OUTPUT_DIR)
print(f"\nAdapter saved to {ADAPTER_OUTPUT_DIR}")

if PUSH_TO_HUB:
    trainer.push_to_hub()
    print(f"Adapter pushed to https://huggingface.co/{HF_REPO_ID}")
else:
    print("PUSH_TO_HUB is false - local only. On Colab the runtime is wiped on "
          "disconnect, so set PUSH_TO_HUB=true.")