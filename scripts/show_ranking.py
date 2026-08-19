"""Print the airport rankings this build supports:
    - top 10 nationally, 'general' weight profile
    - the full New England ranking, 'terminal_expansion' weight profile

Both are investment rankings: the investability floor (config:
thresholds.investable_hub_sizes) excludes non_hub airports before display,
even though they're scored and remain in mart_airport_metrics for
non-investment queries (see scoring/score.py's filter_investable).

For each airport: hub_size cohort (shown beside every score -- composite is
a percentile within cohort, not a national percentile, so mixing cohorts in
one sorted list without labelling them would be misleading), composite
score, pillar breakdown, which pillars were unavailable, and confidence
(scoring/score.py's compute_confidence -- varies per airport by metric
coverage and departure volume, not a constant). Region filtering happens on
the OUTPUT of score_airports, never on its input -- see scoring/score.py
and tests/test_reproducible.py for why.

Run:
    python -m scripts.show_ranking
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import yaml

from scoring.score import filter_investable, score_airports

MART_PATH = Path("data/marts/mart_airport_metrics.parquet")
WEIGHTS_CONFIG_PATH = Path("config/weights.yaml")

PILLAR_NAMES = ["demand_pressure", "capacity_strain", "growth_trajectory", "feasibility"]


def load_metrics() -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"SELECT * FROM read_parquet('{MART_PATH.as_posix()}')").df()
    con.close()
    return df


def load_weights_config() -> dict:
    return yaml.safe_load(WEIGHTS_CONFIG_PATH.read_text())


def format_pillar(value: float) -> str:
    return "unavailable" if pd.isna(value) else f"{value:5.1f}"


def print_ranking(scored: pd.DataFrame, title: str, profile: str) -> None:
    print("=" * 78)
    print(f"{title}  (profile: {profile})")
    print("NOTE: composite/pillar scores are percentiles within each airport's")
    print("hub_size cohort, not a national percentile -- a 'small' airport at 90 is")
    print("top-of-cohort for small hubs, not directly comparable in raw traffic to a")
    print("'large' airport at 90. Cohort is shown on every row for that reason.")
    print("non_hub airports are excluded (investability floor, config/weights.yaml).")
    print("=" * 78)
    for rank, row in enumerate(scored.itertuples(), start=1):
        pillar_values = {p: getattr(row, p) for p in PILLAR_NAMES}
        unavailable = [p for p, v in pillar_values.items() if pd.isna(v)]
        cohort_tag = f"[{row.hub_size.upper()}]"

        print(f"\n#{rank}  {cohort_tag:<9s} {row.code}  {row.name}  ({row.state})")
        composite_str = "n/a" if pd.isna(row.composite) else f"{row.composite:5.1f}"
        print(f"    composite:  {composite_str}   confidence: {row.confidence:.2f}")
        print(
            "    pillars:    "
            + "  ".join(f"{p}={format_pillar(v)}" for p, v in pillar_values.items())
        )
        if unavailable:
            print(f"    unavailable pillars: {', '.join(unavailable)}")


def main() -> None:
    metrics_df = load_metrics()
    weights_config = load_weights_config()

    national_general = filter_investable(
        score_airports(metrics_df, weights_config, "general"), weights_config
    )
    print_ranking(national_general.head(10), "TOP 10 NATIONALLY", "general")

    national_terminal = filter_investable(
        score_airports(metrics_df, weights_config, "terminal_expansion"), weights_config
    )
    new_england = (
        national_terminal[national_terminal["region"] == "new_england"]
        .reset_index(drop=True)
    )
    print("\n")
    print_ranking(new_england, f"NEW ENGLAND -- full ranking ({len(new_england)} airports)", "terminal_expansion")

    print("\n" + "=" * 78)
    print("PILLAR IMPLEMENTATION STATUS (this build)")
    print("=" * 78)
    print("  demand_pressure    : load_factor implemented. peak_month_concentration")
    print("                       and upgauge_gap are not (need multi-month T-100 history).")
    print("  capacity_strain    : nas_delay_per_departure, taxi_out_p80, pct_delayed_15,")
    print("                       cancellation_rate implemented from On-Time Performance")
    print("                       (ingest/ontime.py). completion_gap (T-100-based) was")
    print("                       tried and removed -- DEPARTURES_SCHEDULED proved")
    print("                       unreliable at scale, see docs/DATA_RECON.md. OTP only")
    print("                       covers reporting carriers, so this pillar is NaN for")
    print("                       any airport with no May 2026 OTP match.")
    print("  growth_trajectory  : not implemented at all (needs multi-year T-100")
    print("                       history and FAA TAF ingest) -- always None this build.")
    print("  feasibility        : fully implemented (runways_per_mpax, slot_controlled).")


if __name__ == "__main__":
    main()
