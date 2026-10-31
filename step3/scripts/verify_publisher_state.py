import argparse
import hashlib
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("approved_draft", type=Path)
parser.add_argument("state_file", type=Path)
parser.add_argument("--previous-state", type=Path)
parser.add_argument("--published-markdown", type=Path)
args = parser.parse_args()

approved = json.loads(args.approved_draft.read_text(encoding="utf-8"))
state = json.loads(args.state_file.read_text(encoding="utf-8"))
key = hashlib.sha256(f"{approved['title']}\n{approved['story']}".encode()).hexdigest()
attempt = state.get("attempts", {}).get(key)
if not isinstance(attempt, int) or attempt < 1:
    print("Publisher attempt state for the approved draft is missing.", file=sys.stderr)
    raise SystemExit(1)

if args.previous_state:
    previous = json.loads(args.previous_state.read_text(encoding="utf-8"))
    previous_attempt = previous.get("attempts", {}).get(key)
    if not isinstance(previous_attempt, int) or attempt < previous_attempt:
        print("Publisher attempt state regressed across replacement.", file=sys.stderr)
        raise SystemExit(1)

if args.published_markdown:
    if key not in state.get("published", {}):
        print("Publisher has no durable result record for the approved draft.", file=sys.stderr)
        raise SystemExit(1)
    markdown = args.published_markdown.read_text(encoding="utf-8")
    if markdown != f"# {approved['title']}\n\n{approved['story']}\n":
        print("Published Markdown does not match the human-approved draft.", file=sys.stderr)
        raise SystemExit(1)

print(f"Publisher state verified for the approved draft (attempt {attempt}).")
