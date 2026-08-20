"""Descriptive, non-scoring tool: flight_mix (T-100 route/carrier
composition).

It does not feed the scoring model. Like every other tool in this system it
reads only the frozen May 2026 dataset -- there is no live data source.
"""
from __future__ import annotations

import pandas as pd

from tools import data as d
from tools import format as f

HAUL_CAVEATS = [
    "\"Long haul\" has no binding industry definition -- IATA classifies by flight time, the US EPA "
    "uses >2,300 miles, carriers set their own. These bands use the cutoffs in config/haul.yaml "
    "(long_haul >= 2,500 statute miles); a different cutoff gives a materially different percentage.",
    "Bands are measured on T-100's per-segment DISTANCE (great-circle miles between the two airports), "
    "not flight time, and describe individual segments -- a connecting itinerary is counted as its "
    "separate legs, never as one long-haul trip.",
    "All-cargo service (CLASS 'G' and 'P') is excluded, per the passenger-service filter in "
    "config/ingest.yaml. This matters most at freight hubs: including cargo would put Anchorage's "
    "long-haul share at 40.2% of departures instead of 9.6%, measuring freighters rather than "
    "passenger service.",
]



def _origin_totals(con, code: str) -> pd.Series:
    return con.execute(
        f"""
        SELECT
            SUM(PASSENGERS) AS passengers,
            SUM(SEATS) AS seats,
            SUM(DEPARTURES_PERFORMED) AS departures_performed,
            COUNT(DISTINCT DEST) AS distinct_destinations,
            SUM(CASE WHEN CLASS = 'L' THEN PASSENGERS ELSE 0 END) AS charter_passengers
        FROM read_parquet('{d.T100_PATH.as_posix()}')
        WHERE ORIGIN = ?
        """,
        [code],
    ).df().iloc[0]


def _top_by_passengers(con, code: str, group_col: str, name_col: str) -> pd.DataFrame:
    return con.execute(
        f"""
        SELECT {group_col} AS key, ANY_VALUE({name_col}) AS label,
               SUM(PASSENGERS) AS passengers, SUM(DEPARTURES_PERFORMED) AS departures
        FROM read_parquet('{d.T100_PATH.as_posix()}')
        WHERE ORIGIN = ?
        GROUP BY {group_col}
        ORDER BY passengers DESC
        LIMIT 10
        """,
        [code],
    ).df()


def _haul_band_case(bands: list[dict]) -> str:
    """SQL CASE assigning each T-100 row to exactly one band from
    config/haul.yaml. Half-open [min_miles, max_miles); null max is
    unbounded."""
    clauses = []
    for band in bands:
        low = float(band["min_miles"])
        high = band["max_miles"]
        bound = f"DISTANCE >= {low}" if high is None else f"DISTANCE >= {low} AND DISTANCE < {float(high)}"
        clauses.append(f"WHEN {bound} THEN '{band['name']}'")
    return "CASE " + " ".join(clauses) + " END"


def _haul_totals(con, code: str, bands: list[dict]) -> pd.DataFrame:
    """One row per band actually present for this origin, indexed by band
    name. Bands with no traffic are absent and filled in by the caller."""
    return con.execute(
        f"""
        SELECT {_haul_band_case(bands)} AS band,
               SUM(DEPARTURES_PERFORMED) AS departures,
               SUM(PASSENGERS) AS passengers,
               SUM(SEATS) AS seats,
               COUNT(DISTINCT DEST) AS distinct_destinations,
               SUM(CASE WHEN PASSENGERS = 0 THEN DEPARTURES_PERFORMED ELSE 0 END)
                   AS zero_passenger_departures
        FROM read_parquet('{d.T100_PATH.as_posix()}')
        WHERE ORIGIN = ?
        GROUP BY 1
        """,
        [code],
    ).df().set_index("band")


def flight_mix(code: str) -> dict:
    """Carrier and route composition for one origin, straight from T-100
    Segment -- descriptive only, never a scoring input."""
    code = code.upper()
    con = d.t100_connection()
    try:
        totals = _origin_totals(con, code)
        if pd.isna(totals["departures_performed"]) or totals["departures_performed"] == 0:
            return {
                "code": code,
                "found": False,
                "as_of": d.AS_OF,
                "confidence": {"value": 0.0, "reason": "no May 2026 T-100 rows for this origin"},
                "caveats": ["Unknown IATA code, or zero passenger-service departures from this origin in May 2026."],
            }
        top_destinations = _top_by_passengers(con, code, "DEST", "DEST_CITY_NAME")
        top_carriers = _top_by_passengers(con, code, "UNIQUE_CARRIER", "UNIQUE_CARRIER_NAME")
        bands = d.haul_config()["bands"]
        haul_totals = _haul_totals(con, code, bands)
    finally:
        con.close()

    total_passengers = float(totals["passengers"])
    total_departures = float(totals["departures_performed"])
    total_seats = float(totals["seats"])

    def share(passengers: float) -> float | None:
        return f.num(passengers / total_passengers, 4) if total_passengers else None

    full_departures = d.scoring_config()["confidence"]["full_confidence_departures"]

    def band_share(value: float, total: float) -> float | None:
        return f.num(value / total, 4) if total else None

    # Every configured band is reported, including ones with no traffic, so
    # the model never has to infer that a missing band means zero.
    haul_mix = []
    for band in bands:
        row = haul_totals.loc[band["name"]] if band["name"] in haul_totals.index else None
        departures = float(row["departures"]) if row is not None else 0.0
        passengers = float(row["passengers"]) if row is not None else 0.0
        seats = float(row["seats"]) if row is not None else 0.0
        haul_mix.append({
            "band": band["name"],
            "min_miles": band["min_miles"],
            "max_miles": band["max_miles"],
            "departures": f.integer(departures),
            "share_of_departures": band_share(departures, total_departures),
            "passengers": f.integer(passengers),
            "share_of_passengers": band_share(passengers, total_passengers),
            "seats": f.integer(seats),
            "share_of_seats": band_share(seats, total_seats),
            "distinct_destinations": f.integer(row["distinct_destinations"]) if row is not None else 0,
            "zero_passenger_departures": f.integer(row["zero_passenger_departures"]) if row is not None else 0,
        })

    return {
        "code": code,
        "found": True,
        "total_passengers": f.integer(total_passengers),
        "total_seats": f.integer(total_seats),
        "total_departures_performed": f.integer(total_departures),
        "distinct_destinations": f.integer(totals["distinct_destinations"]),
        "avg_seats_per_departure": f.num(total_seats / total_departures, 1) if total_departures else None,
        "charter_passenger_share": share(totals["charter_passengers"]),
        "haul_mix": haul_mix,
        "top_destinations": [
            {
                "dest": r.key,
                "dest_city": r.label,
                "passengers": f.integer(r.passengers),
                "departures": f.integer(r.departures),
                "share_of_origin_passengers": share(r.passengers),
            }
            for r in top_destinations.itertuples()
        ],
        "top_carriers": [
            {
                "carrier": r.key,
                "carrier_name": r.label,
                "passengers": f.integer(r.passengers),
                "departures": f.integer(r.departures),
                "share_of_origin_passengers": share(r.passengers),
            }
            for r in top_carriers.itertuples()
        ],
        "as_of": d.AS_OF,
        "confidence": {
            "value": round(min(total_departures / full_departures, 1.0), 2),
            "reason": f"{int(total_departures):,} May 2026 departures from this origin",
        },
        "caveats": [
            d.CAVEAT_SINGLE_MONTH,
            d.CAVEAT_CLASS_L_INCLUDED,
            "avg_seats_per_departure is a descriptive figure only -- it is NOT the scoring model's "
            "(unimplemented) upgauge_gap metric, which would need multi-month history to detect "
            "underuse of larger aircraft relative to route demand.",
            *HAUL_CAVEATS,
        ],
    }
