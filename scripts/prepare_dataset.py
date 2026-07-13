"""Build the fine-tuning dataset for the Vigor fitness coach.

Loads four fitness Q&A datasets from the Hugging Face Hub, normalizes them into a
single chat/messages format, deduplicates, shuffles, splits into
train/validation/test, and writes JSONL files to data/processed/.

Run from the project root:
    python scripts/prepare_dataset.py

The hammamwahab/fitness-qa dataset (123k rows) is intentionally excluded - it is
templated, extractive, and refusal-heavy. Quality over volume for a small model.
"""

import json
import os
import random
import re
from pathlib import Path

from datasets import load_dataset
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config - edit these freely
# ---------------------------------------------------------------------------

# Keep this identical to the system prompt used at inference time so the model
# learns to behave under the same instructions it will actually see.
SYSTEM_PROMPT = (
    "You are a knowledgeable personal fitness and nutrition coach. Give "
    "accurate, practical, and concise advice on fitness, bodybuilding, "
    "exercises, nutrition and supplements. Never suggest or explain steroid use "
    "or advise on illegal substances. If a question is unrelated to fitness, say "
    "you are a fitness coach, not a general knowledge assistant. If you do not "
    "know an answer, say so."
)

# Each entry: Hub id + which normalizer to use.
DATASETS = [
    {"id": "its-myrto/fitness-question-answers", "normalizer": "qa_columns"},
    {"id": "chibbss/fitness-chat-prompt-completion-dataset", "normalizer": "instruction_output"},
    {"id": "onurSakar/GYM-Exercise", "normalizer": "inst_text"},
    {"id": "PandurangMopgar/fitness__data", "normalizer": "inst_text"},
]

MAX_EXAMPLES = 20000        # hard cap on total examples (we expect ~3k)
SEED = 42                   # fixed seed for reproducible shuffle/split
VAL_RATIO = 0.05            # fraction held out for validation
TEST_RATIO = 0.05           # fraction held out for final test
MIN_CHARS = 20              # drop examples where either side is shorter than this

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "processed"

# ---------------------------------------------------------------------------
# Parsing helpers for the Llama [INST] text format
# ---------------------------------------------------------------------------

_SYS_BLOCK = re.compile(r"<<SYS>>.*?<</SYS>>", re.DOTALL)
_INST_PATTERN = re.compile(r"\[INST\](.*?)\[/INST\](.*)", re.DOTALL)


def _parse_inst_text(text):
    """Parse '<s>[INST] <<SYS>>..<</SYS>> {user} [/INST]{assistant} </s>'.

    Handles rows with and without a <<SYS>> block. Returns (user, assistant)
    or None if the row does not match the expected shape.
    """
    text = _SYS_BLOCK.sub("", text)          # strip the generic system block
    match = _INST_PATTERN.search(text)
    if not match:
        return None
    user = match.group(1).replace("<s>", "").strip()
    assistant = match.group(2).replace("</s>", "").replace("<s>", "").strip()
    return user, assistant


# ---------------------------------------------------------------------------
# Per-dataset normalizers -> each returns a list of (user, assistant) tuples
# ---------------------------------------------------------------------------

def _norm_qa_columns(ds):
    # its-myrto: columns 'Question' / 'Answer' (plus an unused index column)
    return [((r.get("Question") or "").strip(), (r.get("Answer") or "").strip()) for r in ds]


def _norm_instruction_output(ds):
    # chibbss: columns 'instruction' / 'output'
    return [((r.get("instruction") or "").strip(), (r.get("output") or "").strip()) for r in ds]


def _norm_inst_text(ds):
    # onurSakar + Pandurang: single 'text' field in Llama [INST] format
    pairs = []
    for r in ds:
        parsed = _parse_inst_text(r.get("text") or "")
        if parsed:
            pairs.append(parsed)
    return pairs


NORMALIZERS = {
    "qa_columns": _norm_qa_columns,
    "instruction_output": _norm_instruction_output,
    "inst_text": _norm_inst_text,
}

# ---------------------------------------------------------------------------
# Validity / dedup
# ---------------------------------------------------------------------------

# Known placeholder / junk values to discard (e.g. the '[INST] instruction
# [/INST] output' template row in Pandurang).
_JUNK_USERS = {"instruction"}
_JUNK_ASSISTANTS = {"output", "i don't have data on that"}

# Off-target patterns: structured "profile in, one-line exercise out" rows.
# These teach a rigid "Suggested Exercise: X" template that does not match how
# real users talk, so we drop them entirely.
_PROFILE_USER = re.compile(r"^\s*fitness goal\s*:", re.IGNORECASE)
_TEMPLATE_ANSWER = re.compile(r"^\s*suggested exercise\s*:", re.IGNORECASE)


def _is_valid(user, assistant):
    if len(user) < MIN_CHARS or len(assistant) < MIN_CHARS:
        return False
    if user.lower() in _JUNK_USERS or assistant.lower() in _JUNK_ASSISTANTS:
        return False
    if _PROFILE_USER.search(user) or _TEMPLATE_ANSWER.search(assistant):
        return False
    return True


def _dedup_key(user, assistant):
    # Collapse whitespace and lowercase so trivial variants collapse together.
    u = " ".join(user.lower().split())
    a = " ".join(assistant.lower().split())
    return (u, a)


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------

def _write_jsonl(path, pairs):
    with open(path, "w", encoding="utf-8") as f:
        for user, assistant in pairs:
            obj = {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": assistant},
                ]
            }
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    load_dotenv()
    token = os.getenv("HF_TOKEN") or None

    all_pairs = []
    for entry in DATASETS:
        ds_id = entry["id"]
        normalizer = NORMALIZERS[entry["normalizer"]]
        try:
            ds = load_dataset(ds_id, token=token)
            split = ds[list(ds.keys())[0]]          # these all have one 'train' split
            pairs = normalizer(split)
            print(f"  {ds_id}: {len(pairs)} raw pairs")
            all_pairs.extend(pairs)
        except Exception as e:
            print(f"  {ds_id}: ERROR {type(e).__name__}: {e} (skipped)")

    print(f"\nTotal raw pairs: {len(all_pairs)}")

    # Filter junk / empties.
    valid = [(u, a) for (u, a) in all_pairs if _is_valid(u, a)]
    print(f"After validity filter: {len(valid)}")

    # Deduplicate on normalized (user, assistant).
    seen = set()
    deduped = []
    for user, assistant in valid:
        key = _dedup_key(user, assistant)
        if key not in seen:
            seen.add(key)
            deduped.append((user, assistant))
    print(f"After dedup: {len(deduped)}")

    # Shuffle (fixed seed) and cap.
    random.seed(SEED)
    random.shuffle(deduped)
    if len(deduped) > MAX_EXAMPLES:
        deduped = deduped[:MAX_EXAMPLES]
        print(f"Capped to MAX_EXAMPLES: {len(deduped)}")

    # Split: test first, then validation, remainder is train.
    n = len(deduped)
    n_test = int(n * TEST_RATIO)
    n_val = int(n * VAL_RATIO)
    test = deduped[:n_test]
    val = deduped[n_test:n_test + n_val]
    train = deduped[n_test + n_val:]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _write_jsonl(OUTPUT_DIR / "train.jsonl", train)
    _write_jsonl(OUTPUT_DIR / "validation.jsonl", val)
    _write_jsonl(OUTPUT_DIR / "test.jsonl", test)

    print(f"\nWrote to {OUTPUT_DIR}:")
    print(f"  train.jsonl      {len(train)}")
    print(f"  validation.jsonl {len(val)}")
    print(f"  test.jsonl       {len(test)}")


if __name__ == "__main__":
    main()