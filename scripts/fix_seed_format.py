import re
from pathlib import Path

PATH = Path("data/raw/curated_data.md")
text = PATH.read_text(encoding="utf-8")

# Split any remaining "=== MARKER === content" onto two lines
text, n1 = re.subn(
    r"^(=== (?:CATEGORY|USER|ASSISTANT|TOOL) ===)[ \t]+(\S.*)$",
    lambda m: f"{m.group(1)}\n{m.group(2)}",
    text,
    flags=re.MULTILINE,
)

# Put single-line tool calls onto their own lines
text, n2 = re.subn(
    r"<tool_call>[ \t]+(.+?)[ \t]*</tool_call>",
    lambda m: f"<tool_call>\n{m.group(1)}\n</tool_call>",
    text,
)

PATH.write_text(text, encoding="utf-8")
print(f"split {n1} markers, normalized {n2} tool calls")