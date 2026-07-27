import json, re
from pathlib import Path

PATH = Path("data/processed/scenarios.jsonl")

# Things the app has no concept of. Word-boundary matched to avoid false hits.
BAD = re.compile(r"""
    rest\s?day | training\s?week | \bschedule\b | reschedul | move\s+my\s+\w+\s+to |
    swim | \bjog | run(ning)?\s+(outside|outdoors|a\s+5k) | \b5k\b | marathon |
    step\s?count | \bsteps\b | sleep\s?track | smartwatch | apple\s?watch | fitbit | garmin |
    log(ged|ging)?\s+(my\s+)?(food|meal|breakfast|lunch|dinner) | meal\s?plan |
    calorie\s?(log|track) | macro\s?track | food\s?diary | \bmyfitnesspal\b |
    heart\s?rate | \bhrv\b | progress\s?pic | share\s+my
""", re.IGNORECASE | re.VERBOSE)

rows = [json.loads(l) for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip()]

# Capability boundary is supposed to contain these - leave it alone.
kept, dropped = [], []
for r in rows:
    if r["category"] != "Capability boundary" and BAD.search(r["scenario"]):
        dropped.append(r)
    else:
        kept.append(r)

PATH.write_text(
    "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n",
    encoding="utf-8",
)

print(f"kept {len(kept)}, dropped {len(dropped)}")
from collections import Counter
for cat, n in Counter(r["category"] for r in dropped).most_common():
    print(f"  {cat}: -{n}")
print("\nsample of dropped:")
for r in dropped[:15]:
    print(f"  [{r['category']}] {r['scenario']}")