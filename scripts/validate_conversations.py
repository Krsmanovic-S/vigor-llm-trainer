"""Validate and repair generated conversations.

Two jobs:
  1. Repair what can be fixed deterministically - aggregate arithmetic, missing
     scope envelopes, pipe characters in exercise names, wrong return shapes,
     stale "returned" counts, duplicate and out-of-order sessions.
  2. Reject what cannot - missing turns, unparseable JSON, truncation, leaked
     code. Rejected rows are removed from conversations.jsonl so that rerunning
     phase B regenerates exactly those scenarios.

    python scripts/validate_conversations.py            # report only
    python scripts/validate_conversations.py --apply    # rewrite files
"""

import argparse
import json
import re
import shutil
from collections import Counter
from pathlib import Path

_PROJECT = Path(__file__).resolve().parent.parent
PATH = _PROJECT / "data" / "processed" / "conversations.jsonl"
REJECTS = _PROJECT / "data" / "processed" / "conversations_rejected.jsonl"

READ_SCOPES = {
    "profile", "measurements", "muscle_balance", "workout_history",
    "exercise_history", "templates", "active_workout", "menstrual",
}
NAME_KEYS = {"name", "exercise_name", "replacement_name", "template_name", "from", "to", "exercise"}
LIST_KEYS = ("sessions", "workouts", "exercises", "templates")

BLOCK_RE = re.compile(r"^=== (USER|ASSISTANT|TOOL) ===[ \t]*$", re.MULTILINE)
TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
SET_RE = re.compile(r"^\s*(\d+)\s*x\s*([\d.]+)\s*$")
LEAK_RE = re.compile(r"\bif false else\b|\bNone\b|\bundefined\b|Something went wrong")

stats = Counter()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_blocks(text):
    """Split '=== ROLE ===' text into [(role, content), ...]."""
    parts = BLOCK_RE.split(text)
    if len(parts) < 3:
        return []
    blocks = []
    for i in range(1, len(parts) - 1, 2):
        blocks.append((parts[i], parts[i + 1].strip()))
    return blocks


def rebuild(blocks):
    return "\n\n".join(f"=== {role} ===\n{content}" for role, content in blocks)


# ---------------------------------------------------------------------------
# Repairs
# ---------------------------------------------------------------------------

def fix_aggregates(obj):
    """Recompute total_reps, top_weight and volume from the sets array."""
    n = 0
    if isinstance(obj, dict):
        sets = obj.get("sets")
        if isinstance(sets, list) and sets and all(isinstance(s, str) for s in sets):
            parsed = [SET_RE.match(s) for s in sets]
            if all(parsed):
                reps = sum(int(m.group(1)) for m in parsed)
                top = max(float(m.group(2)) for m in parsed)
                vol = round(sum(int(m.group(1)) * float(m.group(2)) for m in parsed), 1)
                for key, want in (("total_reps", reps), ("top_weight", top), ("volume", vol)):
                    if key in obj and obj[key] != want:
                        obj[key] = want
                        n += 1
        for v in obj.values():
            n += fix_aggregates(v)
    elif isinstance(obj, list):
        for v in obj:
            n += fix_aggregates(v)
    return n


def fix_pipes(obj):
    """The | is a catalog column separator, never part of an exercise name."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in NAME_KEYS and isinstance(v, str) and "|" in v:
                obj[k] = v.split("|", 1)[0].strip()
                n += 1
            else:
                n += fix_pipes(v)
    elif isinstance(obj, list):
        for v in obj:
            n += fix_pipes(v)
    return n


def fix_counts(obj):
    """Make 'returned' agree with the list beside it, and drop duplicate dates."""
    n = 0
    if isinstance(obj, dict):
        for key in LIST_KEYS:
            items = obj.get(key)
            if not isinstance(items, list):
                continue
            if key == "sessions":
                seen, unique = set(), []
                for it in items:
                    date = it.get("date") if isinstance(it, dict) else None
                    if date and date in seen:
                        n += 1
                        continue
                    if date:
                        seen.add(date)
                    unique.append(it)
                if len(unique) != len(items):
                    items = unique
                    obj[key] = items
                # Newest first.
                if all(isinstance(i, dict) and "date" in i for i in items):
                    ordered = sorted(items, key=lambda i: i["date"], reverse=True)
                    if ordered != items:
                        obj[key] = ordered
                        n += 1
            if "returned" in obj and obj["returned"] != len(obj[key]):
                obj["returned"] = len(obj[key])
                n += 1
        for v in obj.values():
            n += fix_counts(v)
    elif isinstance(obj, list):
        for v in obj:
            n += fix_counts(v)
    return n


def fix_swap_shape(obj):
    """swap_exercise returns from/to, not exercise_name/replacement_name."""
    if (isinstance(obj, dict) and obj.get("action") == "swap_exercise"
            and "replacement_name" in obj):
        obj["from"] = obj.pop("exercise_name", obj.get("from"))
        obj["to"] = obj.pop("replacement_name")
        return 1
    return 0


def fix_envelope(result, call):
    """read_user_data results must be keyed by scope name."""
    if not isinstance(result, dict) or "error" in result:
        return 0
    if not call or call.get("name") != "read_user_data":
        return 0
    if all(k in READ_SCOPES for k in result):
        return 0                       # already correct
    scopes = call.get("arguments", {}).get("scope") or []
    if isinstance(scopes, str):
        scopes = [scopes]
    if len(scopes) != 1:
        return 0                       # cannot infer which scope - leave it
    result_copy = dict(result)
    result.clear()
    result[scopes[0]] = result_copy
    return 1


# ---------------------------------------------------------------------------
# Per conversation
# ---------------------------------------------------------------------------

def process(row):
    """Returns (ok, repaired_text_or_None, [reasons])."""
    text = row["conversation"]
    reasons = []

    if LEAK_RE.search(text):
        reasons.append("leaked code or narration in output")

    blocks = parse_blocks(text)
    if not blocks:
        return False, None, ["no === blocks parsed"]
    if blocks[0][0] != "USER":
        reasons.append(f"starts with {blocks[0][0]}, not USER")
    if blocks[-1][0] != "ASSISTANT":
        reasons.append(f"ends with {blocks[-1][0]}, not ASSISTANT")

    # Collect tool calls in order, and validate their JSON.
    calls = []
    for role, content in blocks:
        if role != "ASSISTANT":
            continue
        for raw in TOOLCALL_RE.findall(content):
            try:
                calls.append(json.loads(raw))
            except json.JSONDecodeError:
                reasons.append("tool_call is not valid JSON")
                calls.append(None)
        if "<tool_call>" in content and "</tool_call>" not in content:
            reasons.append("unclosed tool_call (likely truncated)")

    tool_blocks = [i for i, (r, _) in enumerate(blocks) if r == "TOOL"]
    if len(calls) != len(tool_blocks):
        reasons.append(f"{len(calls)} tool calls vs {len(tool_blocks)} TOOL blocks")

    if reasons:
        return False, None, reasons

    # Repair each TOOL block, paired with its call by position.
    fixes = 0
    for n, idx in enumerate(tool_blocks):
        raw = blocks[idx][1]
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            return False, None, ["TOOL block is not valid JSON"]

        call = calls[n] if n < len(calls) else None
        f = 0
        f += fix_envelope(result, call)
        stats["envelope"] += f
        a = fix_aggregates(result); stats["aggregates"] += a
        p = fix_pipes(result);      stats["pipes"] += p
        c = fix_counts(result);     stats["counts"] += c
        s = fix_swap_shape(result); stats["swap_shape"] += s
        f += a + p + c + s
        if f:
            blocks[idx] = ("TOOL", json.dumps(result, ensure_ascii=False))
            fixes += f

    # Pipes can also appear in the tool call arguments themselves.
    for i, (role, content) in enumerate(blocks):
        if role != "ASSISTANT" or "<tool_call>" not in content:
            continue
        def _repair(m):
            nonlocal fixes
            try:
                call = json.loads(m.group(1))
            except json.JSONDecodeError:
                return m.group(0)
            p = fix_pipes(call)
            if p:
                fixes += p
                stats["pipes"] += p
                return f"<tool_call>\n{json.dumps(call, ensure_ascii=False)}\n</tool_call>"
            return m.group(0)
        blocks[i] = (role, TOOLCALL_RE.sub(_repair, content))

    return True, (rebuild(blocks) if fixes else text), []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes to disk")
    args = ap.parse_args()

    if not PATH.exists():
        raise SystemExit(f"Not found: {PATH}")

    rows = [json.loads(l) for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"{len(rows)} conversations\n")

    kept, rejected, repaired = [], [], 0
    for row in rows:
        ok, text, reasons = process(row)
        if ok:
            if text != row["conversation"]:
                row["conversation"] = text
                repaired += 1
            kept.append(row)
        else:
            row["_reasons"] = reasons
            rejected.append(row)

    print("REPAIRS")
    for k in ("aggregates", "envelope", "pipes", "counts", "swap_shape"):
        print(f"  {k:12s} {stats[k]}")
    print(f"\n  conversations touched: {repaired}")

    print(f"\nREJECTED ({len(rejected)})")
    for r in rejected:
        print(f"  [{r['category']}] {r['scenario'][:55]}")
        for reason in r["_reasons"]:
            print(f"      - {reason}")

    if not args.apply:
        print("\nDry run. Rerun with --apply to write changes.")
        return

    shutil.copy(PATH, PATH.with_suffix(".jsonl.bak"))
    PATH.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
        encoding="utf-8",
    )
    if rejected:
        REJECTS.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rejected) + "\n",
            encoding="utf-8",
        )
    print(f"\nWrote {len(kept)} to {PATH.name} (backup at {PATH.name}.bak)")
    print(f"Wrote {len(rejected)} to {REJECTS.name}")
    print("Rerun phase B to regenerate the rejected scenarios.")


if __name__ == "__main__":
    main()