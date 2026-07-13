# vigor-coach-llm

On-device fitness coach LLM - fine-tuning Qwen2.5-1.5B-Instruct with QLoRA and exporting to Q4_K_M GGUF.

## Structure

```
vigor-coach-llm/
├── data/
│   ├── raw/                    # source material (nutrition tables, exercise DB exports)
│   └── processed/              # instruction/response JSONL for training
├── scripts/
│   ├── prepare_dataset.py      # build train.jsonl from raw sources
│   ├── train_qlora.py          # QLoRA fine-tune
│   ├── merge_adapter.py        # merge LoRA into base model
│   └── export_gguf.py          # convert + quantize to Q4_K_M GGUF
├── configs/
│   └── train_config.yaml
├── outputs/
│   ├── adapter/                # LoRA adapter weights
│   └── gguf/                   # final quantized model
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -r requirements.txt

copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
```

Then open `.env` and paste your Hugging Face token into `HUGGINGFACE_TOKEN=`.

## Pipeline

1. `scripts/prepare_dataset.py` - build `data/processed/train.jsonl` from `data/raw/`
2. `scripts/train_qlora.py` - QLoRA fine-tune, writes to `outputs/adapter/`
3. `scripts/merge_adapter.py` - merge the adapter into the base model
4. `scripts/export_gguf.py` - convert and quantize to Q4_K_M, writes to `outputs/gguf/`
