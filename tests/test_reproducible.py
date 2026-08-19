"""The two guarantees scoring/score.py has to keep:

1. The same input scored twice gives byte-identical output.
2. Percentiles are national, never computed within a region-filtered subset
   -- otherwise the best airport in any region scores 100 by definition.

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

    # Scoring a region-prefiltered frame ranks airports against a much
    # smaller cohort, so it must NOT match the national-then-filter result.
    # If it ever does, something shrank the cohort before ranking.
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
