import json, re
from pathlib import Path

P = Path("data/processed/conversations.jsonl")
rows = [json.loads(l) for l in P.read_text(encoding="utf-8").splitlines() if l.strip()]
kept = [r for r in rows
        if not ("=== TOOL ===" not in r["conversation"]
                and re.search(r"\b(82\.5|2840|2,840|14\.5|104\.0|38\.4)\b", r["conversation"]))]
P.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n", encoding="utf-8")
print(f"kept {len(kept)}, dropped {len(rows) - len(kept)}")