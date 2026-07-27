"""Check curated_data.md for structural and numeric errors.

    python scripts/validate_seeds.py
"""

import json
import re
from collections import Counter
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "curated_data.md"
MARKERS = ("CATEGORY", "USER", "ASSISTANT", "TOOL")

text = PATH.read_text(encoding="utf-8")
blocks = re.split(r"^## Conversation ", text, flags=re.MULTILINE)[1:]
print(f"parsed {len(blocks)} conversations\n")

problems = 0

# --- collapsed markers, file-wide ---------------------------------------
collapsed = re.findall(
    rf"^(=== (?:{'|'.join(MARKERS)}) ===)[ \t]+(\S.*)$", text, re.MULTILINE
)
if collapsed:
    problems += len(collapsed)
    print(f"COLLAPSED MARKERS ({len(collapsed)}):")
    for marker, tail in collapsed:
        print(f"  {marker} {tail[:70]}")
    print()

# --- tool call formatting consistency -----------------------------------
inline_tc = len(re.findall(r"<tool_call>[ \t]+\S", text))
block_tc = len(re.findall(r"<tool_call>\s*\n", text))
print(f"tool_call style: {block_tc} on own line, {inline_tc} inline")
if inline_tc and block_tc:
    print("  (mixed - seeds should use one style so generated output is consistent)\n")

# --- per conversation ----------------------------------------------------
cats = []
for block in blocks:
    title = block.splitlines()[0].strip()

    m = re.search(r"=== CATEGORY ===[ \t]*\n?[ \t]*(.+)", block)
    cat = m.group(1).strip() if m else None
    cats.append(cat or "<<MISSING>>")
    if not cat:
        problems += 1
        print(f"NO CATEGORY: {title}")

    body = block[block.index("=== CATEGORY ==="):]
    n_calls = len(re.findall(r"<tool_call>", body))
    n_results = len(re.findall(r"^=== TOOL ===", body, re.MULTILINE))
    if n_calls != n_results:
        problems += 1
        print(f"TOOL MISMATCH: {title} - {n_calls} calls, {n_results} results")

# --- aggregate arithmetic ------------------------------------------------
SET_RE = re.compile(r"^(\d+)\s*x\s*([\d.]+)$")
for obj in re.finditer(r'\{"date":.*?"volume":\s*[\d.]+\}', text):
    try:
        d = json.loads(obj.group(0))
    except json.JSONDecodeError:
        continue
    parsed = [SET_RE.match(s.strip()) for s in d.get("sets", [])]
    if not all(parsed):
        continue
    reps = sum(int(p.group(1)) for p in parsed)
    top = max(float(p.group(2)) for p in parsed)
    vol = round(sum(int(p.group(1)) * float(p.group(2)) for p in parsed), 1)
    for key, want, got in (
        ("total_reps", reps, d.get("total_reps")),
        ("top_weight", top, d.get("top_weight")),
        ("volume", vol, d.get("volume")),
    ):
        if got != want:
            problems += 1
            print(f"AGGREGATE: {d['date']} {key} is {got}, should be {want}")

# --- summary -------------------------------------------------------------
print("\ncategories:")
for c, n in Counter(cats).most_common():
    print(f"  {n}x  {c}")

print(f"\n{'OK - no problems found' if not problems else f'{problems} problem(s) found'}")