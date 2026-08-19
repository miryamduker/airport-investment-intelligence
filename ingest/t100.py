"""Download one month of BTS T-100 Segment (passenger service only) into
data/staging/t100_segment.parquet.

The row-level T-100 Segment table is not on the Socrata REST API at
data.bts.gov -- see docs/DATA_RECON.md. It lives on the legacy TranStats
system (transtats.bts.gov) behind an ASP.NET download form
(DL_SelectFields.aspx) rather than a REST endpoint. BTS obfuscates that
form's querystring parameters and lookup-table links with ROT13 (e.g.
gnoyr_VQ -> table_ID); this module replicates the form POST the same way
the browser does: GET the page for fresh __VIEWSTATE/__EVENTVALIDATION
tokens, then POST the desired field checkboxes plus year/month.

Run:
    python -m ingest.t100
    python -m ingest.t100 --year 2026 --month 5
"""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import requests
import yaml

from ingest.common import ca_bundle_verify

# --- TranStats access -------------------------------------------------------

DL_URL = "https://transtats.bts.gov/DL_SelectFields.aspx"
# ROT13-obfuscated by BTS: table_ID=FMG, DB_short_name=Air Carriers
DL_PARAMS = {"gnoyr_VQ": "FMG", "QO_fu146_anzr": "Nv4 Pn44vr45"}

# Field checkboxes on DL_SelectFields.aspx, confirmed present by fetching and
# parsing the live form (docs/DATA_RECON.md: "T-100 Segment ingest path
# resolved"). This is the full carrier x origin x dest x month grain the
# thesis needs, including the PASSENGERS/SEATS/CLASS/AIRCRAFT_CONFIG columns
# that were NOT present in the earlier stale PREZIP sample.
FIELDS = [
    "UNIQUE_CARRIER", "UNIQUE_CARRIER_NAME",
    "ORIGIN_AIRPORT_ID", "ORIGIN", "ORIGIN_CITY_NAME", "ORIGIN_STATE_ABR",
    "DEST_AIRPORT_ID", "DEST", "DEST_CITY_NAME", "DEST_STATE_ABR",
    "AIRCRAFT_GROUP", "AIRCRAFT_TYPE", "AIRCRAFT_CONFIG", "CLASS",
    "YEAR", "QUARTER", "MONTH", "DISTANCE", "DISTANCE_GROUP",
    "DEPARTURES_SCHEDULED", "DEPARTURES_PERFORMED", "SEATS", "PASSENGERS",
    "FREIGHT", "MAIL", "DATA_SOURCE",
]

# Latest month confirmed published as of this recon (2026-08-19): May 2026
# downloads real data, June 2026 returns the form's HTML error page instead
# of a zip, i.e. not yet released. Override with --year/--month once later
# months are published.
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
STRING_COLUMNS = [
    "UNIQUE_CARRIER", "UNIQUE_CARRIER_NAME", "ORIGIN", "ORIGIN_CITY_NAME",
    "ORIGIN_STATE_ABR", "DEST", "DEST_CITY_NAME", "DEST_STATE_ABR", "CLASS",
    "DATA_SOURCE",
]


def _hidden_field(html: str, field_id: str) -> str:
    m = re.search(rf'id="{field_id}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else ""


def download_t100(year: int, month: int, raw_dir: Path) -> Path:
    """Download one month of T-100 Segment as a zip, caching in raw_dir."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / f"t100_segment_{year}_{month:02d}.zip"
    if dest.exists():
        print(f"raw zip already cached: {dest}")
        return dest

    session = requests.Session()
    session.verify = ca_bundle_verify()

    form_page = session.get(DL_URL, params=DL_PARAMS, timeout=30)
    form_page.raise_for_status()
    html = form_page.text

    data = {
        "__VIEWSTATE": _hidden_field(html, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _hidden_field(html, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _hidden_field(html, "__EVENTVALIDATION"),
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "cboGeography": "All",
        "cboYear": str(year),
        "cboPeriod": str(month),
        "chkDownloadZip": "on",
        "btnDownload": "Download",
    }
    for field in FIELDS:
        data[field] = "on"

    response = session.post(DL_URL, params=DL_PARAMS, data=data, timeout=120)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    if "zip" not in content_type:
        raise RuntimeError(
            f"expected a zip download for {year}-{month:02d}, got "
            f"Content-Type={content_type!r}. Most likely that month isn't "
            f"published yet on TranStats -- try an earlier --month."
        )

    dest.write_bytes(response.content)
    print(f"downloaded {dest} ({len(response.content):,} bytes)")
    return dest


# --- staging -----------------------------------------------------------------


def load_passenger_classes(config_path: Path) -> list[str]:
    config = yaml.safe_load(config_path.read_text())
    return config["t100_segment"]["passenger_service_classes"]


def read_segment_csv(zip_path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(
            n for n in z.namelist()
            if n.lower().endswith(".csv") and "document" not in n.lower()
        )
        with z.open(csv_name) as f:
            return pd.read_csv(f, low_memory=False)


def apply_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in NUMERIC_INT_COLUMNS:
        df[col] = df[col].astype("int64")
    for col in STRING_COLUMNS:
        df[col] = df[col].astype("string")
    # state abbreviations are legitimately null for international routes
    for col in ("ORIGIN_STATE_ABR", "DEST_STATE_ABR"):
        df[col] = df[col].astype("string")
    return df


def write_parquet(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY df TO '{out_path.as_posix()}' (FORMAT PARQUET)")
    con.close()


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

    zip_path = download_t100(args.year, args.month, RAW_DIR)

    df_raw = read_segment_csv(zip_path)
    passenger_classes = load_passenger_classes(CONFIG_PATH)
    df_filtered = df_raw[df_raw["CLASS"].isin(passenger_classes)].reset_index(drop=True)

    df_typed = apply_types(df_filtered)

    out_path = STAGING_DIR / "t100_segment.parquet"
    write_parquet(df_typed, out_path)
    print(f"\nwrote {len(df_typed):,} rows to {out_path}")

    print_validation_report(df_raw, df_typed)


if __name__ == "__main__":
    main()
