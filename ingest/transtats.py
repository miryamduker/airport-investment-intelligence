"""The TranStats download form, shared by ingest/t100.py and ingest/ontime.py.

BTS serves the row-level tables from an ASP.NET form (DL_SelectFields.aspx),
not a REST endpoint, and obfuscates its querystring parameters with ROT13
(gnoyr_VQ -> table_ID). Downloading means doing what the browser does: GET
the page for fresh __VIEWSTATE/__EVENTVALIDATION tokens, then POST the field
checkboxes plus year/month. See docs/DATA_RECON.md.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pandas as pd
import requests

from ingest.common import ca_bundle_verify

DL_URL = "https://transtats.bts.gov/DL_SelectFields.aspx"


def _hidden_field(html: str, field_id: str) -> str:
    match = re.search(rf'id="{field_id}"[^>]*value="([^"]*)"', html)
    return match.group(1) if match else ""


def download_table(
    params: dict[str, str],
    fields: list[str],
    year: int,
    month: int,
    dest: Path,
    timeout: int = 120,
) -> Path:
    """Download one month of one TranStats table as a zip, cached at dest."""
    if dest.exists():
        print(f"raw zip already cached: {dest}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.verify = ca_bundle_verify()

    form_page = session.get(DL_URL, params=params, timeout=30)
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
    data.update({field: "on" for field in fields})

    response = session.post(DL_URL, params=params, data=data, timeout=timeout)
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


def read_csv_from_zip(zip_path: Path) -> pd.DataFrame:
    """The data CSV out of a TranStats zip, skipping its readme."""
    with zipfile.ZipFile(zip_path) as archive:
        csv_name = next(
            n for n in archive.namelist()
            if n.lower().endswith(".csv") and "document" not in n.lower()
        )
        with archive.open(csv_name) as handle:
            return pd.read_csv(handle, low_memory=False)
