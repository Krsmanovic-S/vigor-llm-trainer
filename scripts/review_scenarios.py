"""Sample scenarios for manual review and flag mode collapse.

    python scripts/review_scenarios.py
    python scripts/review_scenarios.py --per-category 30 --category Nutrition
"""

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "scenarios.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-category", type=int, default=15)
    ap.add_argument("--category", default=None, help="Only this category")
    ap.add_argument("--stats-only", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(l) for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r["scenario"])

    print(f"{len(rows)} scenarios across {len(by_cat)} categories\n")

    for cat in sorted(by_cat):
        if args.category and cat != args.category:
            continue
        items = by_cat[cat]

        # Mode collapse signals
        openers = Counter(" ".join(s.lower().split()[:2]) for s in items)
        lengths = sorted(len(s.split()) for s in items)
        top = openers.most_common(3)
        top_share = sum(c for _, c in top) / len(items) * 100
        questions = sum(1 for s in items if s.strip().endswith("?")) / len(items) * 100

        print("=" * 70)
        print(f"{cat}  ({len(items)} scenarios)")
        print("=" * 70)
        print(f"  words: min {lengths[0]}, median {lengths[len(lengths)//2]}, max {lengths[-1]}")
        print(f"  ends with '?': {questions:.0f}%")
        print(f"  top openers ({top_share:.0f}% of all): " +
              ", ".join(f'"{o}" x{c}' for o, c in top))

        if top_share > 25:
            print("  !! openers are clustering - likely mode collapse")
        if lengths[-1] - lengths[0] < 8:
            print("  !! very narrow length range")

        if not args.stats_only:
            print()
            for s in random.sample(items, min(args.per_category, len(items))):
                print(f"  - {s}")
        print()


if __name__ == "__main__":
    main()