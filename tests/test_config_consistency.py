"""Cross-file invariants that config/ comments assert but nothing enforced.

Each config file is loaded by different code, so a value can be renamed in
one place and silently ignored in another. These are the seams:

1. hub_size labels. config/ingest.yaml sets the cohort boundaries;
   ingest/airports.py turns them into the strings large/medium/small/non_hub;
   config/scoring.yaml filters rankings on those strings. A typo in
   investable_hub_sizes empties every ranking without raising anything.
2. Weight sets that must sum to 1 -- pillar profiles and the confidence blend.
3. Polarity must be exactly +1 or -1: score.py multiplies by it, so 0 would
   silently flatten a metric and 2 would double-weight it.
4. Codes referenced in config (aliases, slot control) must exist in the
   airport dimension, as those files claim.

Run:
    python -m pytest tests/test_config_consistency.py -v
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

CONFIG_DIR = Path("config")
DIM_AIRPORT_PATH = Path("data/staging/dim_airport.parquet")

# The labels ingest/airports.py::add_hub_size can emit. Kept here as the
# explicit contract: if that function grows a new tier, this list is the
# thing that has to change with it.
HUB_SIZE_LABELS = {"large", "medium", "small", "non_hub"}

# The pillars this build actually computes. growth_trajectory is part of the
# designed model (DESIGN.md 2.1) but has no implemented metric, so it is
# deliberately absent from config/scoring.yaml rather than declared empty.
PILLARS = {"demand_pressure", "capacity_strain", "feasibility"}


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def scoring_cfg() -> dict:
    return _load("scoring.yaml")


@pytest.fixture(scope="module")
def ingest() -> dict:
    return _load("ingest.yaml")


@pytest.fixture(scope="module")
def dim_codes() -> set[str]:
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    con = duckdb.connect()
    codes = con.execute(
        f"SELECT iata FROM read_parquet('{DIM_AIRPORT_PATH.as_posix()}')"
    ).df()["iata"]
    con.close()
    return set(codes)


# --- 1. hub_size labels agree across ingest.yaml and scoring.yaml ----------

def test_investable_hub_sizes_are_real_labels(scoring_cfg):
    """The failure this exists for: 'mid' instead of 'medium' in
    scoring.yaml drops every mid-size hub from rankings, silently."""
    investable = set(scoring_cfg["thresholds"]["investable_hub_sizes"])
    unknown = investable - HUB_SIZE_LABELS
    assert not unknown, (
        f"investable_hub_sizes contains labels ingest/airports.py never emits: "
        f"{sorted(unknown)}. Valid labels: {sorted(HUB_SIZE_LABELS)}"
    )


def test_non_hub_is_not_investable(scoring_cfg):
    """non_hub is excluded by design -- percentile rank measures 'unusual for
    its cohort', so a thin month floats a tiny airport to the top of its own."""
    assert "non_hub" not in scoring_cfg["thresholds"]["investable_hub_sizes"]


def test_hub_size_boundaries_are_strictly_descending(ingest):
    """add_hub_size tests these in order; out-of-order bounds make a tier
    unreachable rather than raising."""
    hub = ingest["hub_size"]
    assert hub["large_min_share"] > hub["medium_min_share"] > hub["small_min_share"] > 0


def test_investable_labels_actually_appear_in_the_dimension(scoring_cfg):
    """Guards the other direction: labels that are valid in the abstract but
    match no airport in the built data."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    con = duckdb.connect()
    present = set(
        con.execute(
            f"SELECT DISTINCT hub_size FROM read_parquet('{DIM_AIRPORT_PATH.as_posix()}')"
        ).df()["hub_size"]
    )
    con.close()
    investable = set(scoring_cfg["thresholds"]["investable_hub_sizes"])
    assert investable <= present, (
        f"investable_hub_sizes names cohorts absent from dim_airport: "
        f"{sorted(investable - present)}"
    )


# --- 2. weight sets sum to 1 ----------------------------------------------

@pytest.mark.parametrize("profile", ["terminal_expansion", "airfield_expansion", "general"])
def test_profile_weights_sum_to_one(scoring_cfg, profile):
    assert profile in scoring_cfg["profiles"], f"missing profile: {profile}"
    w = scoring_cfg["profiles"][profile]
    assert set(w) == PILLARS, f"{profile} pillars {set(w)} != {PILLARS}"
    assert sum(w.values()) == pytest.approx(1.0)


def test_confidence_weights_sum_to_one(scoring_cfg):
    """compute_confidence does not normalise -- these three must sum to 1 or
    every confidence score is scaled wrong."""
    c = scoring_cfg["confidence"]
    total = c["metric_coverage_weight"] + c["volume_weight"] + c["carrier_diversity_weight"]
    assert total == pytest.approx(1.0)


def test_confidence_saturation_points_are_positive(scoring_cfg):
    """Both are divisors in compute_confidence."""
    c = scoring_cfg["confidence"]
    assert c["full_confidence_departures"] > 0
    assert c["full_confidence_carriers"] > 0


# --- 3. polarity is exactly +1 or -1 --------------------------------------

def test_every_metric_declares_valid_polarity(scoring_cfg):
    assert set(scoring_cfg["pillars"]) == PILLARS
    for pillar, cfg in scoring_cfg["pillars"].items():
        assert cfg["metrics"], f"{pillar} declares no metrics"
        for metric, mcfg in cfg["metrics"].items():
            assert mcfg["polarity"] in (1, -1), (
                f"{pillar}.{metric} polarity {mcfg['polarity']!r} is not +1 or -1"
            )


# --- 4. codes named in config exist in the airport dimension --------------

def test_slot_controlled_airports_exist(ingest, dim_codes):
    missing = set(ingest["slot_controlled_airports"]) - dim_codes
    assert not missing, f"slot_controlled_airports not in dim_airport: {sorted(missing)}"


def test_alias_codes_exist(dim_codes):
    """aliases.yaml claims 'codes must exist in dim_airport.parquet'."""
    aliases = _load("aliases.yaml")["metro_aliases"]
    missing = {c for codes in aliases.values() for c in codes} - dim_codes
    assert not missing, f"alias codes not in dim_airport: {sorted(missing)}"


def test_every_alias_round_trips_through_the_resolver():
    """An alias that does not actually win the resolver is dead config. This
    also pins the priority order: aliases are tried before the city step, so
    a key that is also a municipality must still resolve to the curated
    codes, not to whatever the city match would have returned."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    from tools.lookup import resolve_airports

    for key, codes in _load("aliases.yaml")["metro_aliases"].items():
        res = resolve_airports(key)
        assert res["match_type"] == "metro_alias", (
            f"alias {key!r} was resolved as {res['match_type']}, not metro_alias"
        )
        assert [m["code"] for m in res["matches"]] == list(codes), (
            f"alias {key!r} resolved to {[m['code'] for m in res['matches']]}, expected {codes}"
        )


def test_multi_code_aliases_are_flagged_ambiguous():
    """The guess-prevention guarantee: a multi-airport metro must stop the
    agent and make it ask. Single-code aliases are legitimate -- they map
    names that are not a municipality ("Guam") or override a city match that
    lands on the wrong airport ("Sanford" -> SFM in Maine) -- so the rule is
    about ambiguity being reported, not about entry length."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    from tools.lookup import resolve_airports

    for key, codes in _load("aliases.yaml")["metro_aliases"].items():
        res = resolve_airports(key)
        assert res["ambiguous"] == (len(codes) > 1), (
            f"alias {key!r} has {len(codes)} codes but ambiguous={res['ambiguous']}"
        )


def test_city_step_reaches_hubs_named_after_people():
    """Regression guard for the bug this step was added for: SJU is named
    "Luis Munoz Marin", so substring matching resolved "San Juan" to UGI,
    a seaplane base in Alaska, with ambiguous=false and confidence 0.8."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    from tools.lookup import resolve_airports

    for query, expected in [
        ("San Juan", "SJU"), ("Las Vegas", "LAS"), ("Honolulu", "HNL"),
        ("Milwaukee", "MKE"), ("Omaha", "OMA"), ("Knoxville", "TYS"),
        ("Cincinnati", "CVG"), ("Boston", "BOS"),
    ]:
        res = resolve_airports(query)
        assert [m["code"] for m in res["matches"]] == [expected], (
            f"{query!r} resolved to {[m['code'] for m in res['matches']]}, expected [{expected!r}]"
        )


def test_portland_stays_ambiguous():
    """Two real cities share the name; the city step must not pick one."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    from tools.lookup import resolve_airports

    res = resolve_airports("Portland")
    assert res["ambiguous"] is True
    assert set(m["code"] for m in res["matches"]) == {"PDX", "PWM"}


def test_every_declared_region_matches_at_least_one_airport():
    """Dead-region guard. `territories` was declared here for months while
    ingest/airports.py filtered OurAirports on iso_country = 'US' alone --
    PR/VI/GU/AS/MP have their own ISO codes, so no airport ever received the
    region and SJU (a medium hub) was missing from the mart entirely."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    declared = set(_load("regions.yaml")["regions"])
    con = duckdb.connect()
    present = set(
        con.execute(
            f"SELECT DISTINCT region FROM read_parquet('{DIM_AIRPORT_PATH.as_posix()}') "
            "WHERE region IS NOT NULL"
        ).df()["region"]
    )
    con.close()
    assert declared <= present, (
        f"regions declared in config but matching no airport: {sorted(declared - present)}"
    )


def test_no_airport_is_left_without_a_region():
    """The inverse: a state code in the dimension that regions.yaml does not
    map would silently drop that airport from every region filter."""
    if not DIM_AIRPORT_PATH.exists():
        pytest.skip("dim_airport.parquet not built")
    con = duckdb.connect()
    unmapped = con.execute(
        f"SELECT DISTINCT state FROM read_parquet('{DIM_AIRPORT_PATH.as_posix()}') "
        "WHERE region IS NULL AND state IS NOT NULL"
    ).df()["state"].tolist()
    con.close()
    assert not unmapped, f"states with no region mapping: {sorted(unmapped)}"


def test_regions_partition_states_without_overlap():
    """A state in two regions would double-count it in region filters."""
    regions = _load("regions.yaml")["regions"]
    seen: dict[str, str] = {}
    for region, states in regions.items():
        for state in states:
            assert state not in seen, f"{state} in both {seen[state]} and {region}"
            seen[state] = region
