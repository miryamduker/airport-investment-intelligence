"""Single-airport tools: airport_profile, diagnose_unmet_demand,
explain_score.

All three read the same frozen percentile/pillar tables; they differ in what
they show -- a snapshot, a bottleneck label, or the weighting arithmetic.
"""
from __future__ import annotations

import pandas as pd

from tools import data as d
from tools import format as f

UNKNOWN_CODE = "Unknown IATA code, or excluded by the minimum-departures threshold."


def _not_found(code: str, detail: str) -> dict:
    return {
        "code": code,
        "found": False,
        "as_of": d.AS_OF,
        "confidence": dict(f.NO_MART_DATA),
        "caveats": [detail],
    }


def airport_profile(code: str) -> dict:
    """Raw metrics, cohort percentiles, pillar scores, and the composite
    under all three profiles."""
    code = code.upper()
    idx = d.row_index_for_code(code)
    if idx is None:
        return _not_found(
            code,
            "Unknown IATA code, or excluded by the minimum-departures threshold "
            "(config/scoring.yaml: thresholds.min_departures_threshold).",
        )

    mart_row = d.metrics_df().iloc[idx]
    pct_row = d.percentiles().iloc[idx]
    pillar_row = d.pillar_scores().iloc[idx]

    raw_metrics = {
        "passengers": f.integer(mart_row["passengers"]),
        "seats": f.integer(mart_row["seats"]),
        "departures_performed": f.integer(mart_row["departures_performed"]),
        "distinct_destinations": f.integer(mart_row["distinct_destinations"]),
        "load_factor": f.num(mart_row["load_factor"], 4),
        "runway_count": f.integer(mart_row["runway_count"]),
        "runways_per_mpax": f.num(mart_row["runways_per_mpax"], 4),
        "nas_delay_per_departure": f.num(mart_row["nas_delay_per_departure"], 2),
        "taxi_out_p80": f.num(mart_row["taxi_out_p80"], 2),
        "pct_delayed_15": f.num(mart_row["pct_delayed_15"], 4),
        "cancellation_rate": f.num(mart_row["cancellation_rate"], 4),
        "reporting_carrier_count": f.integer(mart_row["reporting_carrier_count"]),
    }

    composite_by_profile = {}
    for profile in d.scoring_config()["profiles"]:
        scored = d.scored(profile)
        row = scored[scored["code"] == code].iloc[0]
        composite_by_profile[profile] = {
            "composite": f.num(row["composite"], 1),
            "confidence": f.confidence(row["confidence"]),
        }

    caveats = [
        d.CAVEAT_COHORT_PERCENTILE,
        d.CAVEAT_GROWTH_MISSING,
        d.CAVEAT_SINGLE_MONTH,
        d.CAVEAT_HUB_SIZE_ONE_MONTH,
    ]
    if pd.isna(mart_row["nas_delay_per_departure"]):
        caveats.append(f"No May 2026 On-Time Performance match for {code} -- capacity_strain is unavailable.")
    if mart_row["hub_size"] == "non_hub":
        caveats.append(d.CAVEAT_INVESTABILITY_FLOOR)

    return {
        "code": code,
        "found": True,
        "name": mart_row["name"],
        "state": mart_row["state"],
        "region": mart_row["region"],
        "hub_size_cohort": mart_row["hub_size"],
        "slot_controlled": f.boolean(mart_row["slot_controlled"]),
        "raw_metrics": raw_metrics,
        "percentiles_within_cohort": {k: f.num(v, 1) for k, v in pct_row.items()},
        "pillars": {p: f.num(pillar_row[p], 1) for p in d.pillar_names()},
        "composite_by_profile": composite_by_profile,
        "as_of": d.AS_OF,
        "confidence": composite_by_profile["general"]["confidence"],
        "caveats": caveats,
    }


def _classify(code: str, mart_row: pd.Series, pct_row: pd.Series, pillar_row: pd.Series) -> tuple[str, str]:
    """The bottleneck decision tree. Cutoffs come from config/diagnosis.yaml;
    the rule order below is code, and rests on two judgment calls:

    - Slot control outranks every other signal. A regulatory cap on
      operations is by definition a constraint capital cannot remove, so
      JFK/LGA/DCA are regulatory_constrained however their pillars score.
    - Splitting airfield_constrained from terminal_constrained compares
      taxi_out_p80 (ground movement) against the mean of pct_delayed_15 and
      cancellation_rate (schedule/turnaround). nas_delay_per_departure is
      deliberately left out of the split: it does not cleanly separate
      airfield from terminal causes. A documented boundary, not a fitted one.

    weather_vulnerable, from the original design, is not reachable: see the
    caveat in diagnose_unmet_demand."""
    cfg = d.diagnosis_config()
    high, low = cfg["high_percentile"], cfg["low_percentile"]

    demand = pillar_row["demand_pressure"]
    capacity = pillar_row["capacity_strain"]
    feasibility = pillar_row["feasibility"]
    demand_high = pd.notna(demand) and demand >= high
    capacity_high = pd.notna(capacity) and capacity >= high
    feasibility_low = pd.notna(feasibility) and feasibility <= low
    hub_size = mart_row["hub_size"]

    if bool(mart_row["slot_controlled"]):
        return "regulatory_constrained", (
            f"{code} is on the FAA's slot-controlled list (config/ingest.yaml) -- a regulatory cap on "
            "scheduled operations that capital cannot remove, a structural trap regardless of demand or "
            "capacity signals."
        )

    if demand_high and capacity_high and feasibility_low:
        return "capacity_trap", (
            f"{code} shows high demand pressure ({demand:.1f} pctile) and high capacity strain "
            f"({capacity:.1f} pctile) within its {hub_size} cohort, but feasibility is low "
            f"({feasibility:.1f} pctile) -- congestion with little room to add capacity."
        )

    if demand_high and capacity_high:
        taxi_pct = pct_row.get("taxi_out_p80_pctile")
        turnaround = [
            v for v in (pct_row.get("pct_delayed_15_pctile"), pct_row.get("cancellation_rate_pctile"))
            if pd.notna(v)
        ]
        turnaround_pct = sum(turnaround) / len(turnaround) if turnaround else None
        if pd.notna(taxi_pct) and (turnaround_pct is None or taxi_pct >= turnaround_pct):
            return "airfield_constrained", (
                f"{code} shows high demand pressure and high capacity strain, driven more by ground-movement "
                f"congestion (taxi_out_p80 at the {taxi_pct:.1f} pctile) than by delay/cancellation signals -- "
                "points to the airfield itself as the binding constraint."
            )
        return "terminal_constrained", (
            f"{code} shows high demand pressure and high capacity strain, driven more by delay/cancellation "
            "signals than by ground-movement congestion -- points to terminal/gate capacity rather than the "
            "airfield as the binding constraint."
        )

    if demand_high:
        return "latent_demand_unconstrained", (
            f"{code} shows high demand pressure ({demand:.1f} pctile) but capacity strain is not elevated -- "
            "demand exists but the current constraint isn't binding yet."
        )

    if capacity_high:
        return "strain_without_demand_pressure", (
            f"{code} shows high capacity strain ({capacity:.1f} pctile) without elevated demand pressure -- "
            "operational friction alone, not the demand-driven pattern this thesis targets."
        )

    return "unconstrained", (
        f"{code} shows neither elevated demand pressure nor elevated capacity strain within its {hub_size} "
        "cohort -- not indicated as a renovation candidate under this thesis."
    )


def diagnose_unmet_demand(code: str) -> dict:
    """Rule-based bottleneck classification, not a model judgment."""
    code = code.upper()
    idx = d.row_index_for_code(code)
    if idx is None:
        return _not_found(code, UNKNOWN_CODE)

    mart_row = d.metrics_df().iloc[idx]
    pct_row = d.percentiles().iloc[idx]
    pillar_row = d.pillar_scores().iloc[idx]
    cfg = d.diagnosis_config()

    diagnosis, explanation = _classify(code, mart_row, pct_row, pillar_row)

    caveats = [
        d.CAVEAT_COHORT_PERCENTILE,
        d.CAVEAT_LOAD_FACTOR_ONLY,
        "weather_vulnerable is not a possible diagnosis in this build: On-Time Performance's weather-specific "
        "delay cause is downloaded but never aggregated into the mart, so there is no weather-caused-delay "
        "column to classify from.",
    ]
    missing_pillars = [
        p for p in ("demand_pressure", "capacity_strain", "feasibility") if pd.isna(pillar_row[p])
    ]
    if missing_pillars:
        caveats.append(
            f"Diagnosis is based on incomplete pillar data for {code}: {', '.join(missing_pillars)} unavailable."
        )

    return {
        "code": code,
        "found": True,
        "diagnosis": diagnosis,
        "explanation": explanation,
        "hub_size_cohort": mart_row["hub_size"],
        "slot_controlled": bool(mart_row["slot_controlled"]),
        "evidence": {
            "demand_pressure_pctile": f.num(pillar_row["demand_pressure"], 1),
            "capacity_strain_pctile": f.num(pillar_row["capacity_strain"], 1),
            "feasibility_pctile": f.num(pillar_row["feasibility"], 1),
            "taxi_out_p80_pctile": f.num(pct_row.get("taxi_out_p80_pctile"), 1),
            "pct_delayed_15_pctile": f.num(pct_row.get("pct_delayed_15_pctile"), 1),
            "cancellation_rate_pctile": f.num(pct_row.get("cancellation_rate_pctile"), 1),
            "high_percentile_threshold": cfg["high_percentile"],
            "low_percentile_threshold": cfg["low_percentile"],
        },
        "as_of": d.AS_OF,
        "confidence": f.confidence_for_code(code),
        "caveats": caveats,
    }


def explain_score(code: str, profile: str) -> dict:
    """metric -> percentile -> pillar -> renormalized weight -> contribution."""
    code = code.upper()
    idx = d.row_index_for_code(code)
    if idx is None:
        return _not_found(code, UNKNOWN_CODE)

    mart_row = d.metrics_df().iloc[idx]
    pct_row = d.percentiles().iloc[idx]
    pillar_row = d.pillar_scores().iloc[idx]
    weights = d.scoring_config()
    profile_weights = weights["profiles"][profile]

    metrics_detail = []
    for pillar_name, pillar_cfg in weights["pillars"].items():
        for metric_name, metric_cfg in pillar_cfg["metrics"].items():
            implemented = metric_name in d.metrics_df().columns
            raw_value = None
            if implemented:
                raw_value = (
                    f.boolean(mart_row[metric_name]) if metric_name == "slot_controlled"
                    else f.num(mart_row.get(metric_name), 4)
                )
            metrics_detail.append({
                "metric": metric_name,
                "pillar": pillar_name,
                "implemented": implemented,
                "polarity": metric_cfg["polarity"],
                "raw_value": raw_value,
                "percentile_within_cohort": f.num(pct_row.get(f"{metric_name}_pctile"), 1) if implemented else None,
            })

    available = {p: w for p, w in profile_weights.items() if pd.notna(pillar_row[p])}
    total_weight = sum(available.values())

    pillars_detail = {}
    for p in d.pillar_names():
        score = pillar_row[p]
        is_available = p in available
        renorm_weight = (profile_weights[p] / total_weight) if (is_available and total_weight) else 0.0
        pillars_detail[p] = {
            "score": f.num(score, 1),
            "available": is_available,
            "profile_weight": profile_weights[p],
            "renormalized_weight": round(renorm_weight, 4),
            "contribution_to_composite": f.num(score * renorm_weight, 2) if is_available else None,
        }

    scored = d.scored(profile)
    scored_row = scored[scored["code"] == code].iloc[0]

    caveats = [d.CAVEAT_COHORT_PERCENTILE, d.CAVEAT_GROWTH_MISSING, d.CAVEAT_LOAD_FACTOR_ONLY]
    if pd.isna(mart_row["nas_delay_per_departure"]):
        caveats.append(f"No May 2026 On-Time Performance match for {code} -- capacity_strain is unavailable.")

    return {
        "code": code,
        "found": True,
        "name": mart_row["name"],
        "hub_size_cohort": mart_row["hub_size"],
        "profile": profile,
        "metrics": metrics_detail,
        "pillars": pillars_detail,
        "composite": f.num(scored_row["composite"], 1),
        "confidence": f.confidence(scored_row["confidence"]),
        "as_of": d.AS_OF,
        "caveats": caveats,
    }
