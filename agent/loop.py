"""Hand-rolled OpenAI function-calling loop -- no agent framework.

Temperature 0, so tool selection is reproducible rather than creative. Each
turn returns the model's text plus every tool call made, so a caller can
show the calls beneath the reply: that is what makes "the model never
produces a number" checkable rather than merely asserted.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from agent.system_prompt import SYSTEM_PROMPT
from tools.registry import TOOL_DISPATCH
from tools.schemas import TOOL_SCHEMAS

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
TEMPERATURE = 0
MAX_TOOL_ITERATIONS = 8

# Bounds one model call, not the whole turn: a turn is up to
# MAX_TOOL_ITERATIONS of these. Without it the SDK waits indefinitely and the
# UI has nothing to time out against.
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 2

TURN_LIMIT_REPLY = "(stopped after too many tool calls without a final answer -- try rephrasing)"
EMPTY_REPLY_FALLBACK = "(the model returned an empty reply -- try rephrasing the question)"

# One client per process, not per request: a fresh OpenAI() builds a new
# connection pool every time. Lazy so importing this module doesn't require
# OPENAI_API_KEY to be set (tests inject their own client).
_shared_client: OpenAI | None = None


def _default_client() -> OpenAI:
    global _shared_client
    if _shared_client is None:
        _shared_client = OpenAI(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
    return _shared_client


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
    model can narrate, rather than killing the conversation. The exception is
    logged, so a broken tool surfaces as an alert and not just as an odd answer."""
    func = TOOL_DISPATCH.get(name)
    if func is None:
        logger.warning("unknown tool requested: %s", name)
        return {"error": f"unknown tool: {name}"}

    started = time.perf_counter()
    try:
        result = func(**arguments)
    except Exception as exc:
        logger.exception(
            "tool %s(%s) raised after %.0fms",
            name,
            json.dumps(arguments, default=str),
            (time.perf_counter() - started) * 1000,
        )
        return {"error": f"{exc.__class__.__name__}: {exc}"}

    logger.info(
        "tool %s(%s) ok in %.0fms",
        name,
        json.dumps(arguments, default=str),
        (time.perf_counter() - started) * 1000,
    )
    return result


class Agent:
    """One conversation: message history plus the OpenAI client. Call .ask()
    per user turn; history persists so follow-ups keep their context."""

    def __init__(self, model: str = DEFAULT_MODEL, client: OpenAI | None = None):
        self.model = model
        self.client = client or _default_client()
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def ask(self, user_message: str) -> AgentTurnResult:
        self.messages.append({"role": "user", "content": user_message})
        tool_calls_made: list[ToolCallRecord] = []
        # Set by an ambiguous/matchless resolve_airports: the next request
        # goes out without tools, so the model can only ask the user.
        force_text_only = False

        for iteration in range(MAX_TOOL_ITERATIONS):
            request_kwargs: dict[str, Any] = dict(
                model=self.model,
                temperature=TEMPERATURE,
                messages=self.messages,
            )
            if not force_text_only:
                request_kwargs["tools"] = TOOL_SCHEMAS
                request_kwargs["tool_choice"] = "auto"

            logger.debug(
                "model call %d/%d (tools=%s, messages=%d)",
                iteration + 1,
                MAX_TOOL_ITERATIONS,
                not force_text_only,
                len(self.messages),
            )
            response = self.client.chat.completions.create(**request_kwargs)
            message = response.choices[0].message
            self.messages.append(message.model_dump(exclude_none=True))

            if not message.tool_calls:
                reply = message.content or EMPTY_REPLY_FALLBACK
                # A null-content final message dumps to a message with no
                # `content` key at all, which is not a valid message to send
                # back. Backfill it so the history stays replayable.
                self.messages[-1]["content"] = reply
                logger.info("turn finished after %d tool call(s)", len(tool_calls_made))
                return AgentTurnResult(reply=reply, tool_calls=tool_calls_made)

            for call in message.tool_calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    logger.warning("tool %s got unparseable arguments: %r", name, call.function.arguments)
                    arguments = {}
                result = _execute_tool(name, arguments)
                tool_calls_made.append(ToolCallRecord(name=name, arguments=arguments, result=result))
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                })
                if name == "resolve_airports" and _needs_clarification(result):
                    logger.info("resolve_airports needs clarification -- next call goes out without tools")
                    force_text_only = True

        logger.warning("hit the %d-iteration cap with %d tool call(s)", MAX_TOOL_ITERATIONS, len(tool_calls_made))
        # Recorded in history too, so a persistent Agent's next turn doesn't
        # see a transcript ending in tool results with no assistant reply.
        self.messages.append({"role": "assistant", "content": TURN_LIMIT_REPLY})
        return AgentTurnResult(reply=TURN_LIMIT_REPLY, tool_calls=tool_calls_made)
