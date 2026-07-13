"""QLoRA fine-tune Qwen2.5-1.5B-Instruct into the Vigor fitness coach.

Loads the base model in 4-bit, attaches a LoRA adapter, trains on the chat/messages
JSONL produced by prepare_dataset.py, and (optionally) pushes the adapter to the
Hugging Face Hub so it can be pulled down elsewhere for merging/export.

Runs on a Colab T4 (16GB) or a local NVIDIA GPU (e.g. RTX 4070 Ti 12GB).

    python train_qlora.py

Paths and the Hub repo can be overridden with environment variables:
    TRAIN_PATH, VAL_PATH, HF_REPO_ID, PUSH_TO_HUB, HF_TOKEN
"""

import os
from pathlib import Path

import torch
from datasets import load_dataset
from dotenv import load_dotenv
from peft import LoraConfig, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

# ---------------------------------------------------------------------------
# Config - edit these freely
# ---------------------------------------------------------------------------
BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

# Data - defaults to the repo layout; override with env vars in Colab if needed.
_PROJECT = Path(__file__).resolve().parent.parent
TRAIN_PATH = os.getenv("TRAIN_PATH", str(_PROJECT / "data" / "processed" / "train.jsonl"))
VAL_PATH = os.getenv("VAL_PATH", str(_PROJECT / "data" / "processed" / "validation.jsonl"))

# Where the trained adapter is written locally.
ADAPTER_OUTPUT_DIR = os.getenv("ADAPTER_OUTPUT_DIR", str(_PROJECT / "outputs" / "adapter"))

# Hub push - set HF_REPO_ID to "your-username/vigor-coach-qlora" and a WRITE token.
HF_REPO_ID = os.getenv("HF_REPO_ID", "your-username/vigor-coach-qlora")
PUSH_TO_HUB = os.getenv("PUSH_TO_HUB", "false").lower() == "true"

# LoRA hyperparameters.
LORA_R = 16
LORA_ALPHA = 32                 # rule of thumb: ~2x rank
LORA_DROPOUT = 0.05
TARGET_MODULES = [              # attention + MLP projections for Qwen2.5
    "q_proj", "k_proj", "v_proj", "o_proj",
    "gate_proj", "up_proj", "down_proj",
]

# Training hyperparameters.
EPOCHS = 3
BATCH_SIZE = 4                  # per device
GRAD_ACCUM = 4                  # effective batch = BATCH_SIZE * GRAD_ACCUM = 16
LEARNING_RATE = 2e-4            # higher LR is typical for LoRA
MAX_LENGTH = 1024               # covers system + question + answer with headroom
SEED = 42

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()
HF_TOKEN = os.getenv("HF_TOKEN") or None

if not torch.cuda.is_available():
    raise SystemExit(
        "No CUDA GPU detected. QLoRA training needs an NVIDIA GPU - run this on "
        "Colab (T4) or your home machine, not the work laptop."
    )

# T4 (Colab) has no native bf16; Ada cards (4070 Ti) do. Detect and adapt so the
# same script runs on both without an OOM or dtype crash.
bf16_ok = torch.cuda.is_bf16_supported()
compute_dtype = torch.bfloat16 if bf16_ok else torch.float16
print(f"GPU: {torch.cuda.get_device_name(0)} | bf16 supported: {bf16_ok}")

# ---------------------------------------------------------------------------
# Load model (4-bit) + tokenizer
# ---------------------------------------------------------------------------
quant_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=compute_dtype,
    bnb_4bit_quant_type="nf4",
)

tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, token=HF_TOKEN)
if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

model = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL,
    quantization_config=quant_config,
    device_map="auto",
    token=HF_TOKEN,
)
model.config.use_cache = False          # required with gradient checkpointing
model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)

# ---------------------------------------------------------------------------
# LoRA + data
# ---------------------------------------------------------------------------
peft_config = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    target_modules=TARGET_MODULES,
    bias="none",
    task_type="CAUSAL_LM",
)

# Each JSONL line is {"messages": [...]} - SFTTrainer applies Qwen's chat
# template automatically because it recognizes the conversational format.
train_ds = load_dataset("json", data_files=TRAIN_PATH, split="train")
val_ds = load_dataset("json", data_files=VAL_PATH, split="train")
print(f"Train: {len(train_ds)} | Validation: {len(val_ds)}")

# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------
sft_config = SFTConfig(
    output_dir=ADAPTER_OUTPUT_DIR,
    num_train_epochs=EPOCHS,
    per_device_train_batch_size=BATCH_SIZE,
    per_device_eval_batch_size=BATCH_SIZE,
    gradient_accumulation_steps=GRAD_ACCUM,
    learning_rate=LEARNING_RATE,
    max_length=MAX_LENGTH,
    warmup_ratio=0.03,
    lr_scheduler_type="cosine",
    optim="paged_adamw_8bit",           # memory-friendly optimizer for QLoRA
    bf16=bf16_ok,
    fp16=not bf16_ok,
    gradient_checkpointing=True,
    gradient_checkpointing_kwargs={"use_reentrant": False},
    logging_steps=10,
    eval_strategy="steps",
    eval_steps=50,
    save_strategy="steps",
    save_steps=50,
    save_total_limit=2,
    load_best_model_at_end=True,        # keep the checkpoint with lowest val loss
    metric_for_best_model="eval_loss",
    seed=SEED,
    report_to="none",                   # switch to "wandb" if you want live charts
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
    peft_config=peft_config,
)

trainer.train()

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
trainer.save_model(ADAPTER_OUTPUT_DIR)          # writes adapter weights locally
tokenizer.save_pretrained(ADAPTER_OUTPUT_DIR)
print(f"\nAdapter saved to {ADAPTER_OUTPUT_DIR}")

if PUSH_TO_HUB:
    trainer.push_to_hub()
    print(f"Adapter pushed to https://huggingface.co/{HF_REPO_ID}")
else:
    print("PUSH_TO_HUB is false - adapter saved locally only.")