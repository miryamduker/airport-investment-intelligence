"""Download one month of BTS T-100 Segment (passenger service only) into
data/staging/t100_segment.parquet.

Access path is the shared TranStats form-post -- see ingest/transtats.py.

Run:
    python -m ingest.t100
    python -m ingest.t100 --year 2026 --month 5
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
import yaml

from ingest.common import write_parquet
from ingest.transtats import download_table, read_csv_from_zip

# ROT13-obfuscated by BTS: table_ID=FMG, DB_short_name=Air Carriers
DL_PARAMS = {"gnoyr_VQ": "FMG", "QO_fu146_anzr": "Nv4 Pn44vr45"}

FIELDS = [
    "UNIQUE_CARRIER", "UNIQUE_CARRIER_NAME",
    "ORIGIN_AIRPORT_ID", "ORIGIN", "ORIGIN_CITY_NAME", "ORIGIN_STATE_ABR",
    "DEST_AIRPORT_ID", "DEST", "DEST_CITY_NAME", "DEST_STATE_ABR",
    "AIRCRAFT_GROUP", "AIRCRAFT_TYPE", "AIRCRAFT_CONFIG", "CLASS",
    "YEAR", "QUARTER", "MONTH", "DISTANCE", "DISTANCE_GROUP",
    "DEPARTURES_SCHEDULED", "DEPARTURES_PERFORMED", "SEATS", "PASSENGERS",
    "FREIGHT", "MAIL", "DATA_SOURCE",
]

# Latest month published as of 2026-08-19; June 2026 returns the form's error
# page instead of a zip. Override with --year/--month.
DEFAULT_YEAR = 2026
DEFAULT_MONTH = 5

RAW_DIR = Path("data/raw")
STAGING_DIR = Path("data/staging")
CONFIG_PATH = Path("config/ingest.yaml")

NUMERIC_INT_COLUMNS = [
    "ORIGIN_AIRPORT_ID", "DEST_AIRPORT_ID", "AIRCRAFT_GROUP", "AIRCRAFT_TYPE",
    "AIRCRAFT_CONFIG", "YEAR", "QUARTER", "MONTH", "DISTANCE_GROUP",
    "DEPARTURES_SCHEDULED", "DEPARTURES_PERFORMED", "SEATS", "PASSENGERS",
    "FREIGHT", "MAIL", "DISTANCE",
]
# State abbreviations are legitimately null for international routes, so these
# stay nullable strings rather than being filled.
STRING_COLUMNS = [
    "UNIQUE_CARRIER", "UNIQUE_CARRIER_NAME", "ORIGIN", "ORIGIN_CITY_NAME",
    "ORIGIN_STATE_ABR", "DEST", "DEST_CITY_NAME", "DEST_STATE_ABR", "CLASS",
    "DATA_SOURCE",
]


def load_passenger_classes(config_path: Path) -> list[str]:
    return yaml.safe_load(config_path.read_text())["t100_segment"]["passenger_service_classes"]


def apply_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in NUMERIC_INT_COLUMNS:
        df[col] = df[col].astype("int64")
    for col in STRING_COLUMNS:
        df[col] = df[col].astype("string")
    return df


def print_validation_report(df_before: pd.DataFrame, df_after: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("VALIDATION REPORT")
    print("=" * 70)

    print(f"\nrows before passenger filter: {len(df_before):,}")
    print(f"rows after passenger filter:  {len(df_after):,}")
    print(f"rows removed:                 {len(df_before) - len(df_after):,}")

    print(f"\ndistinct origin airports: {df_after['ORIGIN'].nunique():,}")

    total_pax = df_after["PASSENGERS"].sum()
    total_seats = df_after["SEATS"].sum()
    load_factor = total_pax / total_seats if total_seats else float("nan")
    print(f"\ntotal passengers: {total_pax:,}")
    print(f"total seats:      {total_seats:,}")
    print(f"implied national load factor: {load_factor:.1%}")

    print("\n10 busiest origins by departures performed:")
    busiest = (
        df_after.groupby("ORIGIN")["DEPARTURES_PERFORMED"]
        .sum()
        .sort_values(ascending=False)
        .head(10)
    )
    for origin, departures in busiest.items():
        print(f"  {origin:>4s}  {departures:>8,.0f}")

    print("\norigins where total passengers > total seats (bug check):")
    by_origin = df_after.groupby("ORIGIN")[["PASSENGERS", "SEATS"]].sum()
    bad = by_origin[by_origin["PASSENGERS"] > by_origin["SEATS"]]
    if bad.empty:
        print("  none")
    else:
        for origin, row in bad.iterrows():
            print(f"  {origin:>4s}  passengers={row['PASSENGERS']:,.0f}  seats={row['SEATS']:,.0f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--month", type=int, default=DEFAULT_MONTH)
    args = parser.parse_args()

    zip_path = download_table(
        DL_PARAMS, FIELDS, args.year, args.month,
        RAW_DIR / f"t100_segment_{args.year}_{args.month:02d}.zip",
    )

    df_raw = read_csv_from_zip(zip_path)
    passenger_classes = load_passenger_classes(CONFIG_PATH)
    df_filtered = df_raw[df_raw["CLASS"].isin(passenger_classes)].reset_index(drop=True)
    df_typed = apply_types(df_filtered)

    out_path = STAGING_DIR / "t100_segment.parquet"
    write_parquet(df_typed, out_path)
    print(f"\nwrote {len(df_typed):,} rows to {out_path}")

    print_validation_report(df_raw, df_typed)


if __name__ == "__main__":
    main()
