"""resolve_airports: free text -> IATA codes.

The one tool that accepts free text. Everything else takes codes from an
enum, so a wrong answer here is the only way the agent can end up analysing
an airport the user didn't mean -- hence the explicit `ambiguous` flag.
"""
from __future__ import annotations

import re

from tools import data as d


def _city_parts(value: str) -> list[str]:
    """OurAirports packs multiple served cities into one municipality field,
    separated by commas or slashes: "Honolulu, Oahu", "Cincinnati / Covington",
    "Greenville/Greer/Spartanburg". Each part is a name a user might type."""
    return [p.strip().upper() for p in re.split(r"[,/]", value or "") if p.strip()]


def _airport_summary(code: str) -> dict | None:
    dim = d.dim_airport_df()
    row = dim[dim["iata"] == code]
    if row.empty:
        return None
    row = row.iloc[0]
    return {
        "code": code,
        "name": row["name"],
        "state": row["state"],
        "region": row["region"],
        "hub_size_cohort": row["hub_size"],
        "has_metrics": code in set(d.metrics_df()["code"]),
    }


def _summaries(codes: list[str]) -> list[dict]:
    return [s for c in codes if (s := _airport_summary(c)) is not None]


def resolve_airports(query: str) -> dict:
    """Match priority: exact IATA code > metro alias > region > state >
    city > airport-name substring."""
    raw_query = query
    upper = query.strip().upper()
    dim = d.dim_airport_df()

    def result(matches: list[dict], ambiguous: bool, match_type: str, conf_value: float, conf_reason: str) -> dict:
        caveats = []
        thin = [m["code"] for m in matches if not m["has_metrics"]]
        if thin:
            caveats.append(
                f"{', '.join(thin)} matched but {'has' if len(thin) == 1 else 'have'} no May 2026 "
                "mart data (unknown to scoring, or below the minimum-departures threshold) -- "
                "not usable by rank_airports/compare_airports/etc."
            )
        if ambiguous:
            caveats.append("Multiple airports match this query -- ask the user which one they mean before calling another tool with a code.")
        return {
            "query": raw_query,
            "match_type": match_type,
            "matches": matches,
            "ambiguous": ambiguous,
            "as_of": d.AS_OF,
            "confidence": {"value": conf_value, "reason": conf_reason},
            "caveats": caveats,
        }

    if len(upper) == 3 and (dim["iata"] == upper).any():
        return result(_summaries([upper]), False, "code", 1.0, "exact IATA code match")

    aliases = d.aliases_config()["metro_aliases"]
    alias_key = next((k for k in aliases if k.upper() == upper), None)
    if alias_key is not None:
        matches = _summaries(aliases[alias_key])
        return result(matches, len(matches) > 1, "metro_alias", 1.0, "curated metro-area alias")

    # regions accept underscores or spaces: "new england" == "new_england"
    region_key = next((r for r in d.region_names() if r.upper() == upper.replace(" ", "_")), None)
    if region_key is not None:
        codes = dim[dim["region"] == region_key]["iata"].tolist()
        return result(_summaries(codes), False, "region", 1.0, "region name match")

    if len(upper) == 2 and (dim["state"] == upper).any():
        codes = dim[dim["state"] == upper]["iata"].tolist()
        return result(_summaries(codes), False, "state", 1.0, "state abbreviation match")

    # City match, before the substring fallback. 39 of the 135 investable
    # hubs are named after a person, not their city -- SJU is "Luis Munoz
    # Marin", LAS is "Harry Reid" -- so substring matching cannot reach them,
    # and for SJU it actively resolved to UGI (San Juan /Uganik/ Seaplane
    # Base, Alaska) instead. Exact city match, not substring, so "Portland"
    # still returns both Portlands rather than silently picking one.
    city_hits = dim[dim["city"].fillna("").map(lambda v: upper in _city_parts(v))]
    if len(city_hits):
        # Narrow to investable hubs first. A city often has one real airport
        # plus general-aviation fields (Cincinnati: CVG and Lunken; Miami: MIA
        # and Opa-locka; San Juan: SJU and Isla Grande). Where exactly one is
        # an investable hub, that is unambiguously the one meant. Where two
        # are -- Portland OR and Portland ME -- it stays ambiguous and the
        # agent asks.
        investable = set(d.scoring_config()["thresholds"]["investable_hub_sizes"])
        hubs = city_hits[city_hits["hub_size"].isin(investable)]
        chosen = hubs if len(hubs) else city_hits
        matches = _summaries(chosen["iata"].tolist()[:8])
        return result(
            matches, len(matches) > 1, "city",
            1.0 if len(matches) == 1 else 0.7,
            "exact city match" if len(matches) == 1 else "several airports serve this city",
        )

    # Prefer name matches that have mart data: a no-data collision (a seaplane
    # base, a private strip) shouldn't turn a real hub's clean match ambiguous.
    name_hits = dim[dim["name"].str.upper().str.contains(upper, na=False, regex=False)]
    scored_hits = name_hits[name_hits["iata"].isin(set(d.metrics_df()["code"]))]
    candidates = scored_hits if len(scored_hits) else name_hits
    if len(candidates) == 1:
        matches = _summaries([candidates.iloc[0]["iata"]])
        return result(matches, False, "name_substring", 0.8, "single airport-name match, not an exact code")
    if len(candidates) > 1:
        matches = _summaries(candidates["iata"].tolist()[:8])
        return result(matches, True, "name_substring", 0.5, "multiple airports match this text")

    return result([], False, "not_found", 0.0, "no match found in the airport dimension")
