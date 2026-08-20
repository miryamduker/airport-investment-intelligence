"""Cross-airport tools: rank_airports, compare_airports."""
from __future__ import annotations

import pandas as pd

from tools import data as d
from tools import format as f


def rank_airports(region: str, profile: str, top_n: int) -> dict:
    """Score nationally, then filter by region -- never the other way round
    (CLAUDE.md: "Never rank within a filtered subset"). Each row carries its
    own cohort label, since a mixed-cohort ranking is not comparable
    row-to-row.
    """
    investable = d.scored_investable(profile)
    pool = investable if region == "national" else investable[investable["region"] == region]
    top = pool.head(top_n)

    results = []
    for rank, row in enumerate(top.itertuples(), start=1):
        pillars = {p: getattr(row, p) for p in d.pillar_names()}
        results.append({
            "rank": rank,
            "code": row.code,
            "name": row.name,
            "state": row.state,
            "region": row.region,
            "hub_size_cohort": row.hub_size,
            "slot_controlled": f.boolean(row.slot_controlled),
            "composite": f.num(row.composite, 1),
            "pillars": {p: f.num(v, 1) for p, v in pillars.items()},
            "unavailable_pillars": [p for p, v in pillars.items() if pd.isna(v)],
            "confidence": f.confidence(row.confidence),
        })

    caveats = [
        d.CAVEAT_COHORT_PERCENTILE,
        d.CAVEAT_GROWTH_MISSING,
        d.CAVEAT_LOAD_FACTOR_ONLY,
        d.CAVEAT_INVESTABILITY_FLOOR,
        d.CAVEAT_SINGLE_MONTH,
    ]
    if not results:
        caveats.append(f"No investable airports found for region={region}.")

    return {
        "region": region,
        "profile": profile,
        "weights_used": d.scoring_config()["profiles"][profile],
        "top_n_requested": top_n,
        "total_investable_airports_considered": len(pool),
        "results": results,
        "as_of": d.AS_OF,
        "confidence": {"value": None, "reason": "confidence is reported per airport (see each result's confidence field), not as a single ranking-wide number"},
        "caveats": caveats,
    }


def compare_airports(codes: list[str], dimension: str, profile: str = "general") -> dict:
    """2-5 airports, ONE dimension. Raw values are primary; percentiles are
    secondary and cohort-relative, so they are not comparable across cohorts.
    """
    codes = [c.upper() for c in codes]
    mart = d.metrics_df()
    known = set(mart["code"])
    valid_codes = [c for c in codes if c in known]
    not_found = [c for c in codes if c not in known]

    results = []
    cohorts: set[str] = set()

    if dimension == "composite":
        scored = d.scored(profile)
        sub = scored[scored["code"].isin(valid_codes)].set_index("code").loc[valid_codes].reset_index()
        for row in sub.itertuples():
            cohorts.add(row.hub_size)
            results.append({
                "code": row.code,
                "name": row.name,
                "hub_size_cohort": row.hub_size,
                "composite": f.num(row.composite, 1),
                "confidence": f.confidence(row.confidence),
            })
    else:
        meta = d.RAW_METRIC_COLUMNS[dimension]
        for code in valid_codes:
            idx = d.row_index_for_code(code)
            mart_row = mart.iloc[idx]
            cohorts.add(mart_row["hub_size"])
            results.append({
                "code": code,
                "name": mart_row["name"],
                "hub_size_cohort": mart_row["hub_size"],
                "raw_value": f.num(mart_row[dimension], 4),
                "unit": meta["unit"],
                "percentile_within_cohort": f.num(d.percentiles().iloc[idx].get(f"{dimension}_pctile"), 1),
            })

    caveats = [d.CAVEAT_COHORT_PERCENTILE]
    if len(cohorts) > 1:
        caveats.append(
            "These airports are in different hub_size cohorts -- their percentiles are each "
            "relative to a different peer set and are not directly comparable to each other; "
            "raw values are apples-to-apples, percentiles are not."
        )
    if dimension == "composite":
        caveats.extend([d.CAVEAT_GROWTH_MISSING, d.CAVEAT_LOAD_FACTOR_ONLY])
    else:
        polarity = d.scoring_config()["pillars"][meta["pillar"]]["metrics"][dimension]["polarity"]
        direction = "the SAME direction as" if polarity == 1 else "the OPPOSITE direction from"
        higher_pctile_means = meta["higher_raw_means"] if polarity == 1 else f"LESS {meta['higher_raw_means']}"
        caveats.append(
            f"percentile_within_cohort moves in {direction} the raw value for {dimension}: a higher percentile "
            f"here means {higher_pctile_means} (relative to cohort peers). Do not read a higher percentile as "
            "'better' or 'less congested' in the everyday sense -- it is a ranking on this specific raw metric only."
        )
    if not_found:
        caveats.append(f"No mart data for: {', '.join(not_found)} (unknown code, or excluded by the minimum-departures threshold).")

    return {
        "codes_requested": codes,
        "dimension": dimension,
        "profile": profile if dimension == "composite" else None,
        "results": results,
        "as_of": d.AS_OF,
        "confidence": {"value": None, "reason": "confidence is reported per airport where applicable (composite dimension only)"},
        "caveats": caveats,
    }
