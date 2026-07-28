"""Convert exercise_data.dart into a compact catalog for the generator prompt."""

import json
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "data" / "exercise_data.dart"
OUT = Path(__file__).resolve().parent.parent / "configs" / "exercise_catalog.txt"

text = SRC.read_text(encoding="utf-8")
body = text[text.index("["):text.rindex("]") + 1]
body = re.sub(r"//[^\n]*", "", body)          # strip // comments
body = re.sub(r",(\s*[}\]])", r"\1", body)    # strip trailing commas
data = json.loads(body)

lines = []
for e in data:
    primary = ",".join(e.get("primaryMuscles", []))
    secondary = ",".join(e.get("secondaryMuscles", []))
    lines.append(
        f"{e['name']}|{e['equipment']}|{e['bodyPart']}|{primary}|{secondary}"
    )

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {len(lines)} exercises to {OUT}")