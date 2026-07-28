"""Validate and repair generated conversations.

Recovers as much as possible rather than rejecting, because regenerating costs
money. Only conversations that cannot be made structurally valid are dropped.

    python scripts/validate_conversations.py            # report only
    python scripts/validate_conversations.py --apply    # rewrite files
    python scripts/validate_conversations.py --show 3   # print 3 rejected in full
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
NAME_KEYS = {"name", "exercise_name", "replacement_name", "template_name",
             "from", "to", "exercise"}
LIST_KEYS = ("sessions", "workouts", "exercises", "templates")

BLOCK_RE = re.compile(r"^=== (USER|ASSISTANT|TOOL) ===[ \t]*$", re.MULTILINE)
TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
SET_RE = re.compile(r"^\s*(\d+)\s*x\s*([\d.]+)\s*$")

# Deliberately narrow. An earlier version matched \bNone\b and rejected ordinary
# English ("None of this matters much").
LEAK_RE = re.compile(r"\bif false else\b|Something went wrong parsing|"
                     r"Let me try again\.\s*$")

stats = Counter()
recovered = Counter()


# ---------------------------------------------------------------------------
# Lenient JSON
# ---------------------------------------------------------------------------

def loads_lenient(raw):
    """Parse JSON, repairing the mistakes generators actually make."""
    raw = raw.strip()
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        pass

    fixed = raw
    fixed = re.sub(r",(\s*[}\]])", r"\1", fixed)              # trailing commas
    fixed = re.sub(r"\bTrue\b", "true", fixed)                # python literals
    fixed = re.sub(r"\bFalse\b", "false", fixed)
    fixed = re.sub(r"\bNone\b", "null", fixed)
    fixed = fixed.replace("\u200b", "")                       # zero width space
    fixed = re.sub(r"</?br\s*/?>", "", fixed)                 # stray html
    try:
        return json.loads(fixed), True
    except json.JSONDecodeError:
        pass

    # Unbalanced brackets - usually a truncated result. Close what is open.
    opens = fixed.count("{") - fixed.count("}")
    closes = fixed.count("[") - fixed.count("]")
    if 0 < opens + closes <= 4:
        candidate = fixed + ("]" * max(closes, 0)) + ("}" * max(opens, 0))
        try:
            return json.loads(candidate), True
        except json.JSONDecodeError:
            pass
    return None, False


# ---------------------------------------------------------------------------
# Parsing - keeps any text that appears before the first marker
# ---------------------------------------------------------------------------

def parse_blocks(text, scenario):
    """Split into [(role, content)], inferring missing leading headers."""
    parts = BLOCK_RE.split(text)
    prefix = parts[0].strip()
    blocks = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]

    if not blocks:
        return []

    # Content before the first header is an unlabelled turn. If it contains a
    # tool call it belongs to the assistant, otherwise it is the user message.
    if prefix:
        role = "ASSISTANT" if "<tool_call>" in prefix else "USER"
        blocks.insert(0, (role, prefix))
        recovered["restored leading turn"] += 1

    # The scenario IS the opening user message, so a missing one is free to add.
    if blocks[0][0] != "USER":
        blocks.insert(0, ("USER", scenario))
        recovered["prepended USER turn"] += 1

    return blocks


def rebuild(blocks):
    return "\n\n".join(f"=== {r} ===\n{c}" for r, c in blocks)


def trim_to_complete(blocks):
    """Drop trailing turns until the conversation ends on assistant prose.

    A conversation ending on a TOOL block, or on an assistant turn whose tool
    call was never answered, teaches the model to state results it never saw.
    """
    dropped = 0
    while blocks:
        role, content = blocks[-1]
        if role == "ASSISTANT":
            prose = TOOLCALL_RE.sub("", content).strip()
            n_calls = len(TOOLCALL_RE.findall(content))
            # Count TOOL blocks that follow this assistant turn - none, since
            # it is last. So any call here is unanswered.
            if n_calls == 0 and prose:
                break
            if prose and n_calls:
                blocks[-1] = (role, prose)      # keep the prose, drop the call
                dropped += 1
                break
        blocks.pop()
        dropped += 1
    if dropped:
        recovered["trimmed incomplete tail"] += 1
    return blocks


# ---------------------------------------------------------------------------
# Content repairs
# ---------------------------------------------------------------------------

def fix_aggregates(obj):
    n = 0
    if isinstance(obj, dict):
        sets = obj.get("sets")
        if isinstance(sets, list) and sets and all(isinstance(s, str) for s in sets):
            m = [SET_RE.match(s) for s in sets]
            if all(m):
                reps = sum(int(x.group(1)) for x in m)
                top = max(float(x.group(2)) for x in m)
                vol = round(sum(int(x.group(1)) * float(x.group(2)) for x in m), 1)
                for k, want in (("total_reps", reps), ("top_weight", top), ("volume", vol)):
                    if k in obj and obj[k] != want:
                        obj[k] = want
                        n += 1
        for v in obj.values():
            n += fix_aggregates(v)
    elif isinstance(obj, list):
        for v in obj:
            n += fix_aggregates(v)
    return n


def fix_pipes(obj):
    n = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
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
    n = 0
    if isinstance(obj, dict):
        for key in LIST_KEYS:
            items = obj.get(key)
            if not isinstance(items, list):
                continue
            if key == "sessions" and all(isinstance(i, dict) for i in items):
                seen, unique = set(), []
                for it in items:
                    d = it.get("date")
                    if d and d in seen:
                        n += 1
                        continue
                    if d:
                        seen.add(d)
                    unique.append(it)
                if len(unique) != len(items):
                    items = unique
                    obj[key] = items
                if all("date" in i for i in items):
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
    if (isinstance(obj, dict) and obj.get("action") == "swap_exercise"
            and "replacement_name" in obj):
        obj["from"] = obj.pop("exercise_name", obj.get("from"))
        obj["to"] = obj.pop("replacement_name")
        return 1
    return 0


def fix_envelope(result, call):
    if not isinstance(result, dict) or "error" in result:
        return 0
    if not call or call.get("name") != "read_user_data":
        return 0
    if all(k in READ_SCOPES for k in result):
        return 0
    scopes = call.get("arguments", {}).get("scope") or []
    if isinstance(scopes, str):
        scopes = [scopes]
    if len(scopes) != 1:
        return 0
    inner = dict(result)
    result.clear()
    result[scopes[0]] = inner
    return 1


# ---------------------------------------------------------------------------
# Per conversation
# ---------------------------------------------------------------------------

def process(row):
    text = row["conversation"]
    scenario = row["scenario"]

    if LEAK_RE.search(text):
        return False, None, ["leaked generator narration"]

    blocks = parse_blocks(text, scenario)
    if not blocks:
        return False, None, ["no === blocks parsed"]

    blocks = trim_to_complete(blocks)
    if len(blocks) < 2:
        return False, None, ["nothing left after trimming incomplete tail"]

    # Pair calls with TOOL blocks by walking in order.
    calls, tool_idx, pending = [], [], []
    for i, (role, content) in enumerate(blocks):
        if role == "ASSISTANT":
            for raw in TOOLCALL_RE.findall(content):
                obj, _ = loads_lenient(raw)
                if obj is None:
                    return False, None, ["tool_call is not valid JSON"]
                pending.append(obj)
        elif role == "TOOL":
            tool_idx.append(i)
            calls.append(pending.pop(0) if pending else None)

    if pending:
        return False, None, [f"{len(pending)} tool call(s) with no result"]

    fixes = 0
    for n, idx in enumerate(tool_idx):
        result, repaired_json = loads_lenient(blocks[idx][1])
        if result is None:
            return False, None, ["TOOL block is not valid JSON"]
        if repaired_json:
            stats["json repaired"] += 1
            fixes += 1

        f = fix_envelope(result, calls[n]);  stats["envelope"] += f
        a = fix_aggregates(result);          stats["aggregates"] += a
        p = fix_pipes(result);               stats["pipes"] += p
        c = fix_counts(result);              stats["counts"] += c
        s = fix_swap_shape(result);          stats["swap_shape"] += s
        total = f + a + p + c + s
        if total or repaired_json:
            blocks[idx] = ("TOOL", json.dumps(result, ensure_ascii=False))
            fixes += total

    # Pipes can also appear in the arguments of the calls themselves.
    for i, (role, content) in enumerate(blocks):
        if role != "ASSISTANT" or "<tool_call>" not in content:
            continue

        def _repair(m):
            nonlocal fixes
            obj, _ = loads_lenient(m.group(1))
            if obj is None:
                return m.group(0)
            p = fix_pipes(obj)
            if p:
                fixes += p
                stats["pipes"] += p
            return f"<tool_call>\n{json.dumps(obj, ensure_ascii=False)}\n</tool_call>"

        blocks[i] = (role, TOOLCALL_RE.sub(_repair, content))

    return True, rebuild(blocks), []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", type=int, default=0, help="print N rejected in full")
    args = ap.parse_args()

    rows = [json.loads(l) for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"{len(rows)} conversations\n")

    kept, rejected, changed = [], [], 0
    for row in rows:
        original = row["conversation"]
        ok, text, reasons = process(row)
        if ok:
            if text != original:
                row["conversation"] = text
                changed += 1
            kept.append(row)
        else:
            row["_reasons"] = reasons
            rejected.append(row)

    print("STRUCTURAL RECOVERY")
    for k, v in recovered.most_common():
        print(f"  {k:28s} {v}")
    print("\nCONTENT REPAIRS")
    for k in ("json repaired", "aggregates", "envelope", "pipes", "counts", "swap_shape"):
        print(f"  {k:28s} {stats[k]}")

    print(f"\nkept {len(kept)}, rejected {len(rejected)}, modified {changed}")

    if rejected:
        print("\nREJECTED BY REASON")
        for reason, n in Counter(r["_reasons"][0] for r in rejected).most_common():
            print(f"  {n:4d}  {reason}")

    for r in rejected[:args.show]:
        print("\n" + "=" * 70)
        print(f"[{r['category']}] {r['scenario']}")
        print(f"reasons: {r['_reasons']}")
        print("=" * 70)
        print(r["conversation"][:2000])

    if not args.apply:
        print("\nDry run. Rerun with --apply to write changes.")
        return

    shutil.copy(PATH, PATH.with_suffix(".jsonl.bak"))
    PATH.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
                    encoding="utf-8")
    if rejected:
        REJECTS.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rejected) + "\n",
                           encoding="utf-8")
    print(f"\nWrote {len(kept)} to {PATH.name} (backup at {PATH.name}.bak)")


if __name__ == "__main__":
    main()