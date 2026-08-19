"""Hand-rolled OpenAI function-calling loop -- no agent framework.

Temperature 0, so tool selection is reproducible rather than creative. Each
turn returns the model's text plus every tool call made, so a caller can
show the calls beneath the reply: that is what makes "the model never
produces a number" checkable rather than merely asserted.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from agent.system_prompt import SYSTEM_PROMPT
from tools.registry import TOOL_DISPATCH
from tools.schemas import TOOL_SCHEMAS

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
TEMPERATURE = 0
MAX_TOOL_ITERATIONS = 8


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    result: Any


@dataclass
class AgentTurnResult:
    reply: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)


def _needs_clarification(result: Any) -> bool:
    """True when resolve_airports matched several airports, or none.

    Enforced in code rather than left to the system prompt: the model was
    observed proceeding with a guessed code after both outcomes.
    """
    if not isinstance(result, dict):
        return False
    return bool(result.get("ambiguous")) or not result.get("matches")


def _execute_tool(name: str, arguments: dict) -> dict:
    """Never raises: a tool exception becomes an {"error": ...} result the
    model can narrate, rather than killing the conversation."""
    func = TOOL_DISPATCH.get(name)
    if func is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return func(**arguments)
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"}


class Agent:
    """One conversation: message history plus the OpenAI client. Call .ask()
    per user turn; history persists so follow-ups keep their context."""

    def __init__(self, model: str = DEFAULT_MODEL, client: OpenAI | None = None):
        self.model = model
        self.client = client or OpenAI()
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def ask(self, user_message: str) -> AgentTurnResult:
        self.messages.append({"role": "user", "content": user_message})
        tool_calls_made: list[ToolCallRecord] = []
        # Set by an ambiguous/matchless resolve_airports: the next request
        # goes out without tools, so the model can only ask the user.
        force_text_only = False

        for _ in range(MAX_TOOL_ITERATIONS):
            request_kwargs: dict[str, Any] = dict(
                model=self.model,
                temperature=TEMPERATURE,
                messages=self.messages,
            )
            if not force_text_only:
                request_kwargs["tools"] = TOOL_SCHEMAS
                request_kwargs["tool_choice"] = "auto"

            response = self.client.chat.completions.create(**request_kwargs)
            message = response.choices[0].message
            self.messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                return AgentTurnResult(reply=message.content or "", tool_calls=tool_calls_made)

            for call in message.tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = _execute_tool(name, arguments)
                tool_calls_made.append(ToolCallRecord(name=name, arguments=arguments, result=result))
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                })
                if name == "resolve_airports" and _needs_clarification(result):
                    force_text_only = True

        return AgentTurnResult(
            reply="(stopped after too many tool calls without a final answer -- try rephrasing)",
            tool_calls=tool_calls_made,
        )
