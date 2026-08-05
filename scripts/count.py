import json, re
from pathlib import Path

VALID = {"readUserData", "readAllTemplates", "getExerciseStats", "findExercises",
         "createTemplate", "addExercise", "removeExercise"}
TOOLCALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)

P = Path("data/processed/conversations.jsonl")
rows = [json.loads(l) for l in P.read_text(encoding="utf-8").splitlines() if l.strip()]

def ok(r):
    for raw in TOOLCALL_RE.findall(r["conversation"]):
        try:
            if json.loads(raw).get("name") not in VALID:
                return False
        except json.JSONDecodeError:
            return False
    return True

kept = [r for r in rows if ok(r)]
P.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n", encoding="utf-8")
print(f"kept {len(kept)}, dropped {len(rows) - len(kept)}")