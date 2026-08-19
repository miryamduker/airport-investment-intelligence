"""Build data/marts/mart_airport_metrics.parquet: one row per origin
airport, aggregated from T-100 Segment, joined to the airport dimension and
to On-Time Performance.

Metrics computed (see CLAUDE.md's scoring model for the full four-pillar
target):
    load_factor              = SUM(passengers) / SUM(seats)                    [demand pressure]
    departures_performed, passengers, distinct_destinations                    [volume, for context/diagnosis]
    runways_per_mpax          = runway_count / (passengers / 1e6)               [feasibility]
    nas_delay_per_departure, taxi_out_p80, pct_delayed_15, cancellation_rate    [capacity strain]

capacity_strain's four metrics come from data/staging/ontime_airport_monthly
.parquet (ingest/ontime.py), LEFT JOINed on airport code -- NOT from T-100's
DEPARTURES_SCHEDULED. An earlier attempt (completion_gap = 1 -
performed/scheduled) was tried and removed: DEPARTURES_SCHEDULED turned out
to be unreliable at scale, not just a small-airport edge case. 362 of 588
surviving airports (62%) showed performed > scheduled, worse at major hubs,
not better -- JFK +23%, LAX +17%, ORD +4%, ATL +1%. See docs/DATA_RECON.md
for the full investigation. On-Time Performance only covers "reporting
carriers" (roughly the larger airlines), so this join leaves capacity_strain
NaN for any surviving airport with no May 2026 OTP match -- never imputed,
propagates through scoring/score.py's pillar renormalization.

3y CAGR, TAF growth (growth trajectory), and peak-month concentration
(demand pressure) are also not computed here -- see CLAUDE.md and
config/weights.yaml for why.

Run:
    python -m scoring.metrics
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import yaml

T100_PATH = Path("data/staging/t100_segment.parquet")
DIM_AIRPORT_PATH = Path("data/staging/dim_airport.parquet")
ONTIME_PATH = Path("data/staging/ontime_airport_monthly.parquet")
WEIGHTS_CONFIG_PATH = Path("config/weights.yaml")
OUT_PATH = Path("data/marts/mart_airport_metrics.parquet")


def build_base_metrics(con: duckdb.DuckDBPyConnection, min_departures: int):
    """T-100 + airport dimension only, no On-Time Performance yet. Returns
    (base_metrics_df, ranked_df) where ranked_df carries every US-matched
    origin (pre-threshold) with its rank by departures, for the cutoff
    diagnostic report."""

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE agg AS
        SELECT
            t.ORIGIN AS iata,
            SUM(t.PASSENGERS) AS passengers,
            SUM(t.SEATS) AS seats,
            SUM(t.DEPARTURES_PERFORMED) AS departures_performed,
            COUNT(DISTINCT t.DEST) AS distinct_destinations
        FROM read_parquet('{T100_PATH.as_posix()}') t
        INNER JOIN read_parquet('{DIM_AIRPORT_PATH.as_posix()}') d
            ON t.ORIGIN = d.iata
        GROUP BY t.ORIGIN
        """
    )

    ranked = con.execute(
        """
        SELECT *, RANK() OVER (ORDER BY departures_performed DESC) AS departures_rank
        FROM agg
        ORDER BY departures_performed DESC
        """
    ).df()

    base_metrics = con.execute(
        f"""
        SELECT
            d.iata AS code,
            d.name,
            d.state,
            d.region,
            d.hub_size,
            d.slot_controlled,
            d.runway_count,
            a.passengers,
            a.seats,
            a.departures_performed,
            a.distinct_destinations,
            a.passengers::DOUBLE / NULLIF(a.seats, 0) AS load_factor,
            d.runway_count::DOUBLE / NULLIF(a.passengers / 1e6, 0) AS runways_per_mpax
        FROM agg a
        INNER JOIN read_parquet('{DIM_AIRPORT_PATH.as_posix()}') d ON a.iata = d.iata
        WHERE a.departures_performed >= {min_departures}
        ORDER BY code
        """
    ).df()

    return base_metrics, ranked


def print_otp_coverage_report(con: duckdb.DuckDBPyConnection, base_metrics, investable_hub_sizes: set) -> None:
    """How many investable (large/medium/small hub) airports have a May
    2026 On-Time Performance match -- printed first, before anything else,
    since that's the number that determines how much of capacity_strain
    will actually be populated for the airports that make it into an
    investment ranking. non_hub airports are excluded here because the
    investability floor excludes them from investment rankings regardless
    (config/weights.yaml: thresholds.investable_hub_sizes)."""

    investable = base_metrics[base_metrics["hub_size"].isin(investable_hub_sizes)]
    otp_codes = set(
        con.execute(f"SELECT DISTINCT iata FROM read_parquet('{ONTIME_PATH.as_posix()}')").df()["iata"]
    )
    matched = investable[investable["code"].isin(otp_codes)]
    missing = investable[~investable["code"].isin(otp_codes)].sort_values("code")

    print("=" * 70)
    print("ON-TIME PERFORMANCE COVERAGE (investable hub airports)")
    print("=" * 70)
    print(
        f"{len(matched)} of {len(investable)} large/medium/small hub airports "
        f"have a May 2026 On-Time Performance match"
    )
    if len(missing):
        print(f"missing ({len(missing)}): {', '.join(missing['code'].tolist())}")
    print()


def join_ontime(con: duckdb.DuckDBPyConnection, base_metrics: pd.DataFrame) -> pd.DataFrame:
    """LEFT JOIN On-Time Performance's airport-month aggregate onto
    base_metrics. Airports with no OTP match get NULL for all four
    capacity_strain metrics -- never imputed, see module docstring."""
    con.register("base_metrics", base_metrics)
    return con.execute(
        f"""
        SELECT
            b.*,
            o.nas_delay_per_departure,
            o.taxi_out_p80,
            o.pct_delayed_15,
            o.cancellation_rate,
            o.reporting_carrier_count
        FROM base_metrics b
        LEFT JOIN read_parquet('{ONTIME_PATH.as_posix()}') o ON b.code = o.iata
        ORDER BY b.code
        """
    ).df()


def print_cutoff_report(ranked, min_departures: int, t100_path: Path, con: duckdb.DuckDBPyConnection) -> None:
    raw_origin_count = con.execute(
        f"SELECT COUNT(DISTINCT ORIGIN) FROM read_parquet('{t100_path.as_posix()}')"
    ).fetchone()[0]

    us_matched = len(ranked)
    survivors = ranked[ranked["departures_performed"] >= min_departures]

    print("=" * 70)
    print("THRESHOLD REPORT")
    print("=" * 70)
    print(f"distinct origins in raw T-100 (incl. foreign):      {raw_origin_count:,}")
    print(f"origins matched to a US airport (dim_airport join): {us_matched:,}")
    print(f"origins surviving min_departures_threshold >= {min_departures}:  {len(survivors):,}")

    cutoff_idx = ranked.index[ranked["departures_performed"] >= min_departures].max()
    window = ranked.loc[max(0, cutoff_idx - 19): cutoff_idx + 20]
    print(f"\n20 airports immediately above and below the cutoff (>= {min_departures}):")
    for _, row in window.iterrows():
        side = "ABOVE" if row["departures_performed"] >= min_departures else "below"
        print(f"  {side:>5s}  {row['iata']:>4s}  departures={row['departures_performed']:>6,.0f}")

    print("\ncount per hub_size cohort after filtering:")
    survivors_iata = set(survivors["iata"])
    dim = con.execute(f"SELECT iata, hub_size FROM read_parquet('{DIM_AIRPORT_PATH.as_posix()}')").df()
    dim = dim[dim["iata"].isin(survivors_iata)]
    print(dim["hub_size"].value_counts().to_string())


def main() -> None:
    weights_config = yaml.safe_load(WEIGHTS_CONFIG_PATH.read_text())
    min_departures = weights_config["thresholds"]["min_departures_threshold"]
    investable_hub_sizes = set(weights_config["thresholds"]["investable_hub_sizes"])

    con = duckdb.connect()

    base_metrics, ranked = build_base_metrics(con, min_departures)
    print_otp_coverage_report(con, base_metrics, investable_hub_sizes)

    metrics = join_ontime(con, base_metrics)
    print_cutoff_report(ranked, min_departures, T100_PATH, con)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    con.register("metrics", metrics)
    con.execute(f"COPY metrics TO '{OUT_PATH.as_posix()}' (FORMAT PARQUET)")
    con.close()

    print(f"\nwrote {len(metrics):,} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
