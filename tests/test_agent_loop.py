"""The agent loop's own behaviour, with a fake OpenAI client.

No API key, no network, no cost: every test drives Agent.ask() against a
scripted sequence of model responses and asserts on what the loop did with
them. What is worth pinning down here is the logic the loop adds on top of
the model -- the clarification gate especially, which exists precisely
because the prompt alone did not hold.

Run:
    python -m pytest tests/test_agent_loop.py -v
"""
from __future__ import annotations

import json

import pytest
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
    Function,
)

from agent.loop import (
    EMPTY_REPLY_FALLBACK,
    MAX_TOOL_ITERATIONS,
    TURN_LIMIT_REPLY,
    Agent,
    _execute_tool,
    _needs_clarification,
)
from tools.registry import TOOL_DISPATCH
from tools.schemas import TOOL_SCHEMAS


# --------------------------------------------------------------------------
# fake client
# --------------------------------------------------------------------------

def text_message(content: str) -> ChatCompletionMessage:
    return ChatCompletionMessage(role="assistant", content=content)


def tool_call_message(name: str, arguments: dict, call_id: str = "call_1") -> ChatCompletionMessage:
    return ChatCompletionMessage(
        role="assistant",
        content=None,
        tool_calls=[
            ChatCompletionMessageFunctionToolCall(
                id=call_id,
                type="function",
                function=Function(name=name, arguments=json.dumps(arguments)),
            )
        ],
    )


class FakeCompletions:
    def __init__(self, scripted: list[ChatCompletionMessage]):
        self.scripted = list(scripted)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        # Repeat the last scripted message once exhausted, so a test can drive
        # the loop to its iteration cap without scripting every step.
        message = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]

        class Choice:
            def __init__(self, msg):
                self.message = msg

        class Response:
            def __init__(self, msg):
                self.choices = [Choice(msg)]

        return Response(message)


class FakeClient:
    def __init__(self, scripted: list[ChatCompletionMessage]):
        self.completions = FakeCompletions(scripted)

        class Chat:
            def __init__(self, completions):
                self.completions = completions

        self.chat = Chat(self.completions)

    @property
    def calls(self) -> list[dict]:
        return self.completions.calls


def agent_with(scripted: list[ChatCompletionMessage]) -> Agent:
    return Agent(model="fake-model", client=FakeClient(scripted))


# --------------------------------------------------------------------------
# registry / schema parity
# --------------------------------------------------------------------------

def test_dispatch_table_and_schemas_declare_the_same_tools():
    """registry.py says the two 'must stay in step'. This is what enforces it."""
    schema_names = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert schema_names == set(TOOL_DISPATCH)


# --------------------------------------------------------------------------
# the clarification gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "result,expected",
    [
        ({"matches": [{"code": "BOS"}], "ambiguous": False}, False),
        ({"matches": [{"code": "LAX"}, {"code": "BUR"}], "ambiguous": True}, True),
        ({"matches": [], "ambiguous": False}, True),
        ({"error": "boom"}, True),
        ("not a dict", False),
    ],
)
def test_needs_clarification(result, expected):
    assert _needs_clarification(result) is expected


def test_ambiguous_resolve_forces_the_next_call_out_without_tools(monkeypatch):
    """The heart of it: after an ambiguous match the model is structurally
    unable to call another tool, so it can only come back and ask the user."""
    monkeypatch.setitem(
        TOOL_DISPATCH,
        "resolve_airports",
        lambda query: {"matches": [{"code": "LAX"}, {"code": "BUR"}], "ambiguous": True},
    )
    agent = agent_with([
        tool_call_message("resolve_airports", {"query": "LA"}),
        text_message("Did you mean LAX or BUR?"),
    ])

    result = agent.ask("How busy is LA?")

    assert result.reply == "Did you mean LAX or BUR?"
    first, second = agent.client.calls
    assert "tools" in first
    assert "tools" not in second, "the follow-up call must go out with no tools at all"
    assert "tool_choice" not in second


def test_unambiguous_resolve_leaves_tools_available(monkeypatch):
    monkeypatch.setitem(
        TOOL_DISPATCH,
        "resolve_airports",
        lambda query: {"matches": [{"code": "BOS"}], "ambiguous": False},
    )
    agent = agent_with([
        tool_call_message("resolve_airports", {"query": "Boston"}),
        text_message("BOS it is."),
    ])

    agent.ask("Tell me about Boston")

    assert all("tools" in call for call in agent.client.calls)


def test_no_matches_also_forces_text_only(monkeypatch):
    monkeypatch.setitem(TOOL_DISPATCH, "resolve_airports", lambda query: {"matches": [], "ambiguous": False})
    agent = agent_with([
        tool_call_message("resolve_airports", {"query": "Atlantis"}),
        text_message("I could not find that airport."),
    ])

    agent.ask("Tell me about Atlantis")

    assert "tools" not in agent.client.calls[1]


# --------------------------------------------------------------------------
# tool execution never kills the conversation
# --------------------------------------------------------------------------

def test_unknown_tool_becomes_an_error_result():
    assert _execute_tool("no_such_tool", {}) == {"error": "unknown tool: no_such_tool"}


def test_tool_exception_becomes_an_error_result(monkeypatch):
    def boom(code: str):
        raise ValueError("mart is missing")

    monkeypatch.setitem(TOOL_DISPATCH, "airport_profile", boom)
    assert _execute_tool("airport_profile", {"code": "BOS"}) == {"error": "ValueError: mart is missing"}


def test_bad_arguments_become_an_error_result(monkeypatch):
    monkeypatch.setitem(TOOL_DISPATCH, "airport_profile", lambda code: {"code": code})
    result = _execute_tool("airport_profile", {"not_a_parameter": 1})
    assert "TypeError" in result["error"]


def test_a_failing_tool_still_lets_the_turn_finish(monkeypatch):
    def boom(code: str):
        raise RuntimeError("no data")

    monkeypatch.setitem(TOOL_DISPATCH, "airport_profile", boom)
    agent = agent_with([
        tool_call_message("airport_profile", {"code": "BOS"}),
        text_message("That lookup failed."),
    ])

    result = agent.ask("Tell me about BOS")

    assert result.reply == "That lookup failed."
    assert result.tool_calls[0].result == {"error": "RuntimeError: no data"}


def test_unparseable_tool_arguments_fall_back_to_empty(monkeypatch):
    monkeypatch.setitem(TOOL_DISPATCH, "rank_airports", lambda **kwargs: {"kwargs": kwargs})
    broken = ChatCompletionMessage(
        role="assistant",
        content=None,
        tool_calls=[
            ChatCompletionMessageFunctionToolCall(
                id="call_1",
                type="function",
                function=Function(name="rank_airports", arguments="{not json"),
            )
        ],
    )
    agent = agent_with([broken, text_message("done")])

    result = agent.ask("top airports")

    assert result.tool_calls[0].arguments == {}


# --------------------------------------------------------------------------
# turn shape
# --------------------------------------------------------------------------

def test_plain_answer_makes_no_tool_calls():
    agent = agent_with([text_message("That's outside what I can help with.")])
    result = agent.ask("What's the capital of France?")
    assert result.tool_calls == []
    assert result.reply == "That's outside what I can help with."


def test_tool_calls_are_reported_verbatim(monkeypatch):
    monkeypatch.setitem(TOOL_DISPATCH, "airport_profile", lambda code: {"code": code, "score": 71.4})
    agent = agent_with([
        tool_call_message("airport_profile", {"code": "BOS"}),
        text_message("BOS scores 71.4."),
    ])

    result = agent.ask("Tell me about BOS")

    assert len(result.tool_calls) == 1
    record = result.tool_calls[0]
    assert record.name == "airport_profile"
    assert record.arguments == {"code": "BOS"}
    assert record.result == {"code": "BOS", "score": 71.4}


def test_iteration_cap_terminates_and_is_recorded_in_history(monkeypatch):
    """A model that never stops calling tools must not loop forever, and the
    history must not end on a tool result with no assistant reply."""
    monkeypatch.setitem(TOOL_DISPATCH, "airport_profile", lambda code: {"code": code})
    agent = agent_with([tool_call_message("airport_profile", {"code": "BOS"})])

    result = agent.ask("Tell me about BOS")

    assert result.reply == TURN_LIMIT_REPLY
    assert len(agent.client.calls) == MAX_TOOL_ITERATIONS
    assert len(result.tool_calls) == MAX_TOOL_ITERATIONS
    assert agent.messages[-1] == {"role": "assistant", "content": TURN_LIMIT_REPLY}


def test_empty_model_reply_falls_back_to_a_readable_message():
    agent = agent_with([text_message("")])
    result = agent.ask("hello")
    assert result.reply == EMPTY_REPLY_FALLBACK
    # and the history stays replayable rather than holding a contentless message
    assert agent.messages[-1]["content"] == EMPTY_REPLY_FALLBACK


def test_history_persists_across_turns():
    agent = agent_with([text_message("first"), text_message("second")])
    agent.ask("one")
    agent.ask("two")
    roles = [m["role"] for m in agent.messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]


def test_tool_results_are_serialised_into_the_transcript(monkeypatch):
    """The model sees the tool's JSON -- that is where its numbers come from."""
    monkeypatch.setitem(TOOL_DISPATCH, "airport_profile", lambda code: {"code": code, "score": 71.4})
    agent = agent_with([
        tool_call_message("airport_profile", {"code": "BOS"}, call_id="call_abc"),
        text_message("done"),
    ])

    agent.ask("Tell me about BOS")

    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_abc"
    assert json.loads(tool_messages[0]["content"]) == {"code": "BOS", "score": 71.4}


def test_system_prompt_leads_the_transcript():
    agent = agent_with([text_message("hi")])
    assert agent.messages[0]["role"] == "system"
    assert "never" in agent.messages[0]["content"].lower()
