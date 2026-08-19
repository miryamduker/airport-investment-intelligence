"""resolve_airports: free text -> IATA codes.

The one tool that accepts free text. Everything else takes codes from an
enum, so a wrong answer here is the only way the agent can end up analysing
an airport the user didn't mean -- hence the explicit `ambiguous` flag.
"""
from __future__ import annotations

from tools import data as d


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
    airport-name substring."""
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
