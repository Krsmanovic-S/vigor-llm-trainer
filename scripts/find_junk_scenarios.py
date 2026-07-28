"""Find scenarios that are too short to be real user messages."""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "scenarios.jsonl"

rows = [json.loads(l) for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
short = [r for r in rows if len(r["scenario"].split()) <= 2]

print(f"{len(short)} of {len(rows)} scenarios are 2 words or fewer\n")
for r in sorted(short, key=lambda r: r["category"]):
    print(f"  [{r['category']}] {r['scenario']!r}")