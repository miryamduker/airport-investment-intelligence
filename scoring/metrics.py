"""Build data/marts/mart_airport_metrics.parquet: one row per origin airport,
aggregated from T-100 Segment, joined to the airport dimension and to
On-Time Performance.

    load_factor              = passengers / seats                    [demand]
    runways_per_mpax         = runways / (passengers / 1e6)          [feasibility]
    nas_delay_per_departure, taxi_out_p80, pct_delayed_15,
    cancellation_rate                                                [strain]

The four strain metrics come from On-Time Performance via a LEFT JOIN, so
they stay NULL -- never imputed -- for airports OTP does not cover. Growth
metrics need multi-month history this build does not ingest; see
docs/LIMITATIONS.md.

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
SCORING_CONFIG_PATH = Path("config/scoring.yaml")
OUT_PATH = Path("data/marts/mart_airport_metrics.parquet")


def build_base_metrics(con: duckdb.DuckDBPyConnection, min_departures: int):
    """T-100 joined to the airport dimension, before On-Time Performance.
    Returns (base_metrics, ranked), where ranked keeps every US-matched
    origin pre-threshold for the cutoff report."""
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


def join_ontime(con: duckdb.DuckDBPyConnection, base_metrics: pd.DataFrame) -> pd.DataFrame:
    """LEFT JOIN, so airports with no OTP match keep NULL capacity_strain."""
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


def print_build_report(
    con: duckdb.DuckDBPyConnection,
    base_metrics: pd.DataFrame,
    ranked: pd.DataFrame,
    min_departures: int,
    investable_hub_sizes: set,
) -> None:
    """OTP coverage over investable hubs (how much of capacity_strain will be
    populated where it matters) plus what the departures threshold kept."""
    print("=" * 70)
    print("BUILD REPORT")
    print("=" * 70)

    investable = base_metrics[base_metrics["hub_size"].isin(investable_hub_sizes)]
    otp_codes = set(
        con.execute(f"SELECT DISTINCT iata FROM read_parquet('{ONTIME_PATH.as_posix()}')").df()["iata"]
    )
    matched = investable[investable["code"].isin(otp_codes)]
    missing = investable[~investable["code"].isin(otp_codes)].sort_values("code")
    print(
        f"\nOn-Time Performance coverage: {len(matched)} of {len(investable)} "
        f"large/medium/small hub airports matched"
    )
    if len(missing):
        print(f"  missing ({len(missing)}): {', '.join(missing['code'].tolist())}")

    raw_origins = con.execute(
        f"SELECT COUNT(DISTINCT ORIGIN) FROM read_parquet('{T100_PATH.as_posix()}')"
    ).fetchone()[0]
    survivors = ranked[ranked["departures_performed"] >= min_departures]
    print(f"\ndistinct origins in raw T-100 (incl. foreign):      {raw_origins:,}")
    print(f"origins matched to a US airport (dim_airport join): {len(ranked):,}")
    print(f"origins surviving min_departures >= {min_departures}:          {len(survivors):,}")

    print("\ncount per hub_size cohort after filtering:")
    print(base_metrics["hub_size"].value_counts().to_string())


def main() -> None:
    scoring_config = yaml.safe_load(SCORING_CONFIG_PATH.read_text())
    min_departures = scoring_config["thresholds"]["min_departures_threshold"]
    investable_hub_sizes = set(scoring_config["thresholds"]["investable_hub_sizes"])

    con = duckdb.connect()
    base_metrics, ranked = build_base_metrics(con, min_departures)
    metrics = join_ontime(con, base_metrics)
    print_build_report(con, base_metrics, ranked, min_departures, investable_hub_sizes)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    con.register("metrics", metrics)
    con.execute(f"COPY metrics TO '{OUT_PATH.as_posix()}' (FORMAT PARQUET)")
    con.close()

    print(f"\nwrote {len(metrics):,} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
