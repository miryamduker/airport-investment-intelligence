"""HTTP plumbing over agent/loop.py -- no logic of its own.

Stateless per request: the client resends prior turns, which are replayed
into a fresh Agent. Responses carry every tool call verbatim, so a UI can
show the raw results behind the prose.

Not hardened for public exposure: no auth, no rate limiting. It is meant to
run on localhost against the operator's own OPENAI_API_KEY -- see README.
The limits below exist to stop an oversized request turning into an
oversized (paid) model call, not to defend against an attacker.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent.loop import DEFAULT_MODEL, Agent
from api.errors import register_error_handlers
from tools import data as d

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 2_000
MAX_HISTORY_TURNS = 40
# Assistant replies are longer than user questions, so history entries get a
# looser cap than a fresh message.
MAX_HISTORY_CHARS = 20_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail fast on a missing key rather than 500-ing on the first question."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Put it in a .env file in the repo root "
            "or export it before starting uvicorn."
        )
    logger.info("agent API ready (model=%s, data as_of=%s)", DEFAULT_MODEL, d.AS_OF)
    yield


app = FastAPI(title="Airport Investment Intelligence Agent API", lifespan=lifespan)

# Dev only: any localhost origin, any port.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=MAX_HISTORY_CHARS)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    history: list[ChatMessage] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)


class ToolCallOut(BaseModel):
    name: str
    arguments: dict[str, Any]
    # Deliberately Any, not dict: a tool that returned a list would otherwise
    # fail at response serialization, after the model call was already paid for.
    result: Any


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallOut]
    as_of: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    model: str
    as_of: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness for the UI, which checks it on load so an unreachable backend
    is visible before the user types a question rather than after.

    `model` and `as_of` are here because they also answer the other question
    worth asking of a running server: not just whether it is up, but whether
    it is the build you think it is."""
    return HealthResponse(status="ok", model=DEFAULT_MODEL, as_of=d.AS_OF)


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    agent = Agent()
    for turn in request.history:
        agent.messages.append({"role": turn.role, "content": turn.content})

    logger.info("chat: %d history turn(s), %d chars in", len(request.history), len(request.message))
    result = agent.ask(request.message)
    return ChatResponse(
        reply=result.reply,
        tool_calls=[ToolCallOut(**asdict(record)) for record in result.tool_calls],
        as_of=d.AS_OF,
    )
