"""Download one month of BTS On-Time Performance (ASQP) and aggregate it to
airport-month grain in data/staging/ontime_airport_monthly.parquet.

Access path is the shared TranStats form-post -- see ingest/transtats.py.
Joining this into the mart happens separately, in scoring/metrics.py.

Run:
    python -m ingest.ontime
    python -m ingest.ontime --year 2026 --month 5
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import pandas as pd

from ingest.transtats import download_table, read_csv_from_zip

# ROT13-ish-obfuscated by BTS: table_ID=FGJ, DB_short_name=On-Time.
DL_PARAMS = {"gnoyr_VQ": "FGJ", "QO_fu146_anzr": "b0-gvzr"}

FIELDS = [
    "YEAR", "MONTH",
    "OP_UNIQUE_CARRIER",
    "ORIGIN",
    "DEP_DEL15",
    "TAXI_OUT",
    "CANCELLED",
    "CARRIER_DELAY", "WEATHER_DELAY", "NAS_DELAY",
]

DEFAULT_YEAR = 2026
DEFAULT_MONTH = 5

RAW_DIR = Path("data/raw")
STAGING_DIR = Path("data/staging")
OUT_PATH = STAGING_DIR / "ontime_airport_monthly.parquet"

# The On-Time export uses BTS's older "friendly name" headers regardless of
# which checkboxes were requested, unlike T-100 whose headers match exactly.
CSV_COLUMN_RENAME = {
    "Year": "YEAR",
    "Month": "MONTH",
    "Reporting_Airline": "OP_UNIQUE_CARRIER",
    "Origin": "ORIGIN",
    "DepDel15": "DEP_DEL15",
    "TaxiOut": "TAXI_OUT",
    "Cancelled": "CANCELLED",
    "CarrierDelay": "CARRIER_DELAY",
    "WeatherDelay": "WEATHER_DELAY",
    "NASDelay": "NAS_DELAY",
}


def read_ontime_csv(zip_path: Path) -> pd.DataFrame:
    df = read_csv_from_zip(zip_path).rename(columns=CSV_COLUMN_RENAME)
    return df[list(CSV_COLUMN_RENAME.values())]


def aggregate_airport_month(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    """One row per (ORIGIN, YEAR, MONTH).

    nas_delay_per_departure and pct_delayed_15 divide by completed
    departures, not all flights: a cancelled flight never departed, so
    counting it would make a cancellation-heavy airport look punctual.
    cancellation_rate uses every OTP row instead -- the "supposed to happen"
    universe -- sourced from OTP's own row count rather than T-100's
    DEPARTURES_SCHEDULED, which proved unreliable (docs/DATA_RECON.md).
    """
    con.register("otp", df)
    return con.execute(
        """
        SELECT
            ORIGIN AS iata,
            YEAR AS year,
            MONTH AS month,
            COUNT(*) AS flights,
            SUM(CASE WHEN CANCELLED = 0 THEN 1 ELSE 0 END) AS completed_departures,
            SUM(COALESCE(NAS_DELAY, 0))
                / NULLIF(SUM(CASE WHEN CANCELLED = 0 THEN 1 ELSE 0 END), 0)
                AS nas_delay_per_departure,
            PERCENTILE_CONT(0.8) WITHIN GROUP (ORDER BY TAXI_OUT) AS taxi_out_p80,
            SUM(CASE WHEN DEP_DEL15 = 1 THEN 1 ELSE 0 END)::DOUBLE
                / NULLIF(SUM(CASE WHEN CANCELLED = 0 THEN 1 ELSE 0 END), 0)
                AS pct_delayed_15,
            SUM(CANCELLED)::DOUBLE / COUNT(*) AS cancellation_rate,
            COUNT(DISTINCT OP_UNIQUE_CARRIER) AS reporting_carrier_count
        FROM otp
        GROUP BY ORIGIN, YEAR, MONTH
        ORDER BY iata
        """
    ).df()


def print_validation_report(df: pd.DataFrame, agg: pd.DataFrame) -> None:
    print("=" * 70)
    print("ON-TIME PERFORMANCE VALIDATION REPORT")
    print("=" * 70)

    print(f"\nflight-level rows downloaded: {len(df):,}")
    print(f"distinct origins:             {df['ORIGIN'].nunique():,}")
    print(f"year/month values present:    {sorted(df['YEAR'].unique())} / {sorted(df['MONTH'].unique())}")

    print("\nnull rates:")
    for col in ["TAXI_OUT", "NAS_DELAY", "CARRIER_DELAY", "WEATHER_DELAY", "DEP_DEL15"]:
        print(f"  {col:<15s} {df[col].isna().mean():6.1%} null")

    print(f"\ncancelled flights: {df['CANCELLED'].mean():.1%}")

    print(f"\nairport-month rows in aggregate: {len(agg):,}")
    print("\naggregate metric ranges:")
    for col in [
        "flights", "completed_departures", "nas_delay_per_departure", "taxi_out_p80",
        "pct_delayed_15", "cancellation_rate", "reporting_carrier_count",
    ]:
        print(f"  {col:<26s} min={agg[col].min():>10.3f}  median={agg[col].median():>10.3f}  max={agg[col].max():>10.3f}")

    busy = agg[agg["completed_departures"] >= 30]
    for metric, label in [("nas_delay_per_departure", "nas_delay/dep"), ("taxi_out_p80", "taxi_out_p80")]:
        print(f"\n10 airports with highest {metric} (min 30 completed departures):")
        for _, row in busy.sort_values(metric, ascending=False).head(10).iterrows():
            print(f"  {row['iata']:>4s}  {label}={row[metric]:6.2f} min  completed={row['completed_departures']:>6,.0f}")

    print("\n10 airports with highest cancellation_rate (min 30 scheduled flights):")
    for _, row in agg[agg["flights"] >= 30].sort_values("cancellation_rate", ascending=False).head(10).iterrows():
        print(f"  {row['iata']:>4s}  cancellation_rate={row['cancellation_rate']:6.1%}  flights={row['flights']:>6,.0f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--month", type=int, default=DEFAULT_MONTH)
    args = parser.parse_args()

    zip_path = download_table(
        DL_PARAMS, FIELDS, args.year, args.month,
        RAW_DIR / f"ontime_{args.year}_{args.month:02d}.zip",
        timeout=180,
    )
    df = read_ontime_csv(zip_path)

    con = duckdb.connect()
    agg = aggregate_airport_month(con, df)
    print_validation_report(df, agg)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    con.register("agg_out", agg)
    con.execute(f"COPY agg_out TO '{OUT_PATH.as_posix()}' (FORMAT PARQUET)")
    con.close()

    print(f"\nwrote {len(agg):,} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
