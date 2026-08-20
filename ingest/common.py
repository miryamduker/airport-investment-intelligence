"""Helpers shared by every ingest module."""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd

# Networks running a TLS-intercepting proxy present a root CA that Python's
# certifi store doesn't know. Point this at a combined bundle (see README) to
# make outbound HTTPS work there; unset falls back to certifi.
CA_BUNDLE_ENV = "BTS_CA_BUNDLE"


def ca_bundle_verify() -> bool | str:
    return os.environ.get(CA_BUNDLE_ENV) or True


def write_parquet(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.register("df", df)
        con.execute(f"COPY df TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    finally:
        con.close()
