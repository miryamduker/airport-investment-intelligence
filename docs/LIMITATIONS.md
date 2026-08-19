# Known Limitations

Scoring is frozen as of this write-up (2026-08-19). These are the specific,
evidence-backed gaps in the current build -- not a general disclaimer list.
Where a limitation is a "needs more data" gap rather than a design flaw, the
pipeline is already parameterised to close it: adding a data dependency is a
config change (more months, another source), not a rewrite.

## demand_pressure rests on load_factor alone

`load_factor` (passengers / seats) is the only implemented `demand_pressure`
metric. It systematically understates pressure at large hubs, which run high
frequency on dense routes and don't need a high per-flight fill rate to be
demand-constrained -- a hub can add capacity by flying more frequently
without ever pushing load factor up, while a smaller airport with fewer,
fuller flights looks more "pressured" by this metric alone even if it has
plenty of spare capacity to add.

Evidence, May 2026, `terminal_expansion` profile, New England region
(`python -m scripts.show_ranking`):

| Airport | Cohort | demand_pressure percentile |
|---|---|---|
| BOS (Boston Logan) | large | 17.2 |
| BDL (Bradley) | medium | 53.6 |

BOS is a well-known constrained facility, yet it scores in the bottom
quintile of its own hub-size cohort on the pillar meant to capture demand
pressure. BDL, a much smaller and less operationally strained airport,
scores near the middle of its cohort. This is the single clearest artifact
of a one-metric pillar in this build's output, not an edge case.

The two metrics that would round this out --
`upgauge_gap` (are carriers flying smaller aircraft than the route could
support, a sign of suppressed demand rather than a capacity ceiling) and
`peak_month_concentration` (does demand spike seasonally in a way that
strains terminals beyond what a flat annual average would suggest) -- both
require multi-month T-100 history. This build ingests one month
(`ingest/t100.py`, `config/ingest.yaml`: `year`/`month`). Extending it to a
trailing window is a config change to which months get pulled and unioned
before `scoring/metrics.py` aggregates, not a change to the aggregation or
scoring logic itself.

## growth_trajectory is unimplemented

Same root cause as above: `passenger_cagr_3y` needs multiple years of T-100
history to compute a trend, and `taf_forecast_growth` needs an FAA TAF
ingest that hasn't been built (`docs/DATA_RECON.md`: "FAA TAF ... hasn't
been checked yet"). `growth_trajectory` is `None` for every airport in this
build (`scoring/score.py`'s `compute_pillar_scores` returns NaN for a pillar
with zero implemented metrics), and every weight profile's
`growth_trajectory` weight is redistributed proportionally across the other
three pillars by `compute_composite`'s renormalization -- it never silently
zeroes out part of a composite.

## completion_gap was specified, implemented, and removed

The original scoring model (this build's first pass) computed
`completion_gap = 1 - DEPARTURES_PERFORMED / DEPARTURES_SCHEDULED` from
T-100 as the one `capacity_strain` metric. Investigating an Alaska-bush
outlier (Iliamna: 1 scheduled vs. 84 performed) surfaced a much bigger
problem:

- **362 of 588 surviving airports (62%) report `DEPARTURES_PERFORMED >
  DEPARTURES_SCHEDULED`**, and the gap is *worse*, not better, at major
  hubs: **JFK +3,281 departures (+23% over scheduled)**, LAX +3,014 (+17%),
  MIA +2,243 (+17%), SFO +1,372 (+9%), ORD +1,626 (+4%), ATL +453 (+1%).
- This is not a `CLASS = 'L'` (non-scheduled/charter) reporting artifact.
  Splitting by `CLASS` at JFK/ORD/ATL/LAX/DCA showed `L`-class contributes
  at most ~150 performed departures with `DEPARTURES_SCHEDULED = 0`
  (expected -- charters aren't scheduled), nowhere near enough to explain
  the gap. `CLASS = 'F'` alone at JFK is already 14,197 scheduled vs.
  17,459 performed.
- Most plausible explanation: `DEPARTURES_SCHEDULED` reflects the schedule
  as originally filed with BTS; carriers add sections, retime, and amend
  schedules after filing -- especially at high-churn hubs -- without that
  showing up as an increase to `DEPARTURES_SCHEDULED`. The gap measures
  carrier schedule-filing/amendment behavior, not airport capacity strain --
  the opposite of what the metric was meant to capture, and worst exactly at
  the large hubs the investment thesis cares most about.

`completion_gap` was removed rather than clipped or tolerance-banded: a
metric that looks like signal but measures something else is worse than an
absent one. `capacity_strain` is now built entirely from On-Time
Performance instead (`nas_delay_per_departure`, `taxi_out_p80`,
`pct_delayed_15`, `cancellation_rate` -- see `ingest/ontime.py` and
`scoring/metrics.py`'s `join_ontime`), which is why NAS delay (isolating
locally caused congestion) rather than total delay is used, per CLAUDE.md.

## hub_size is derived from one month's passenger share, not a full year

`ingest/airports.py`'s `add_hub_size` classifies every US airport into
`large`/`medium`/`small`/`non_hub` from its share of May 2026 T-100
passengers against national totals (`config/ingest.yaml`'s `hub_size`
thresholds), not from the FAA's own hub-size designation, which is based on
a full calendar year of enplanements. Since every pillar's percentile is
computed *within* hub-size cohort (CLAUDE.md's scoring model), an airport
sitting near a cohort boundary could be classified differently -- and
therefore ranked against a different peer set -- once a full year of data
replaces this single month. This affects cohort membership, not the
percentile math itself.

## Class L (non-scheduled) traffic is included in the passenger filter

The passenger-service filter used throughout ingest is `CLASS in ("F",
"L")` -- "F" (Scheduled) and "L" (Non-Scheduled Civilian Passenger-Cargo),
per `docs/DATA_RECON.md`. `AIRCRAFT_CONFIG == 1` was considered as an
additional filter and rejected because it drops legitimate passenger
volume (658 rows in the May 2026 pull carry `AIRCRAFT_CONFIG != 1` --
combi/seaplane aircraft -- while still carrying 325,517 real passengers),
so `CLASS` alone remains the filter.

That decision is correct for excluding all-cargo traffic, but it leaves
charter ("L"-class) passengers mixed into every volume-based metric
(`load_factor`, `hub_size` classification, the `min_departures_threshold`
cutoff). Charter traffic doesn't reflect the *scheduled* capacity
constraints the investment thesis is about -- a charter-heavy airport's
demand pressure or hub-size share can be inflated by traffic that isn't the
kind of recurring, schedule-driven demand renovation capital would be
built to serve.

## On-Time Performance coverage: 131 of 132 investable hubs, HVN has none

On-Time Performance (ASQP) only requires "reporting carriers" (roughly:
airlines above a DOT-set share of domestic scheduled passenger revenue) to
report, so it covers far fewer airports than T-100 -- 347 distinct origins
vs. 795 US-matched T-100 origins nationally, May 2026
(`docs/DATA_RECON.md`). Restricted to the airports that actually matter for
an investment ranking (`investable_hub_sizes: [large, medium, small]`,
`config/weights.yaml`), coverage is much better: **131 of 132 investable
hub airports have a May 2026 OTP match. Only HVN (Tweed New Haven) does
not.**

`capacity_strain` is `NaN` for HVN, never imputed
(`scoring/metrics.py`'s `join_ontime` is a LEFT JOIN; missing rows stay
missing). This is visible directly in `mart_airport_scores`'s confidence
field for HVN:

```
composite:   40.5   confidence: 0.30 (no OTP match for May 2026)
pillars:    demand_pressure= 37.3  capacity_strain=unavailable  growth_trajectory=unavailable  feasibility= 48.0
```

`confidence` is now a structured `{value, reason}` field precisely so a gap
like this one is stated in the agent's own words rather than left for the
model to infer or guess from a bare 0.30.
