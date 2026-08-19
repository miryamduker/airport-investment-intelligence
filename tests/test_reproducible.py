"""Reproducibility guarantees for scoring/score.py:

1. Scoring the same input twice produces byte-identical output -- the model
   never sees or invents a number, so nothing in this pipeline should be
   nondeterministic either.
2. Percentiles are computed nationally, never within a region-filtered
   subset (CLAUDE.md: "Never rank within a filtered subset -- that would
   make the best airport in any region score 100 by definition"). Region
   filtering must happen after score_airports returns, not before.

Run:
    python -m pytest tests/test_reproducible.py -v
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml

from scoring.score import score_airports

MART_PATH = Path("data/marts/mart_airport_metrics.parquet")
WEIGHTS_CONFIG_PATH = Path("config/weights.yaml")
PROFILE = "general"


@pytest.fixture(scope="module")
def metrics_df() -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"SELECT * FROM read_parquet('{MART_PATH.as_posix()}')").df()
    con.close()
    return df


@pytest.fixture(scope="module")
def weights_config() -> dict:
    return yaml.safe_load(WEIGHTS_CONFIG_PATH.read_text())


def test_scoring_is_byte_identical_across_runs(metrics_df, weights_config):
    scored_a = score_airports(metrics_df, weights_config, PROFILE)
    scored_b = score_airports(metrics_df, weights_config, PROFILE)

    csv_a = scored_a.to_csv(index=False).encode("utf-8")
    csv_b = scored_b.to_csv(index=False).encode("utf-8")
    assert csv_a == csv_b


def test_region_filter_after_scoring_matches_national(metrics_df, weights_config):
    national = score_airports(metrics_df, weights_config, PROFILE)
    new_england = (
        national[national["region"] == "new_england"]
        .sort_values("code")
        .reset_index(drop=True)
    )
    assert len(new_england) > 0

    # Filtering the already-scored national output is idempotent: doing it
    # from a freshly-recomputed national score gives the identical subset.
    national_again = score_airports(metrics_df, weights_config, PROFILE)
    new_england_again = (
        national_again[national_again["region"] == "new_england"]
        .sort_values("code")
        .reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(new_england, new_england_again)

    # Regression guard: scoring a region-prefiltered DataFrame ranks
    # airports against a much smaller hub_size cohort, which produces
    # DIFFERENT (wrong) percentiles than filtering the nationally-scored
    # output. If this ever matches the correct national-then-filter result
    # for every airport, something has silently shrunk the national cohort
    # down to just this region before ranking.
    region_only_metrics = (
        metrics_df[metrics_df["region"] == "new_england"].reset_index(drop=True)
    )
    scored_region_only = (
        score_airports(region_only_metrics, weights_config, PROFILE)
        .sort_values("code")
        .reset_index(drop=True)
    )

    composites_correct = new_england["composite"].round(6).reset_index(drop=True)
    composites_wrong = scored_region_only["composite"].round(6).reset_index(drop=True)
    assert not composites_correct.equals(composites_wrong)
