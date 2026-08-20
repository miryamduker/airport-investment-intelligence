"""Pure scoring functions: percentile rank -> pillar score -> weighted
composite. No file I/O; callers pass in DataFrames and config dicts.

Percentiles are computed nationally, partitioned only by hub_size, across
whatever DataFrame reaches score_airports. Region filtering must happen on
its OUTPUT -- filtering first would rank an airport against a region-only
cohort instead of its national hub-size peers (tests/test_reproducible.py
guards this).
"""
from __future__ import annotations

import pandas as pd

IDENTITY_COLUMNS = ["code", "name", "state", "region", "hub_size", "slot_controlled"]


def compute_metric_percentiles(metrics_df: pd.DataFrame, pillars_config: dict) -> pd.DataFrame:
    """One '<metric>_pctile' column (0-100) per metric present in metrics_df.

    A metric named in pillars_config but absent from metrics_df is not
    implemented this build and is skipped -- the mart's columns are the
    single source of truth for "implemented", not a config flag.

    Percentile = rank of (raw_value * polarity) within hub_size group, so
    higher always means stronger candidate. Ties take the average rank; NaN
    raw values stay NaN, never imputed.
    """
    out = pd.DataFrame(index=metrics_df.index)
    for pillar_cfg in pillars_config.values():
        for metric_name, metric_cfg in pillar_cfg["metrics"].items():
            if metric_name not in metrics_df.columns:
                continue
            polarity = metric_cfg["polarity"]
            adjusted = metrics_df[metric_name].astype(float) * polarity
            pct = adjusted.groupby(metrics_df["hub_size"]).rank(pct=True, method="average") * 100
            out[f"{metric_name}_pctile"] = pct
    return out


def compute_pillar_scores(pctile_df: pd.DataFrame, pillars_config: dict) -> pd.DataFrame:
    """One column per pillar: the mean of its available metric percentiles.
    NaN -- never a guessed default -- when the pillar has no implemented
    metrics, or all of them are NaN for that airport.
    """
    out = pd.DataFrame(index=pctile_df.index)
    for pillar_name, pillar_cfg in pillars_config.items():
        cols = [f"{m}_pctile" for m in pillar_cfg["metrics"] if f"{m}_pctile" in pctile_df.columns]
        out[pillar_name] = pctile_df[cols].mean(axis=1, skipna=True) if cols else float("nan")
    return out


def compute_composite(pillar_df: pd.DataFrame, profile_weights: dict) -> pd.Series:
    """Weighted sum of pillar scores, renormalized per airport over the
    pillars that have a score for it.
    """
    def row_composite(row: pd.Series) -> float:
        available = {p: w for p, w in profile_weights.items() if pd.notna(row.get(p))}
        if not available:
            return float("nan")
        total_weight = sum(available.values())
        return sum(row[p] * w for p, w in available.items()) / total_weight

    return pillar_df.apply(row_composite, axis=1)


def _confidence_reason(
    pctile_isna_row: pd.Series,
    reporting_carrier_count: float,
    full_carriers: int,
    departures_performed: float,
    full_departures: int,
) -> str:
    """One sentence naming the biggest driver of a low confidence score, in
    fixed priority order: no OTP match beats a thin OTP match, beats low
    volume, beats a single missing metric. The agent surfaces this verbatim,
    so it is built here from data rather than by the model.
    """
    if pd.isna(reporting_carrier_count):
        return "no OTP match for May 2026"
    if reporting_carrier_count < full_carriers:
        count = int(reporting_carrier_count)
        return f"only {count} reporting carrier{'' if count == 1 else 's'}"
    if departures_performed < full_departures:
        return f"only {int(departures_performed):,} departures in May 2026"

    missing_metrics = [
        col[: -len("_pctile")] for col, is_na in pctile_isna_row.items() if is_na
    ]
    if missing_metrics:
        return f"missing {', '.join(missing_metrics)}"

    return "full metric coverage, high volume, sufficient carrier diversity"


def compute_confidence(pctile_df: pd.DataFrame, metrics_df: pd.DataFrame, confidence_config: dict) -> pd.Series:
    """Per-airport confidence: a weighted blend of three reliability signals
    (weights and rationale in config/scoring.yaml).

    - metric_coverage: share of implemented metrics with a real value here.
      Read off pctile_df, whose NaNs mirror the raw NaNs.
    - sample_volume: May-2026 departures, scaled to full_confidence_departures.
    - carrier_diversity: distinct OTP-reporting carriers, scaled to
      full_confidence_carriers. No OTP match floors this at 0 -- a statement
      about confidence, not an imputed metric value.

    A pillar-availability count was tried first and rejected: it came out
    constant across the whole dataset.

    Returns {"value": float, "reason": str} per airport, not a bare float.
    """
    total_metrics = pctile_df.shape[1]
    metric_coverage = pctile_df.notna().sum(axis=1) / total_metrics

    full_departures = confidence_config["full_confidence_departures"]
    sample_volume = (metrics_df["departures_performed"] / full_departures).clip(upper=1.0)

    full_carriers = confidence_config["full_confidence_carriers"]
    carrier_diversity = (
        metrics_df["reporting_carrier_count"].fillna(0) / full_carriers
    ).clip(upper=1.0)

    w_cov = confidence_config["metric_coverage_weight"]
    w_vol = confidence_config["volume_weight"]
    w_carrier = confidence_config["carrier_diversity_weight"]
    value = (
        w_cov * metric_coverage + w_vol * sample_volume + w_carrier * carrier_diversity
    ).round(2)

    pctile_isna = pctile_df.isna()
    reason = pd.Series(
        [
            _confidence_reason(
                pctile_isna.loc[idx],
                metrics_df.loc[idx, "reporting_carrier_count"],
                full_carriers,
                metrics_df.loc[idx, "departures_performed"],
                full_departures,
            )
            for idx in metrics_df.index
        ],
        index=metrics_df.index,
    )

    return pd.Series(
        [{"value": v, "reason": r} for v, r in zip(value, reason)],
        index=metrics_df.index,
    )


def score_airports(metrics_df: pd.DataFrame, scoring_config: dict, profile: str) -> pd.DataFrame:
    """One row per airport: identity columns, a score per pillar (NaN if
    unavailable), 'composite', and 'confidence'. Sorted by composite DESC,
    code ASC -- a deterministic tie-break; NaN composites sort last.
    """
    pillars_config = scoring_config["pillars"]
    profile_weights = scoring_config["profiles"][profile]

    pctile_df = compute_metric_percentiles(metrics_df, pillars_config)
    pillar_df = compute_pillar_scores(pctile_df, pillars_config)
    composite = compute_composite(pillar_df, profile_weights)
    confidence = compute_confidence(pctile_df, metrics_df, scoring_config["confidence"])

    result = metrics_df[IDENTITY_COLUMNS].copy()
    result = pd.concat([result, pillar_df], axis=1)
    result["composite"] = composite
    result["confidence"] = confidence

    result["_code_sort"] = result["code"]
    result = result.sort_values(
        by=["composite", "_code_sort"],
        ascending=[False, True],
        na_position="last",
    ).drop(columns="_code_sort").reset_index(drop=True)

    return result


def filter_investable(scored_df: pd.DataFrame, scoring_config: dict) -> pd.DataFrame:
    """Apply the investability floor to an already-scored DataFrame.

    Percentiles are per hub_size cohort, so dropping non_hub rows here
    changes no one else's percentile -- unlike filtering by region. Callers
    building investment rankings use this; callers that need every airport
    regardless of size (flight_mix) do not.
    """
    investable_hub_sizes = set(scoring_config["thresholds"]["investable_hub_sizes"])
    return scored_df[scored_df["hub_size"].isin(investable_hub_sizes)].reset_index(drop=True)
