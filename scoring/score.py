"""Pure scoring functions: percentile rank -> pillar score -> weighted
composite. No file I/O -- callers (scripts/show_ranking.py, tests/) load
mart_airport_metrics.parquet and config/weights.yaml and pass in plain
DataFrames/dicts.

Percentiles are computed NATIONALLY, partitioned only by hub_size, across
whatever DataFrame is passed to score_airports. Region filtering must
happen on the OUTPUT of score_airports, never on its input -- filtering
first would rank an airport against a smaller, region-only cohort instead
of its national hub-size peers, which breaks CLAUDE.md's scoring model
(see tests/test_reproducible.py for the regression test).
"""
from __future__ import annotations

import pandas as pd

IDENTITY_COLUMNS = ["code", "name", "state", "region", "hub_size", "slot_controlled"]


def compute_metric_percentiles(metrics_df: pd.DataFrame, pillars_config: dict) -> pd.DataFrame:
    """One '<metric>_pctile' column (0-100) per metric that is actually a
    column in metrics_df. Metrics named in pillars_config but absent from
    metrics_df are not implemented this build and are silently skipped --
    that is the single source of truth for "implemented", not a config flag.

    Percentile = rank of (raw_value * polarity), as a percentage, computed
    within each hub_size group across the full input DataFrame. Higher
    always means a stronger candidate, regardless of the metric's raw
    polarity. Ties get the average rank (deterministic, order-independent).
    Missing raw values (NaN) stay NaN -- never imputed.
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
    """One column per pillar: the mean of that pillar's available metric
    percentiles for each airport. NaN (never a default/guessed value) when
    the pillar has no implemented metrics at all, or when every implemented
    metric is NaN for that specific airport.
    """
    out = pd.DataFrame(index=pctile_df.index)
    for pillar_name, pillar_cfg in pillars_config.items():
        cols = [f"{m}_pctile" for m in pillar_cfg["metrics"] if f"{m}_pctile" in pctile_df.columns]
        out[pillar_name] = pctile_df[cols].mean(axis=1, skipna=True) if cols else float("nan")
    return out


def compute_composite(pillar_df: pd.DataFrame, profile_weights: dict) -> pd.Series:
    """Weighted sum of pillar scores, renormalized per airport over
    whichever pillars have a non-NaN score for that airport.
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
    """One human-readable sentence naming the single biggest driver of a
    low confidence score, in a fixed priority order (missing OTP entirely
    is a bigger problem than a thin-but-present OTP match, which is bigger
    than low T-100 volume, which is bigger than one missing feasibility
    metric). The agent surfaces this text directly (CLAUDE.md: every tool
    payload carries `confidence`) -- it is built from data by this
    function, never composed or estimated by the language model.
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
    """Per-airport confidence: a weighted blend of three independent
    reliability signals (see config/weights.yaml's `confidence` section for
    weights and rationale), not a pillar-count alone -- a pillar-count-only
    measure was constant across the whole dataset, since growth_trajectory
    is unavailable for everyone and the other three pillars always had at
    least one non-null metric for every airport.

    metric_coverage: fraction of this build's implemented metrics (across
    ALL pillars, not just one) that have a real value for this specific
    airport -- catches per-airport gaps a pillar-level count misses, e.g. no
    OurAirports runway match (runways_per_mpax NaN), zero passengers
    (load_factor NaN), or no On-Time Performance match at all (all four
    capacity_strain metrics NaN). Uses pctile_df because a metric's
    percentile is NaN exactly when its raw value was NaN (pandas rank keeps
    NaN as NaN).

    sample_volume: how much May-2026 traffic backs this airport's
    percentile, scaled linearly up to confidence_config['full_confidence_departures'].

    carrier_diversity: how many distinct carriers reported On-Time
    Performance data for this airport, scaled linearly up to
    confidence_config['full_confidence_carriers']. Missing entirely (no OTP
    match) floors this component at 0 -- that's a statement about
    confidence, not a fabricated metric value: capacity_strain itself stays
    NaN for those airports (see scoring/metrics.py's join_ontime) and is
    never imputed from this.

    Returns a Series of {"value": float, "reason": str} dicts, one per
    airport -- not a bare float. `reason` names the single biggest driver
    of that airport's score (see _confidence_reason) so the agent can
    surface it verbatim instead of the model having to interpret or
    describe a number itself.
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


def score_airports(metrics_df: pd.DataFrame, weights_config: dict, profile: str) -> pd.DataFrame:
    """Score every airport in metrics_df under the given weight profile.

    Returns one row per airport: identity columns, one '<pillar>' score
    column per pillar (NaN if unavailable), 'composite', and 'confidence'.
    Sorted by composite DESC, code ASC (deterministic tie-break); NaN
    composites sort last.
    """
    pillars_config = weights_config["pillars"]
    profile_weights = weights_config["profiles"][profile]

    pctile_df = compute_metric_percentiles(metrics_df, pillars_config)
    pillar_df = compute_pillar_scores(pctile_df, pillars_config)
    composite = compute_composite(pillar_df, profile_weights)
    confidence = compute_confidence(pctile_df, metrics_df, weights_config["confidence"])

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


def filter_investable(scored_df: pd.DataFrame, weights_config: dict) -> pd.DataFrame:
    """Apply the investability floor (config: thresholds.investable_hub_sizes)
    to an already-scored DataFrame. Percentiles inside score_airports are
    computed per hub_size cohort, so this is a pure post-filter -- dropping
    non_hub rows here does not change anyone else's percentile, unlike
    filtering by region (see module docstring and
    tests/test_reproducible.py).

    Callers building investment rankings (e.g. scripts/show_ranking.py)
    should call this on score_airports' output. Callers that need every
    surviving airport regardless of size (e.g. a future flight_mix tool)
    should not.
    """
    investable_hub_sizes = set(weights_config["thresholds"]["investable_hub_sizes"])
    return scored_df[scored_df["hub_size"].isin(investable_hub_sizes)].reset_index(drop=True)
