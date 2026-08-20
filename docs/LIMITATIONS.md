# Known Limitations

Scoring is frozen as of this write-up (2026-08-19). These are the specific,
evidence-backed gaps in the current build, not a general disclaimer list.
Where a limitation is a "needs more data" gap rather than a design flaw, the
pipeline is already parameterised to close it: another month or another
source is a config change, not a rewrite. Every gap below is surfaced to the
user at runtime through the `caveats` and `confidence` fields each tool
returns.

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
build. Because a pillar with no implemented metric is not a pillar, it is
**not declared in `config/scoring.yaml` at all** -- the config describes the
three pillars this build computes, and the four-pillar design lives in
DESIGN.md section 2.1. Profile weights are stated over those three
(`terminal_expansion` 0.40/0.40/0.20, `airfield_expansion` 0.25/0.35/0.40,
`general` equal thirds), and `compute_composite` renormalizes again per
airport for any pillar that is NaN for that airport specifically -- which in
practice means `capacity_strain` where there is no OTP match.

The practical consequence: **nothing in this build looks forward.** Every
ranking is a snapshot of May 2026 conditions. An airport whose demand is
flat but currently strained scores the same as one on a steep growth
trajectory, and the model cannot tell them apart.

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
locally caused congestion) rather than total delay is used, per AGENTS.md.

## Cohort membership is filter-dependent: US territories were excluded until it was caught

`ingest/airports.py` filters OurAirports by `iso_country`. The US territories
carry their own ISO codes (`PR`, `VI`, `GU`, `AS`, `MP`), not `US`, so filtering
on `'US'` alone silently dropped every territory airport from
`dim_airport.parquet` and therefore from the mart, even though T-100 reports
them as origins. **SJU (San Juan) is 541,464 May 2026 passengers, 0.57% of the
national total, a `medium` hub larger than OMA or MKE**, and it was absent from
every ranking.

A second defect compounded it. `state` was derived as
`split_part(iso_region, '-', 2)`, which yields the subdivision code: for the 50
states that is the state (`US-CA` gives `CA`), but for territories it is a
district (`PR-U-A` gives `U`, `AS-WT` gives `WT`), so even a widened country
filter would not have matched the `territories` entry in `config/regions.yaml`.
`state` now falls back to `iso_country` outside the 50 states, and the country
list lives in `config/ingest.yaml` (`iso_countries`) as a scope definition
rather than inline.

Both defects are fixed, but the episode is the limitation worth stating:
**because percentiles are computed within hub-size cohort, every airport's score
depends on which airports are in the set.** Adding 8 territory origins took the
investable set from 132 to 135 and put SJU into `medium`, which displaced CVG
from the top of that cohort (its `demand_pressure` fell from 100.0 to 96.6) and
made **DAY, not CVG, the top-ranked airport nationally** under the `general`
profile. One airport entering a cohort reordered the headline result. Now
guarded by
`tests/test_config_consistency.py::test_every_declared_region_matches_at_least_one_airport`,
which fails if a region is declared in config but matches no airport.

## hub_size is derived from one month's passenger share, not a full year

`ingest/airports.py`'s `add_hub_size` classifies every US airport into
`large`/`medium`/`small`/`non_hub` from its share of May 2026 T-100
passengers against national totals (`config/ingest.yaml`'s `hub_size`
thresholds), not from the FAA's own hub-size designation, which is based on
a full calendar year of enplanements. Since every pillar's percentile is
computed *within* hub-size cohort (AGENTS.md's scoring model), an airport
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

## Excluding all-cargo is right for demand, wrong for the airfield

The `CLASS in ("F","L")` filter keeps freighters out of every metric in the
build. For the demand-side metrics that is not a judgment call but a
definition: `load_factor` is passengers / seats, and an all-cargo MD-11 has
zero of both. Freight does not queue at security, does not need a gate, and
has no airport preference to be suppressed -- counting it as "demand
pressure" would be a category error.

**But cargo aircraft do consume airfield capacity.** They occupy runway
slots, taxiways, ramp stands and ATC attention exactly as passenger
aircraft do. Excluding them from `capacity_strain` and `feasibility`
understates the constraint, and it does so worst at precisely the airports
where the airfield question is most interesting. May 2026, departures by
service class:

| airport | cargo deps (G/P) | passenger deps (F/L) | share of movements invisible to the model |
|---|---|---|---|
| **SDF** (UPS hub) | 4,649 | 2,612 | **64.0%** |
| **MEM** (FedEx hub) | 4,141 | 2,398 | **63.3%** |
| **ANC** | 3,179 | 3,442 | **48.0%** |
| ONT | 1,262 | 2,547 | 33.1% |
| CVG | 1,319 | 4,122 | 24.2% |
| IND | 1,078 | 4,793 | 18.4% |
| OAK | 707 | 3,214 | 18.0% |
| MIA | 2,271 | 15,779 | 12.6% |

At Louisville the scorer sees roughly one third of the aircraft actually
using the runway. The distortion is visible in the mart: `runways_per_mpax`
is **14.06** at SDF, **17.97** at MEM and **12.84** at ANC -- the three most
cargo-dominated airports in the country score as the three with the most
apparent runway headroom, because the denominator counts only people. A
`feasibility` pillar built on that ratio reads "easy to expand" at an
airfield whose night sort operation is close to saturated.

The effect compounds through On-Time Performance. FedEx and UPS are not
ASQP reporting carriers, so cargo movements are missing from the strain
metrics' numerator *and* denominator. A passenger departure held behind
twelve freighters at SDF surfaces as `taxi_out_p80` with no visible cause.

The fix is not to relax the passenger filter -- that would break
`load_factor` and `hub_size`. It is to feed cargo into `feasibility` as a
separate signal: either a `total_movements_per_runway` metric counting all
four service classes, or a `cargo_movement_share` metric with polarity -1,
so a high freighter share correctly reads as *less* room to expand rather
than more. Both need only the existing raw T-100 pull, which already
contains the G/P rows -- `ingest/t100.py` filters them at staging, not at
download. This is a scoring-model change, deliberately not made after the
2026-08-19 freeze.

Until then, treat `feasibility` at the cargo hubs above -- SDF, MEM, ANC,
ONT and CVG in particular -- as an upper bound, not an estimate.

## On-Time Performance coverage: 134 of 135 investable hubs, HVN has none

On-Time Performance (ASQP) only requires "reporting carriers" (roughly:
airlines above a DOT-set share of domestic scheduled passenger revenue) to
report, so it covers far fewer airports than T-100 -- 347 distinct origins
vs. 809 US-matched T-100 origins nationally, May 2026
(`docs/DATA_RECON.md`). Restricted to the airports that actually matter for
an investment ranking (`investable_hub_sizes: [large, medium, small]`,
`config/scoring.yaml`), coverage is much better: **134 of 135 investable
hub airports have a May 2026 OTP match. Only HVN (Tweed New Haven) does
not.**

`capacity_strain` is `NaN` for HVN, never imputed
(`scoring/metrics.py`'s `join_ontime` is a LEFT JOIN; missing rows stay
missing). It shows up directly in HVN's confidence field
(`python -m scripts.show_ranking`):

```
composite:   40.5   confidence: 0.30 (no OTP match for May 2026)
pillars:    demand_pressure= 37.3  capacity_strain=unavailable  growth_trajectory=unavailable  feasibility= 48.0
```

`confidence` is now a structured `{value, reason}` field precisely so a gap
like this one is stated in the agent's own words rather than left for the
model to infer or guess from a bare 0.30.

## "Long haul" is a configured cutoff, not an industry constant

`flight_mix`'s `haul_mix` bins T-100 segments into short/medium/long haul
using the bands in `config/haul.yaml`. There is no binding standard to
defer to: IATA classifies by flight time (long-haul = 6-16 hours), the US
EPA uses >2,300 miles, the UK CAA uses >3,000 km, and carriers set their
own (Southwest treats <=500 miles as short-haul, United <=800). This build
uses **2,500 statute miles** as the long-haul floor — closest round number
to the EPA line and to the ~4,000 km the 6-hour IATA boundary implies at
narrowbody cruise, and it falls inside BTS's own `DISTANCE_GROUP` band 6 so
the bands stay reconcilable against BTS groupings.

The cutoff moves the answer more than the data source does. Anchorage
(ANC), May 2026, passenger service, share of departures:

| threshold | share of flights | share of passengers |
|---|---|---|
| >= 1,500 mi | 19.8% | 36.5% |
| >= 2,000 mi | 15.5% | 28.7% |
| **>= 2,500 mi** | **9.6%** | **19.1%** |
| >= 3,000 mi | 2.8% | 5.8% |

Two further things the tool reports rather than hides:

- **Flights and passengers give different answers.** ANC is 9.6% long-haul
  by departures but 19.1% by passengers, because its short-haul tail is
  bush aviation on 9-seaters while its long-haul is mainline narrowbodies.
  `haul_mix` carries departures, passengers and seats for every band so the
  agent quotes the one the user asked for instead of picking silently.
- **Distance is per segment, not per itinerary.** A connection is counted
  as its separate legs. T-100 Segment has no itinerary grain; that would
  need DB1B, which samples tickets rather than flights.

### The Anchorage cargo trap

This is the case AGENTS.md's passenger-service filter exists for, and ANC
is where it bites hardest. All-cargo service (`CLASS` 'G'/'P') at ANC is
3,179 departures carrying 430M lbs of freight and **one** passenger, almost
all of it long-haul (ORD, SDF, HKG, TPE, MEM, MIA). Including it reports
ANC as **40.2%** long-haul instead of 9.6% — a 4x error that would be
measuring freighters, not passenger service.
`tests/test_haul_mix.py::test_all_cargo_service_is_excluded` guards it.

A smaller residue survives the `CLASS in ('F','L')` filter: business-jet
and ferry operators (VistaJet, Mjet, Omni Air, Atlas Air) fly 8 of ANC's
331 long-haul passenger-class departures carrying 0-9 passengers each.
These are **not** filtered out — `AIRCRAFT_CONFIG` was already rejected as
a hard filter for dropping 325,517 real passengers nationally
(`docs/DATA_RECON.md`), and a bespoke passenger-count filter would be a new
unvalidated judgment call. Instead each band reports
`zero_passenger_departures` so the discrepancy is visible in the payload
rather than silently corrected.
