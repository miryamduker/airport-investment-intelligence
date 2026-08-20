"""Cached access to the frozen mart/config layer, shared by every tool.

Loading and percentile/pillar computation are cached at module scope so a
multi-tool-call conversation doesn't re-read parquet or re-rank the whole
country on every turn. Nothing here touches the network.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

from scoring.score import (
    compute_metric_percentiles,
    compute_pillar_scores,
    filter_investable,
    score_airports,
)

MART_METRICS_PATH = Path("data/marts/mart_airport_metrics.parquet")
DIM_AIRPORT_PATH = Path("data/staging/dim_airport.parquet")
T100_PATH = Path("data/staging/t100_segment.parquet")

SCORING_CONFIG_PATH = Path("config/scoring.yaml")
REGIONS_CONFIG_PATH = Path("config/regions.yaml")
ALIASES_CONFIG_PATH = Path("config/aliases.yaml")
DIAGNOSIS_CONFIG_PATH = Path("config/diagnosis.yaml")
HAUL_CONFIG_PATH = Path("config/haul.yaml")

AS_OF = "2026-05"

# Polarity lives with the metric in config/scoring.yaml; this is what the
# tools need to render a raw value.
RAW_METRIC_COLUMNS: dict[str, dict[str, Any]] = {
    "load_factor": {
        "label": "Load factor (passengers / seats)",
        "unit": "ratio",
        "pillar": "demand_pressure",
        "higher_raw_means": "fuller flights",
    },
    "nas_delay_per_departure": {
        "label": "NAS delay per departure",
        "unit": "minutes",
        "pillar": "capacity_strain",
        "higher_raw_means": "more locally caused (National Airspace System) congestion",
    },
    "taxi_out_p80": {
        "label": "80th-percentile taxi-out time",
        "unit": "minutes",
        "pillar": "capacity_strain",
        "higher_raw_means": "more ground/airfield congestion",
    },
    "pct_delayed_15": {
        "label": "Share of departures delayed 15+ minutes",
        "unit": "share (0-1)",
        "pillar": "capacity_strain",
        "higher_raw_means": "more schedule strain",
    },
    "cancellation_rate": {
        "label": "Cancellation rate",
        "unit": "share (0-1)",
        "pillar": "capacity_strain",
        "higher_raw_means": "more operational strain",
    },
    "runways_per_mpax": {
        "label": "Runways per million passengers",
        "unit": "runways / million pax",
        "pillar": "feasibility",
        "higher_raw_means": "more airfield headroom relative to traffic",
    },
}

# Evidence for each of these is in docs/LIMITATIONS.md.
CAVEAT_COHORT_PERCENTILE = (
    "Scores and percentiles are computed within each airport's hub_size "
    "cohort (large/medium/small/non_hub), not nationally -- a small airport "
    "at 90 is top-of-cohort for small hubs, not directly comparable in raw "
    "traffic to a large hub at 90."
)
CAVEAT_GROWTH_MISSING = (
    "growth_trajectory, the fourth pillar of the designed model, is not "
    "computed in this build (it needs multi-year T-100 history and an FAA "
    "TAF ingest, neither of which this build has). It carries no weight "
    "here: every composite is built from demand_pressure, capacity_strain "
    "and feasibility only, so forward-looking growth is absent from the "
    "ranking rather than estimated."
)
CAVEAT_LOAD_FACTOR_ONLY = (
    "demand_pressure rests on load_factor alone this build and systematically "
    "understates pressure at large, high-frequency hubs (e.g. BOS scores in "
    "the bottom quintile of its own cohort on this pillar) -- see "
    "docs/LIMITATIONS.md."
)
CAVEAT_SINGLE_MONTH = "Figures reflect one month of data (May 2026), not a trailing 12-month or annual view."
CAVEAT_HUB_SIZE_ONE_MONTH = (
    "hub_size cohort is derived from May 2026's one-month passenger share, "
    "not the FAA's own full-year hub-size designation."
)
CAVEAT_CLASS_L_INCLUDED = (
    "Volume figures include CLASS='L' (non-scheduled/charter) passenger "
    "traffic alongside scheduled service, per AGENTS.md's passenger-service "
    "filter -- see docs/LIMITATIONS.md."
)
CAVEAT_INVESTABILITY_FLOOR = (
    "non_hub airports are excluded by the investability floor "
    "(config/scoring.yaml: thresholds.investable_hub_sizes) -- they are too "
    "small a share of national traffic to be a plausible infrastructure "
    "investment target, even if a thin month of data lets one top its own cohort."
)


def _read_parquet(path: Path) -> pd.DataFrame:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM read_parquet('{path.as_posix()}')").df()
    finally:
        con.close()


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@lru_cache(maxsize=1)
def metrics_df() -> pd.DataFrame:
    return _read_parquet(MART_METRICS_PATH)


@lru_cache(maxsize=1)
def dim_airport_df() -> pd.DataFrame:
    return _read_parquet(DIM_AIRPORT_PATH)


@lru_cache(maxsize=1)
def scoring_config() -> dict:
    return _read_yaml(SCORING_CONFIG_PATH)


@lru_cache(maxsize=1)
def regions_config() -> dict:
    return _read_yaml(REGIONS_CONFIG_PATH)


@lru_cache(maxsize=1)
def aliases_config() -> dict:
    return _read_yaml(ALIASES_CONFIG_PATH)


@lru_cache(maxsize=1)
def diagnosis_config() -> dict:
    return _read_yaml(DIAGNOSIS_CONFIG_PATH)


@lru_cache(maxsize=1)
def haul_config() -> dict:
    return _read_yaml(HAUL_CONFIG_PATH)


@lru_cache(maxsize=1)
def region_names() -> list[str]:
    return sorted(regions_config()["regions"].keys())


@lru_cache(maxsize=1)
def pillar_names() -> list[str]:
    return list(scoring_config()["pillars"])


@lru_cache(maxsize=1)
def percentiles() -> pd.DataFrame:
    """Cohort percentile per implemented metric. Row order matches
    metrics_df(), so the two frames align positionally."""
    return compute_metric_percentiles(metrics_df(), scoring_config()["pillars"])


@lru_cache(maxsize=1)
def pillar_scores() -> pd.DataFrame:
    return compute_pillar_scores(percentiles(), scoring_config()["pillars"])


@lru_cache(maxsize=8)
def scored(profile: str) -> pd.DataFrame:
    return score_airports(metrics_df(), scoring_config(), profile)


@lru_cache(maxsize=8)
def scored_investable(profile: str) -> pd.DataFrame:
    return filter_investable(scored(profile), scoring_config())


def row_index_for_code(code: str) -> int | None:
    """Positional index into metrics_df()/percentiles()/pillar_scores(), or
    None if the code isn't in the mart."""
    matches = metrics_df().index[metrics_df()["code"] == code.upper()]
    return int(matches[0]) if len(matches) else None


def t100_connection() -> duckdb.DuckDBPyConnection:
    """Fresh connection for flight_mix's ad-hoc T-100 queries; caller closes."""
    return duckdb.connect()
