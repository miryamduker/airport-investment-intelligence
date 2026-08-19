"""Print the two rankings this build supports: the national top 10 under the
'general' profile, and all of New England under 'terminal_expansion'.

Both apply the investability floor, and both filter by region only after
national scoring. Every row shows its hub_size cohort, because composite is
a percentile within cohort rather than a national one.

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

def load_metrics() -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"SELECT * FROM read_parquet('{MART_PATH.as_posix()}')").df()
    con.close()
    return df


def load_weights_config() -> dict:
    return yaml.safe_load(WEIGHTS_CONFIG_PATH.read_text())


PILLAR_NAMES = list(load_weights_config()["pillars"])


def format_pillar(value: float) -> str:
    return "unavailable" if pd.isna(value) else f"{value:5.1f}"


def print_ranking(scored: pd.DataFrame, title: str, profile: str) -> None:
    print("=" * 78)
    print(f"{title}  (profile: {profile})")
    print("Scores are percentiles within each airport's hub_size cohort, not")
    print("national -- a 'small' airport at 90 is top of the small-hub cohort, not")
    print("comparable in raw traffic to a 'large' airport at 90. non_hub airports")
    print("are excluded by the investability floor (config/weights.yaml).")
    print("=" * 78)
    for rank, row in enumerate(scored.itertuples(), start=1):
        pillar_values = {p: getattr(row, p) for p in PILLAR_NAMES}
        unavailable = [p for p, v in pillar_values.items() if pd.isna(v)]
        cohort_tag = f"[{row.hub_size.upper()}]"

        print(f"\n#{rank}  {cohort_tag:<9s} {row.code}  {row.name}  ({row.state})")
        composite_str = "n/a" if pd.isna(row.composite) else f"{row.composite:5.1f}"
        print(
            f"    composite:  {composite_str}   confidence: {row.confidence['value']:.2f} "
            f"({row.confidence['reason']})"
        )
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
    print("Implemented: load_factor (demand), four On-Time Performance metrics")
    print("(strain), runways_per_mpax + slot_controlled (feasibility).")
    print("growth_trajectory has no implemented metric -- see docs/LIMITATIONS.md.")


if __name__ == "__main__":
    main()
