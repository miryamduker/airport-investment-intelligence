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

import openai
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from agent.loop import DEFAULT_MODEL, Agent
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


def _upstream_error(status_code: int, detail: str, exc: Exception) -> JSONResponse:
    logger.error("upstream model error -> %d: %s (%s)", status_code, exc.__class__.__name__, exc)
    return JSONResponse(status_code=status_code, content={"detail": detail})


@app.exception_handler(openai.AuthenticationError)
async def handle_auth_error(request: Request, exc: openai.AuthenticationError) -> JSONResponse:
    return _upstream_error(502, "The model provider rejected our API key. Check OPENAI_API_KEY.", exc)


@app.exception_handler(openai.RateLimitError)
async def handle_rate_limit(request: Request, exc: openai.RateLimitError) -> JSONResponse:
    return _upstream_error(429, "The model provider is rate-limiting us. Wait a moment and try again.", exc)


@app.exception_handler(openai.APITimeoutError)
async def handle_timeout(request: Request, exc: openai.APITimeoutError) -> JSONResponse:
    return _upstream_error(504, "The model provider did not respond in time. Try again.", exc)


@app.exception_handler(openai.APIConnectionError)
async def handle_connection_error(request: Request, exc: openai.APIConnectionError) -> JSONResponse:
    return _upstream_error(502, "Could not reach the model provider. Check the network connection.", exc)


@app.exception_handler(openai.APIError)
async def handle_api_error(request: Request, exc: openai.APIError) -> JSONResponse:
    """Catch-all for the rest of the OpenAI error family. Registered last so
    the specific handlers above win; Starlette resolves by walking the MRO."""
    return _upstream_error(502, "The model provider returned an error. Try again.", exc)


@app.get("/health")
def health() -> dict[str, Any]:
    """Lets the UI tell 'backend down' from 'backend slow' before a send."""
    return {
        "status": "ok",
        "model": DEFAULT_MODEL,
        "as_of": d.AS_OF,
        "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY")),
    }


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
