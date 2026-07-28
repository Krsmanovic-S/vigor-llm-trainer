"""Convert generated conversations and curated seeds into training JSONL.

Produces train/validation/test files where each line has:
  messages - the conversation in role/content form, with tool_calls and tool
             results as separate turns
  tools    - the tool schema, identical for every example
  text     - the fully rendered training string, produced with
             enable_thinking=False

The rendered text is what matters. It must byte-match what the app produces at
inference, otherwise the model is trained on one prompt shape and used with
another. Run with --preview to print one rendered example for that comparison.

    python scripts/build_training_data.py --preview
    python scripts/build_training_data.py
"""

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from transformers import AutoTokenizer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_MODEL = "Qwen/Qwen3-1.7B"

SYSTEM_PROMPT = (
    "You are a knowledgeable personal fitness and nutrition coach inside a "
    "workout tracking app. Give accurate, practical, and concise advice on "
    "fitness, exercises, nutrition and supplements. Never advise on steroids or "
    "other performance enhancing drugs. If a question is unrelated to fitness, "
    "say you are a fitness coach, not a general assistant. Do not diagnose pain "
    "or injury - refer the user to a professional. If you do not know an answer, "
    "say so."
)

SEED = 42
VAL_RATIO = 0.05
TEST_RATIO = 0.05
MIN_ASSISTANT_CHARS = 80        # drop trimmed stubs

_PROJECT = Path(__file__).resolve().parent.parent
CONVERSATIONS = _PROJECT / "data" / "processed" / "conversations.jsonl"
SEEDS = _PROJECT / "data" / "raw" / "curated_data.md"
TOOLS_PATH = _PROJECT / "configs" / "tools.json"
OUT_DIR = _PROJECT / "data" / "processed"

BLOCK_RE = re.compile(r"^=== (USER|ASSISTANT|TOOL) ===[ \t]*$", re.MULTILINE)
TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_blocks(text):
    parts = BLOCK_RE.split(text)
    return [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]


def to_messages(text):
    """Turn '=== ' blocks into the role/content list the chat template wants."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for role, content in parse_blocks(text):
        if role == "USER":
            messages.append({"role": "user", "content": content})

        elif role == "ASSISTANT":
            prose = TOOLCALL_RE.sub("", content).strip()
            calls = []
            for raw in TOOLCALL_RE.findall(content):
                try:
                    call = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                calls.append({
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call.get("arguments", {}),
                    },
                })
            msg = {"role": "assistant", "content": prose}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)

        elif role == "TOOL":
            messages.append({"role": "tool", "content": content})

    return messages


def validate(messages):
    """Structural checks that must hold after conversion."""
    if len(messages) < 3:
        return "too few turns"
    if messages[1]["role"] != "user":
        return "does not open with a user turn"
    if messages[-1]["role"] != "assistant":
        return "does not end with an assistant turn"
    if messages[-1].get("tool_calls"):
        return "final turn has an unanswered tool call"
    if len(messages[-1]["content"]) < MIN_ASSISTANT_CHARS:
        return "final assistant turn is a stub"

    # Every tool_call must be followed by exactly one tool message.
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            want = len(m["tool_calls"])
            got = 0
            for nxt in messages[i + 1:]:
                if nxt["role"] != "tool":
                    break
                got += 1
            if got != want:
                return f"{want} tool calls but {got} results"
    return None


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def load_seeds(path):
    """Parse curated_data.md into the same shape as generated conversations."""
    if not path.exists():
        print(f"  WARNING: seeds not found at {path}, skipping")
        return []
    text = path.read_text(encoding="utf-8")
    rows = []
    for block in re.split(r"^## Conversation ", text, flags=re.MULTILINE)[1:]:
        m = re.search(r"=== CATEGORY ===[ \t]*\n?[ \t]*(.+)", block)
        if not m:
            continue
        category = m.group(1).strip()
        # Everything from the first USER marker onward is the conversation.
        idx = block.find("=== USER ===")
        if idx == -1:
            continue
        user_m = re.search(r"=== USER ===[ \t]*\n?[ \t]*(.+)", block)
        rows.append({
            "category": category,
            "scenario": user_m.group(1).strip() if user_m else "",
            "conversation": block[idx:].strip(),
            "source": "seed",
        })
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true",
                    help="print one rendered example and exit")
    args = ap.parse_args()

    if not TOOLS_PATH.exists():
        sys.exit(f"Tool definitions not found: {TOOLS_PATH}")
    tools = json.loads(TOOLS_PATH.read_text(encoding="utf-8"))

    rows = []
    if CONVERSATIONS.exists():
        for line in CONVERSATIONS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["source"] = "synthetic"
                rows.append(r)
    print(f"synthetic: {len(rows)}")

    seeds = load_seeds(SEEDS)
    print(f"seeds:     {len(seeds)}")
    rows.extend(seeds)

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)

    def render(messages):
        """Render exactly as the app will at inference."""
        try:
            return tokenizer.apply_chat_template(
                messages,
                tools=tools,
                tokenize=False,
                add_generation_prompt=False,
                enable_thinking=False,
            )
        except TypeError:
            # Templates that do not know enable_thinking simply ignore it.
            return tokenizer.apply_chat_template(
                messages, tools=tools, tokenize=False, add_generation_prompt=False
            )

    # --- convert -----------------------------------------------------------
    examples, dropped = [], Counter()
    for row in rows:
        messages = to_messages(row["conversation"])
        if messages is None:
            dropped["unparseable tool_call"] += 1
            continue
        problem = validate(messages)
        if problem:
            dropped[problem] += 1
            continue
        examples.append({
            "category": row["category"],
            "source": row["source"],
            "messages": messages,
            "tools": tools,
            "text": render(messages),
        })

    print(f"\nconverted: {len(examples)}")
    if dropped:
        print("dropped:")
        for reason, n in dropped.most_common():
            print(f"  {n:4d}  {reason}")

    if args.preview:
        ex = next(e for e in examples if any(m.get("tool_calls") for m in e["messages"]))
        print("\n" + "=" * 70)
        print("RENDERED EXAMPLE - diff this against what your app produces")
        print("=" * 70)
        print(ex["text"])
        print("=" * 70)
        print(f"length: {len(ex['text'])} chars, "
              f"{len(tokenizer(ex['text'])['input_ids'])} tokens")
        thinking = [t for t in ("<think>", "</think>") if t in ex["text"]]
        print(f"thinking tags present: {thinking or 'none'}")
        return

    # --- dedupe on the opening user message --------------------------------
    seen, unique = set(), []
    for ex in examples:
        key = " ".join(ex["messages"][1]["content"].lower().split())
        if key in seen:
            continue
        seen.add(key)
        unique.append(ex)
    print(f"after dedupe: {len(unique)}")

    # --- token length report ------------------------------------------------
    lengths = sorted(len(tokenizer(e["text"])["input_ids"]) for e in unique)
    n = len(lengths)
    print(f"\ntokens per example - min {lengths[0]}, median {lengths[n // 2]}, "
          f"p90 {lengths[int(n * 0.9)]}, max {lengths[-1]}")
    over = sum(1 for x in lengths if x > 4096)
    if over:
        print(f"  {over} example(s) over 4096 tokens - they will be truncated "
              f"during training unless max_length is raised")

    # --- stratified split ---------------------------------------------------
    random.seed(SEED)
    by_cat = defaultdict(list)
    for ex in unique:
        by_cat[ex["category"].split(" / ")[0]].append(ex)

    train, val, test = [], [], []
    for cat, items in by_cat.items():
        random.shuffle(items)
        n_test = max(1, int(len(items) * TEST_RATIO)) if len(items) >= 20 else 0
        n_val = max(1, int(len(items) * VAL_RATIO)) if len(items) >= 20 else 0
        test.extend(items[:n_test])
        val.extend(items[n_test:n_test + n_val])
        train.extend(items[n_test + n_val:])

    for split in (train, val, test):
        random.shuffle(split)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, split in (("train", train), ("validation", val), ("test", test)):
        path = OUT_DIR / f"{name}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for ex in split:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
        print(f"\n{name}.jsonl: {len(split)}")

    print("\nper category (train / val / test):")
    for cat in sorted(by_cat):
        t = sum(1 for e in train if e["category"].split(" / ")[0] == cat)
        v = sum(1 for e in val if e["category"].split(" / ")[0] == cat)
        s = sum(1 for e in test if e["category"].split(" / ")[0] == cat)
        print(f"  {cat:24s} {t:4d} / {v:3d} / {s:3d}")

    tool_using = sum(1 for e in train if any(m.get("tool_calls") for m in e["messages"]))
    print(f"\ntool-calling share of train: {tool_using}/{len(train)} "
          f"({tool_using / len(train) * 100:.0f}%)")


if __name__ == "__main__":
    main()