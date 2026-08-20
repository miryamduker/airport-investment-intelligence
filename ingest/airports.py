"""Build data/staging/dim_airport.parquet from OurAirports (free CSV) plus
T-100 Segment passenger shares.

Columns: iata code, name, city, state, lat, lon, runway count, region,
hub_size, slot_controlled.

Run:
    python -m ingest.airports
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import requests
import yaml

from ingest.common import ca_bundle_verify, write_parquet

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


def build_airport_dim(
    con: duckdb.DuckDBPyConnection,
    airports_csv: Path,
    runways_csv: Path,
    iso_countries: list[str],
) -> pd.DataFrame:
    countries = ", ".join(f"'{c}'" for c in iso_countries)
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
            -- OurAirports' municipality. Carried because 39 of the investable
            -- hubs have an airport name that contains no city name at all
            -- (LAS = "Harry Reid", IAH = "George Bush Intercontinental",
            -- SJU = "Luis Munoz Marin"), so resolve_airports cannot reach
            -- them by substring. Some values carry a qualifier after a comma
            -- ("Honolulu, Oahu"); the resolver matches on the part before it.
            a.municipality AS city,
            -- For the 50 states + DC the subdivision half of iso_region IS
            -- the state code. For the territories it is a district code
            -- (PR-U-A -> 'U', AS-WT -> 'WT'), so fall back to iso_country,
            -- which is what config/regions.yaml maps under `territories`.
            CASE WHEN a.iso_country = 'US'
                 THEN split_part(a.iso_region, '-', 2)
                 ELSE a.iso_country END AS state,
            a.latitude_deg AS lat,
            a.longitude_deg AS lon,
            -- Left NULL, never 0: "no runway data" and "zero runways" have
            -- to stay distinguishable for runways_per_mpax and confidence.
            r.runway_count AS runway_count
        FROM read_csv_auto('{airports_csv.as_posix()}') a
        LEFT JOIN runway_counts r ON r.airport_ref = a.id
        WHERE a.iso_country IN ({countries})
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
    ingest_config = yaml.safe_load(INGEST_CONFIG_PATH.read_text())
    df = build_airport_dim(con, airports_csv, runways_csv, ingest_config["iso_countries"])
    print(f"US airports with an IATA code: {len(df):,}")

    state_to_region = load_state_region_map(REGIONS_CONFIG_PATH)
    df["region"] = df["state"].map(state_to_region)
    unmapped = df[df["region"].isna()]["state"].unique()
    if len(unmapped):
        print(f"states with no region mapping ({len(unmapped)}): {sorted(unmapped)}")

    df = add_hub_size(con, df, T100_PATH, ingest_config["hub_size"])

    slot_controlled = set(ingest_config["slot_controlled_airports"])
    df["slot_controlled"] = df["iata"].isin(slot_controlled)

    out_df = df[[
        "iata", "name", "city", "state", "region", "lat", "lon",
        "runway_count", "hub_size", "slot_controlled",
    ]].sort_values("iata").reset_index(drop=True)

    out_path = STAGING_DIR / "dim_airport.parquet"
    con.close()
    write_parquet(out_df, out_path)

    print(f"\nwrote {len(out_df):,} rows to {out_path}")
    print("\nhub_size counts:")
    print(out_df["hub_size"].value_counts())
    print(f"\nslot_controlled: {out_df[out_df['slot_controlled']]['iata'].tolist()}")


if __name__ == "__main__":
    main()
