"""HTTP plumbing over agent/loop.py -- no logic of its own.

Stateless per request: the client resends prior turns, which are replayed
into a fresh Agent. Responses carry every tool call verbatim, so a UI can
show the raw results behind the prose.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.loop import Agent
from tools import data as d

load_dotenv()

app = FastAPI(title="Airport Investment Intelligence Agent API")

# Dev only: any localhost origin, any port.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ToolCallOut(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallOut]
    as_of: str


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    agent = Agent()
    for turn in request.history:
        agent.messages.append({"role": turn.role, "content": turn.content})

    result = agent.ask(request.message)
    return ChatResponse(
        reply=result.reply,
        tool_calls=[ToolCallOut(**asdict(record)) for record in result.tool_calls],
        as_of=d.AS_OF,
    )
