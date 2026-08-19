"""Build data/staging/dim_airport.parquet from OurAirports (free CSV) plus
T-100 Segment passenger shares.

Columns: iata code, name, state, lat, lon, runway count, region, hub_size,
slot_controlled.

Run:
    python -m ingest.airports
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import requests
import yaml

from ingest.common import ca_bundle_verify

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

RAW_DIR = Path("data/raw")
STAGING_DIR = Path("data/staging")
INGEST_CONFIG_PATH = Path("config/ingest.yaml")
REGIONS_CONFIG_PATH = Path("config/regions.yaml")
T100_PATH = STAGING_DIR / "t100_segment.parquet"


def download(url: str, dest: Path) -> Path:
    if dest.exists():
        print(f"raw file already cached: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(url, timeout=60, verify=ca_bundle_verify())
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    print(f"downloaded {dest} ({len(resp.content):,} bytes)")
    return dest


def load_state_region_map(path: Path) -> dict[str, str]:
    config = yaml.safe_load(path.read_text())
    state_to_region: dict[str, str] = {}
    for region, states in config["regions"].items():
        for state in states:
            state_to_region[state] = region
    return state_to_region


def build_airport_dim(con: duckdb.DuckDBPyConnection, airports_csv: Path, runways_csv: Path) -> pd.DataFrame:
    df = con.execute(
        f"""
        WITH runway_counts AS (
            SELECT airport_ref, COUNT(*) AS runway_count
            FROM read_csv_auto('{runways_csv.as_posix()}')
            WHERE closed = 0
            GROUP BY airport_ref
        )
        SELECT
            a.iata_code AS iata,
            a.name,
            split_part(a.iso_region, '-', 2) AS state,
            a.latitude_deg AS lat,
            a.longitude_deg AS lon,
            -- NULL (not 0) when no runways.csv row matches this airport:
            -- that means "no runway data found," not "confirmed zero
            -- runways," and the two need to stay distinguishable downstream
            -- (scoring/score.py's confidence calc, runways_per_mpax) --
            -- COALESCE-ing to 0 here would silently claim a hard runway
            -- count of 0 for airports OurAirports has no runway record for.
            r.runway_count AS runway_count
        FROM read_csv_auto('{airports_csv.as_posix()}') a
        LEFT JOIN runway_counts r ON r.airport_ref = a.id
        WHERE a.iso_country = 'US'
          AND a.iata_code IS NOT NULL
          AND a.iata_code != ''
        """
    ).df()
    return df


def add_hub_size(con: duckdb.DuckDBPyConnection, df: pd.DataFrame, t100_path: Path, thresholds: dict) -> pd.DataFrame:
    shares = con.execute(
        f"""
        WITH by_origin AS (
            SELECT ORIGIN AS iata, SUM(PASSENGERS) AS passengers
            FROM read_parquet('{t100_path.as_posix()}')
            GROUP BY ORIGIN
        ),
        national AS (
            SELECT SUM(passengers) AS total FROM by_origin
        )
        SELECT iata, passengers, passengers / national.total AS pax_share
        FROM by_origin, national
        """
    ).df()

    df = df.merge(shares, on="iata", how="left")

    def classify(share: float) -> str:
        if pd.isna(share):
            return "non_hub"
        if share >= thresholds["large_min_share"]:
            return "large"
        if share >= thresholds["medium_min_share"]:
            return "medium"
        if share >= thresholds["small_min_share"]:
            return "small"
        return "non_hub"

    df["hub_size"] = df["pax_share"].apply(classify)
    return df


def main() -> None:
    airports_csv = download(AIRPORTS_URL, RAW_DIR / "ourairports_airports.csv")
    runways_csv = download(RUNWAYS_URL, RAW_DIR / "ourairports_runways.csv")

    con = duckdb.connect()
    df = build_airport_dim(con, airports_csv, runways_csv)
    print(f"US airports with an IATA code: {len(df):,}")

    state_to_region = load_state_region_map(REGIONS_CONFIG_PATH)
    df["region"] = df["state"].map(state_to_region)
    unmapped = df[df["region"].isna()]["state"].unique()
    if len(unmapped):
        print(f"states with no region mapping ({len(unmapped)}): {sorted(unmapped)}")

    ingest_config = yaml.safe_load(INGEST_CONFIG_PATH.read_text())
    df = add_hub_size(con, df, T100_PATH, ingest_config["hub_size"])

    slot_controlled = set(ingest_config["slot_controlled_airports"])
    df["slot_controlled"] = df["iata"].isin(slot_controlled)

    out_df = df[[
        "iata", "name", "state", "region", "lat", "lon", "runway_count",
        "hub_size", "slot_controlled",
    ]].sort_values("iata").reset_index(drop=True)

    out_path = STAGING_DIR / "dim_airport.parquet"
    con.register("out_df", out_df)
    con.execute(f"COPY out_df TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    con.close()

    print(f"\nwrote {len(out_df):,} rows to {out_path}")
    print("\nhub_size counts:")
    print(out_df["hub_size"].value_counts())
    print(f"\nslot_controlled: {out_df[out_df['slot_controlled']]['iata'].tolist()}")


if __name__ == "__main__":
    main()
