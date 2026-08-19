"""Guarantees for flight_mix's haul_mix block.

1. The bands in config/haul.yaml bin every departure exactly once, so the
   shares are a real decomposition and not a subset that happens to look
   like one.
2. The same call twice returns identical output (CLAUDE.md's core rule --
   the arithmetic is Python's, not the model's).
3. The band arithmetic matches an independent recomputation from the
   staging parquet.
4. All-cargo service is excluded. This is the Anchorage trap: including
   CLASS 'G'/'P' would report ANC as 40% long-haul freighter traffic
   instead of ~10% passenger service.

Run:
    python -m pytest tests/test_haul_mix.py -v
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml

from tools.traffic import flight_mix

T100_PATH = Path("data/staging/t100_segment.parquet")
HAUL_CONFIG_PATH = Path("config/haul.yaml")
INGEST_CONFIG_PATH = Path("config/ingest.yaml")

# ANC is the case the bands were stress-tested against: a bush-aviation
# short-haul tail and a freight operation big enough to swamp the answer if
# the passenger-service filter ever stops being applied.
SAMPLE_CODES = ["ANC", "SFO", "BOS", "LAX", "JFK"]


@pytest.fixture(scope="module")
def t100_df() -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"SELECT * FROM read_parquet('{T100_PATH.as_posix()}')").df()
    con.close()
    return df


@pytest.fixture(scope="module")
def bands() -> list[dict]:
    return yaml.safe_load(HAUL_CONFIG_PATH.read_text())["bands"]


def test_bands_are_ordered_and_contiguous(bands):
    """A gap between bands would silently drop departures from the shares;
    an overlap would double-count them."""
    assert bands[0]["min_miles"] == 0, "first band must start at zero"
    assert bands[-1]["max_miles"] is None, "last band must be unbounded"
    for lower, upper in zip(bands, bands[1:]):
        assert lower["max_miles"] == upper["min_miles"]


@pytest.mark.parametrize("code", SAMPLE_CODES)
def test_haul_shares_sum_to_one(code):
    result = flight_mix(code)
    assert result["found"]

    for unit in ("departures", "passengers", "seats"):
        total = sum(band[f"share_of_{unit}"] for band in result["haul_mix"])
        assert total == pytest.approx(1.0, abs=1e-3), f"{code}: {unit} shares sum to {total}"

    banded = sum(band["departures"] for band in result["haul_mix"])
    assert banded == result["total_departures_performed"]


@pytest.mark.parametrize("code", SAMPLE_CODES)
def test_haul_mix_is_deterministic(code):
    assert flight_mix(code)["haul_mix"] == flight_mix(code)["haul_mix"]


def test_haul_mix_matches_independent_recomputation(t100_df, bands):
    """Recompute ANC's bands in pandas and check the tool's DuckDB path
    agrees -- the two disagreeing means a bug in the band CASE."""
    origin = t100_df[t100_df["ORIGIN"] == "ANC"]
    result = {band["band"]: band for band in flight_mix("ANC")["haul_mix"]}

    for band in bands:
        low, high = band["min_miles"], band["max_miles"]
        in_band = origin["DISTANCE"] >= low
        if high is not None:
            in_band &= origin["DISTANCE"] < high
        rows = origin[in_band]

        assert result[band["name"]]["departures"] == int(rows["DEPARTURES_PERFORMED"].sum())
        assert result[band["name"]]["passengers"] == int(rows["PASSENGERS"].sum())
        assert result[band["name"]]["seats"] == int(rows["SEATS"].sum())


def test_all_cargo_service_is_excluded(t100_df):
    """The Anchorage trap. ANC's all-cargo departures are overwhelmingly
    long-haul, so if CLASS 'G'/'P' ever reach the staging table the
    long-haul share stops describing passenger service."""
    allowed = yaml.safe_load(INGEST_CONFIG_PATH.read_text())["t100_segment"]["passenger_service_classes"]
    assert set(t100_df["CLASS"].unique()) <= set(allowed)

    long_haul = next(b for b in flight_mix("ANC")["haul_mix"] if b["band"] == "long_haul")
    # Passenger service sits near 0.10; the all-class figure is 0.40.
    assert long_haul["share_of_departures"] < 0.20
