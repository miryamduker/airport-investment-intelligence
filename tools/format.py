"""JSON-safety helpers shared by the tool modules.

Tool payloads must be plain JSON-serializable dicts: pandas NaN becomes
None, numpy scalars become Python scalars.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from tools import data as d

NO_MART_DATA = {"value": 0.0, "reason": "no mart data for this code"}


def num(value: Any, ndigits: int | None = None) -> float | None:
    if value is None or pd.isna(value):
        return None
    value = float(value)
    return round(value, ndigits) if ndigits is not None else value


def integer(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def boolean(value: Any) -> bool | None:
    if value is None or pd.isna(value):
        return None
    return bool(value)


def confidence(conf: dict) -> dict:
    """Round the {value, reason} dict scoring/score.py attaches to each row."""
    return {"value": num(conf["value"], 2), "reason": conf["reason"]}


def confidence_for_code(code: str) -> dict:
    scored = d.scored("general")
    match = scored[scored["code"] == code]
    return NO_MART_DATA if match.empty else confidence(match.iloc[0]["confidence"])
