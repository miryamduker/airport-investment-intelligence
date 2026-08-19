"""Terminal chat. Prints the reply and, above it, every tool call behind it
-- name, arguments, full JSON result -- so a transcript is self-verifying.

Interactive:
    python -m cli
    python -m cli --model gpt-4o

Non-interactive (one user turn per stdin line, exits at EOF):
    python -m cli < questions.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

from agent.loop import Agent, AgentTurnResult, DEFAULT_MODEL, ToolCallRecord

load_dotenv()


def print_tool_call(record: ToolCallRecord, index: int) -> None:
    print(f"  [{index}] {record.name}({json.dumps(record.arguments)})")
    for line in json.dumps(record.result, default=str, indent=2).splitlines():
        print(f"      {line}")


def print_turn(result: AgentTurnResult) -> None:
    if result.tool_calls:
        print("\ntool calls:")
        for i, record in enumerate(result.tool_calls, start=1):
            print_tool_call(record, i)
    print("\nassistant:")
    print(result.reply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model name (default: %(default)s, or $OPENAI_MODEL).")
    args = parser.parse_args()

    if "OPENAI_API_KEY" not in os.environ:
        print("OPENAI_API_KEY is not set -- export it before running cli.py.", file=sys.stderr)
        sys.exit(1)

    agent = Agent(model=args.model)
    interactive = sys.stdin.isatty()
    if interactive:
        print(f"Airport Investment Intelligence Agent (model={args.model}). Type 'exit' to quit.\n")

    while True:
        try:
            if interactive:
                user_message = input("you: ").strip()
            else:
                line = sys.stdin.readline()
                if not line:
                    break
                user_message = line.strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_message:
            continue
        if user_message.lower() in {"exit", "quit"}:
            break

        if not interactive:
            print(f"you: {user_message}")
        result = agent.ask(user_message)
        print_turn(result)
        print()


if __name__ == "__main__":
    main()
