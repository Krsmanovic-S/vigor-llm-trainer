"""Remove conversations for one or more categories so phase B regenerates them.

Phase B resumes by scenario text, so deleting the rows here is all that is
needed - rerunning generation picks up exactly those scenarios again.

    python scripts/drop_category.py "Plan Creation"
    python scripts/drop_category.py "Plan Creation" "Safety"
    python scripts/drop_category.py "Plan Creation" --apply
    python scripts/drop_category.py "Template Modification" "Progress Analysis" --apply
    
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "data" / "processed" / "conversations.jsonl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("categories", nargs="+", help="category names to remove")
    ap.add_argument("--apply", action="store_true", help="write the change")
    args = ap.parse_args()

    if not PATH.exists():
        sys.exit(f"Not found: {PATH}")

    rows = [json.loads(l) for l in PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    counts = Counter(r["category"] for r in rows)

    print(f"{len(rows)} conversations\n")
    for cat in sorted(counts):
        mark = "  <- removing" if cat in args.categories else ""
        print(f"  {cat:24s} {counts[cat]:4d}{mark}")

    unknown = [c for c in args.categories if c not in counts]
    if unknown:
        print(f"\nWARNING: no conversations found for {unknown}")

    kept = [r for r in rows if r["category"] not in args.categories]
    removed = len(rows) - len(kept)
    print(f"\nwould keep {len(kept)}, remove {removed}")

    if not args.apply:
        print("\nDry run. Rerun with --apply to write.")
        return

    shutil.copy(PATH, PATH.with_suffix(".jsonl.bak"))
    with open(PATH, "w", encoding="utf-8") as f:
        for r in kept:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nwrote {len(kept)} rows (backup at {PATH.name}.bak)")
    print("Rerun phase B to regenerate the removed scenarios.")


if __name__ == "__main__":
    main()