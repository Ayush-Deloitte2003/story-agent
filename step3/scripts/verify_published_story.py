import argparse
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("approved_draft", type=Path)
parser.add_argument("published_markdown", type=Path)
args = parser.parse_args()

expected = json.loads(args.approved_draft.read_text(encoding="utf-8"))
actual = args.published_markdown.read_text(encoding="utf-8")
if actual != f"# {expected['title']}\n\n{expected['story']}\n":
    print("Published Markdown does not match the human-approved draft.", file=sys.stderr)
    raise SystemExit(1)

print("Published Markdown exactly matches the human-approved draft.")
