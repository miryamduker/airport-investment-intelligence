"""Download one month of BTS On-Time Performance (ASQP) and aggregate it to
airport-month grain in data/staging/ontime_airport_monthly.parquet.

Row-level access path is the same TranStats ASP.NET form-post used by
ingest/t100.py (see docs/DATA_RECON.md) -- GET DL_SelectFields.aspx for
fresh __VIEWSTATE/__EVENTVALIDATION tokens, then POST the desired field
checkboxes plus year/month. The On-Time Performance table's own ROT13-ish
obfuscated querystring (gnoyr_VQ=FGJ, QO_fu146_anzr=b0-gvzr) was confirmed
by fetching the live form and checking its page header reads "On-Time :
Reporting Carrier On-Time Performance (1987-present)" -- see
docs/DATA_RECON.md's On-Time Performance recon section.

This module only builds the airport-month aggregate and prints a validation
report. Joining it into mart_airport_metrics and wiring it into
capacity_strain happens separately, in scoring/metrics.py.

Run:
    python -m ingest.ontime
    python -m ingest.ontime --year 2026 --month 5
"""
from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

import duckdb
import pandas as pd
import requests

from ingest.common import ca_bundle_verify

DL_URL = "https://transtats.bts.gov/DL_SelectFields.aspx"
# ROT13-ish-obfuscated by BTS: table_ID=FGJ, DB_short_name=On-Time. Confirmed
# by fetching https://transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FGJ&
# QO_fu146_anzr=b0-gvzr and checking the page header (see module docstring).
DL_PARAMS = {"gnoyr_VQ": "FGJ", "QO_fu146_anzr": "b0-gvzr"}

# Just enough fields for the capacity-strain metrics this ingest computes
# (nas_delay_per_departure, taxi_out_p80, pct_delayed_15, cancellation_rate)
# plus identifiers. Not every field on the form -- see docs/DATA_RECON.md's
# field inventory for what else is available if a future metric needs it.
FIELDS = [
    "YEAR", "MONTH",
    "OP_UNIQUE_CARRIER",
    "ORIGIN",
    "DEP_DEL15",
    "TAXI_OUT",
    "CANCELLED",
    "CARRIER_DELAY", "WEATHER_DELAY", "NAS_DELAY",
]

# Same latest-published-month situation as T-100 (docs/DATA_RECON.md).
DEFAULT_YEAR = 2026
DEFAULT_MONTH = 5

RAW_DIR = Path("data/raw")
STAGING_DIR = Path("data/staging")
OUT_PATH = STAGING_DIR / "ontime_airport_monthly.parquet"


def _hidden_field(html: str, field_id: str) -> str:
    m = re.search(rf'id="{field_id}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else ""


def download_ontime(year: int, month: int, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dir / f"ontime_{year}_{month:02d}.zip"
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

    response = session.post(DL_URL, params=DL_PARAMS, data=data, timeout=180)
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


# Unlike T-100 Segment (whose export CSV headers match the DL_SelectFields
# checkbox names exactly), the On-Time Performance export uses BTS's older
# "friendly name" column headers regardless of which FIELDS checkboxes were
# requested -- confirmed by inspecting the actual downloaded CSV header row.
# The checkbox selection also didn't limit which columns came back (the
# export included all ~110 fields, not just the ones requested); harmless
# here since this just selects the ones it needs by their real names below.
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
    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(
            n for n in z.namelist()
            if n.lower().endswith(".csv") and "document" not in n.lower()
        )
        with z.open(csv_name) as f:
            df = pd.read_csv(f, low_memory=False)
    df = df.rename(columns=CSV_COLUMN_RENAME)
    return df[list(CSV_COLUMN_RENAME.values())]


def aggregate_airport_month(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> pd.DataFrame:
    """One row per (ORIGIN, YEAR, MONTH). See module docstring for formulas.

    Denominator convention: nas_delay_per_departure and pct_delayed_15
    divide by completed_departures (CANCELLED = 0), not COUNT(*). A
    cancelled flight isn't a low-delay departure -- it never departed --
    so counting it in the denominator would dilute both metrics and make
    an airport with a lot of cancellations look artificially punctual.
    NAS_DELAY and DEP_DEL15 are null for cancelled flights anyway (they
    never departed), so COALESCE(..., 0) in the numerator is a no-op once
    the denominator is restricted to completed flights.

    cancellation_rate = cancelled / COUNT(*), where COUNT(*) is every OTP
    row for that origin/month (completed or cancelled) -- this is the
    "flights that were supposed to happen" universe, sourced entirely from
    OTP's own row count rather than T-100's DEPARTURES_SCHEDULED, which was
    found unreliable at scale (docs/DATA_RECON.md, completion_gap removal).

    taxi_out_p80 excludes nulls (TAXI_OUT is ~99% populated in the May 2026
    pull) since there's no sensible zero-fill for a taxi time that was
    never recorded.
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

    print("\nnull rates (should roughly match docs/DATA_RECON.md's prior sample):")
    for col in ["TAXI_OUT", "NAS_DELAY", "CARRIER_DELAY", "WEATHER_DELAY", "DEP_DEL15"]:
        null_pct = df[col].isna().mean()
        print(f"  {col:<15s} {null_pct:6.1%} null")

    cancelled_pct = df["CANCELLED"].mean()
    print(f"\ncancelled flights: {cancelled_pct:.1%}")

    print(f"\nairport-month rows in aggregate: {len(agg):,}")
    print("\naggregate metric ranges:")
    for col in [
        "flights", "completed_departures", "nas_delay_per_departure", "taxi_out_p80",
        "pct_delayed_15", "cancellation_rate", "reporting_carrier_count",
    ]:
        print(f"  {col:<26s} min={agg[col].min():>10.3f}  median={agg[col].median():>10.3f}  max={agg[col].max():>10.3f}")

    print("\n10 airports with highest nas_delay_per_departure (min 30 completed departures):")
    top_nas = agg[agg["completed_departures"] >= 30].sort_values("nas_delay_per_departure", ascending=False).head(10)
    for _, row in top_nas.iterrows():
        print(f"  {row['iata']:>4s}  nas_delay/dep={row['nas_delay_per_departure']:6.2f} min  completed={row['completed_departures']:>6,.0f}")

    print("\n10 airports with highest taxi_out_p80 (min 30 completed departures):")
    top_taxi = agg[agg["completed_departures"] >= 30].sort_values("taxi_out_p80", ascending=False).head(10)
    for _, row in top_taxi.iterrows():
        print(f"  {row['iata']:>4s}  taxi_out_p80={row['taxi_out_p80']:6.1f} min  completed={row['completed_departures']:>6,.0f}")

    print("\n10 airports with highest cancellation_rate (min 30 scheduled flights):")
    top_cancel = agg[agg["flights"] >= 30].sort_values("cancellation_rate", ascending=False).head(10)
    for _, row in top_cancel.iterrows():
        print(f"  {row['iata']:>4s}  cancellation_rate={row['cancellation_rate']:6.1%}  flights={row['flights']:>6,.0f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=DEFAULT_YEAR)
    parser.add_argument("--month", type=int, default=DEFAULT_MONTH)
    args = parser.parse_args()

    zip_path = download_ontime(args.year, args.month, RAW_DIR)
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
