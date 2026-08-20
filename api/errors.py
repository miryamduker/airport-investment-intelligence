"""Upstream (OpenAI) failures -> readable HTTP responses.

Kept out of main.py because it is a different concern from the routes:
five handlers of identical shape, differing only in which exception maps to
which status and sentence. Registering them from a table keeps that visible
as data instead of forty lines of near-duplicate functions.
"""
from __future__ import annotations

import logging
from typing import Callable

import openai
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# Starlette resolves a handler by walking the raised exception's MRO, so the
# specific entries win over the openai.APIError catch-all regardless of the
# order they are registered in.
ERROR_RESPONSES: tuple[tuple[type[Exception], int, str], ...] = (
    (openai.AuthenticationError, 502, "The model provider rejected our API key. Check OPENAI_API_KEY."),
    (openai.RateLimitError, 429, "The model provider is rate-limiting us. Wait a moment and try again."),
    (openai.APITimeoutError, 504, "The model provider did not respond in time. Try again."),
    (openai.APIConnectionError, 502, "Could not reach the model provider. Check the network connection."),
    (openai.APIError, 502, "The model provider returned an error. Try again."),
)


def _handler(status_code: int, detail: str) -> Callable:
    """The provider's own error text is logged, never returned: it can carry
    request ids and key fragments the browser has no business seeing."""

    async def handle(request: Request, exc: Exception) -> JSONResponse:
        logger.error("upstream model error -> %d: %s (%s)", status_code, exc.__class__.__name__, exc)
        return JSONResponse(status_code=status_code, content={"detail": detail})

    return handle


def register_error_handlers(app: FastAPI) -> None:
    for exc_type, status_code, detail in ERROR_RESPONSES:
        app.add_exception_handler(exc_type, _handler(status_code, detail))
