"""Merge the trained LoRA adapter into the base model.

llama.cpp cannot read LoRA weights sitting beside a base model - it needs one
merged set. The base is loaded in fp16 rather than 4-bit on purpose: merging
into an already-quantized model bakes the quantization error into the weights,
and the GGUF export quantizes again afterwards anyway.

Needs roughly 4GB of RAM. Runs on CPU if the GPU is busy.

    python scripts/merge_adapter.py
    python scripts/merge_adapter.py --adapter outputs/adapter/checkpoint-46
"""

import argparse
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

BASE_MODEL = "Qwen/Qwen3-1.7B"
_PROJECT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default=str(_PROJECT / "outputs" / "adapter"))
    ap.add_argument("--out", default=str(_PROJECT / "outputs" / "merged"))
    ap.add_argument("--cpu", action="store_true", help="force CPU, slower but frees VRAM")
    args = ap.parse_args()

    adapter = Path(args.adapter)
    out = Path(args.out)

    if not (adapter / "adapter_model.safetensors").exists():
        raise SystemExit(
            f"No adapter_model.safetensors in {adapter}\n"
            "Point --adapter at the folder holding the trained adapter."
        )

    device = "cpu" if args.cpu or not torch.cuda.is_available() else "auto"
    print(f"loading {BASE_MODEL} in fp16 on {device}")

    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        dtype=torch.float16,
        device_map=device,
    )

    print(f"applying adapter from {adapter}")
    model = PeftModel.from_pretrained(model, str(adapter))

    print("merging")
    model = model.merge_and_unload()

    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out), safe_serialization=True)

    # The tokenizer and chat template must travel with the model - the template
    # gets baked into the GGUF and is what produces the empty think block.
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    tokenizer.save_pretrained(str(out))

    print(f"\nmerged model written to {out}")
    for f in sorted(out.iterdir()):
        print(f"  {f.name}  {f.stat().st_size / 1e6:.0f} MB")

    print("\nNext:")
    print(f"  cd llama.cpp")
    print(f"  python convert_hf_to_gguf.py {out} --outfile vigor-f16.gguf --outtype f16")


if __name__ == "__main__":
    main()