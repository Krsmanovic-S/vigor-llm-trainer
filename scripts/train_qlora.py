"""QLoRA fine-tune Qwen3-1.7B into the Vigor fitness coach.

Changes from the previous run, which trained fine (loss 0.84 -> 0.80, healthy
grad norms) but produced a broken adapter:

  - load_best_model_at_end is OFF. Reloading a checkpoint into a manually
    wrapped PeftModel is off the standard path and is the prime suspect for the
    corrupted final artifact.
  - Checkpoints are saved once per epoch so you can test each and pick one.
  - A short generation runs in-process right after training, before saving. If
    that output is coherent but the saved adapter is not, the problem is in
    saving or loading rather than in training.
  - PUSH_TO_HUB defaults to false. Push only after testing a checkpoint.

The precision setup is unchanged because it demonstrably worked.

    python train_qlora.py
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

HF_REPO_ID = os.getenv("HF_REPO_ID", "your-username/vigor-coach-qlora")
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
GRAD_ACCUM = 16
LEARNING_RATE = 2e-4
MAX_LENGTH = 2048
EVAL_EVERY = 10
WARMUP_STEPS = 5
SEED = 42

COMPUTE_DTYPE = torch.float16       # T4 has fp16 tensor cores, only emulated bf16

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN") or None

if not torch.cuda.is_available():
    raise SystemExit("No CUDA GPU detected.")

print(f"GPU: {torch.cuda.get_device_name(0)} | compute dtype: {COMPUTE_DTYPE} | autocast: off")

train_ds = load_dataset("json", data_files=TRAIN_PATH, split="train")
val_ds = load_dataset("json", data_files=VAL_PATH, split="train")

if "text" not in train_ds.column_names:
    raise SystemExit("No 'text' column. Run scripts/build_training_data.py first.")

train_ds = train_ds.select_columns(["text"])
val_ds = val_ds.select_columns(["text"])
print(f"Train: {len(train_ds)} | Validation: {len(val_ds)}")

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

# Trainable params in fp32. With autocast off there is no loss scaling, and
# fp16 gradients would underflow without it.
model = get_peft_model(model, peft_config)
for _name, _param in model.named_parameters():
    if _param.requires_grad:
        _param.data = _param.data.float()
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
    bf16=False,
    fp16=False,
    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": False},
    logging_steps=5,
    eval_strategy="steps",
    eval_steps=EVAL_EVERY,
    save_strategy="epoch",              # one checkpoint per epoch, pick by testing
    save_total_limit=3,
    load_best_model_at_end=False,       # removed - suspected cause of corruption
    seed=SEED,
    report_to="none",
    push_to_hub=PUSH_TO_HUB,
    hub_model_id=HF_REPO_ID if PUSH_TO_HUB else None,
    hub_token=HF_TOKEN if PUSH_TO_HUB else None,
)

trainer = SFTTrainer(
    model=model,
    args=sft_config,
    train_dataset=train_ds,
    eval_dataset=val_ds,
    processing_class=tokenizer,
)

steps = max(1, len(train_ds) // (BATCH_SIZE * GRAD_ACCUM)) * EPOCHS
print(f"\n~{steps} total steps, evaluating every {EVAL_EVERY}, saving each epoch\n")

trainer.train()

# ---------------------------------------------------------------------------
# In-memory sanity check - before anything is saved or reloaded
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("SANITY CHECK - generating from the model still in memory")
print("=" * 70)

model.config.use_cache = True
model.eval()

probe = [
    {"role": "system", "content": "You are a knowledgeable personal fitness coach."},
    {"role": "user", "content": "how much protein should I eat"},
]
try:
    text = tokenizer.apply_chat_template(
        probe, tokenize=False, add_generation_prompt=True, enable_thinking=False)
except TypeError:
    text = tokenizer.apply_chat_template(probe, tokenize=False, add_generation_prompt=True)

inputs = tokenizer([text], return_tensors="pt").to(model.device)
with torch.no_grad():
    out = model.generate(**inputs, max_new_tokens=150, do_sample=False)
print(tokenizer.decode(out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True).strip())
print("=" * 70)
print("If the above is coherent, training is fine and any breakage is in "
      "saving or loading.\nIf it is word salad, the problem is in training "
      "itself.\n")

model.config.use_cache = False

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
trainer.save_model(ADAPTER_OUTPUT_DIR)
tokenizer.save_pretrained(ADAPTER_OUTPUT_DIR)
print(f"Adapter saved to {ADAPTER_OUTPUT_DIR}")
print("Per-epoch checkpoints are in checkpoint-* subfolders. Test each with "
      "test_model.py before pushing anything.")

if PUSH_TO_HUB:
    trainer.push_to_hub()
    print(f"Pushed to https://huggingface.co/{HF_REPO_ID}")