import re
from pathlib import Path

text = Path("data/raw/curated_data.md").read_text(encoding="utf-8")
blocks = re.split(r"^## Conversation ", text, flags=re.MULTILINE)[1:]

rows = []
for b in blocks:
    title = b.splitlines()[0].strip()
    for turn in re.split(r"^=== ASSISTANT ===$", b, flags=re.MULTILINE)[1:]:
        prose = re.split(r"^=== (USER|TOOL) ===$", turn, flags=re.MULTILINE)[0]
        prose = re.sub(r"<tool_call>.*?</tool_call>", "", prose, flags=re.S).strip()
        if prose:
            rows.append((len(prose.split()), title))

rows.sort(reverse=True)
print(f"{len(rows)} assistant turns")
print(f"median {sorted(w for w, _ in rows)[len(rows)//2]} words\n")
print("longest:")
for w, t in rows[:25]:
    print(f"  {w:4d}  {t[:60]}")
lengths = sorted(w for w, _ in rows)
print(f"median {lengths[len(lengths)//2]} | p25 {lengths[len(lengths)//4]} | p75 {lengths[3*len(lengths)//4]}")