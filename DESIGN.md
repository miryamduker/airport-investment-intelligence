# Airport Investment Intelligence Agent — Design Document

**Status (2026-08-19): data pipeline and scoring model are built and frozen.**
Sections 3, 4, and 8 below describe what was actually built, verified
against this build's own output. Sections 1, 2, 5, 6, 7, and 9 describe the
agent/tool/web layer, which has not been built yet and is still the
original plan — read them as intent, not as a build report. See
[docs/LIMITATIONS.md](docs/LIMITATIONS.md) for the full evidence behind
every scoring limitation summarized in §8, and
[docs/DATA_RECON.md](docs/DATA_RECON.md) for the data-access investigation
behind §3.

---

## 1. Problem framing

The brief asks for an agent that identifies airports where *renovation will be most profitable, based on increased flight and passenger capacity*.

Restated as an investment thesis:

> **Renovation creates value where demand is currently being suppressed by a physical constraint that capital can remove.**

Three conditions must hold simultaneously for an airport to be a good candidate:

| Condition | Meaning | Counterexample |
|---|---|---|
| **Demand pressure** | More passengers/flights want in than can get in | An airport with spare capacity — nothing to unlock |
| **Capacity strain** | The constraint is actively binding | A busy airport that still flows smoothly — no upside |
| **Expansion feasibility** | Money can actually remove the constraint | LGA: jammed, but boxed in by water and city |

An airport that scores highly on strain but poorly on feasibility is a **trap**, not an opportunity. Systems that rank purely on congestion will surface these traps. This system explicitly penalises them.

---

## 2. Scope

### In scope
- All US airports with scheduled commercial service appearing in BTS T-100 (~350).
- Ranking, pairwise comparison, single-airport diagnostics, and flight-mix analytics.
- Conversational follow-up with retained context.

### Explicitly out of scope
- Construction cost estimation, ROI or IRR modelling. We rank *opportunity*, not *return*. Without construction cost data, any dollar figure would be fabricated.
- Non-US airports.
- Real-time operational advice. This is a capital-allocation tool operating on multi-month structural trends.
- Cargo-only and general aviation facilities.

### Stated assumptions
1. **Passenger and flight capacity are the value drivers**, per the brief. Retail revenue, parking, and landing fees are not modelled.
2. **Recent demand patterns persist.** Trailing-12-month behaviour is treated as indicative of near-term structural demand.
3. **Delay is a valid proxy for capacity strain.** Imperfect — delay also propagates through networks from other airports — and this limitation is surfaced to the user.
4. **Gate counts are approximate.** Where authoritative counts are unavailable, they are estimated from peak concurrent departures; airports using estimates are flagged.

---

## 3. Data sources

| Source | Provides | Access | Coverage in this build |
|---|---|---|---|
| BTS T-100 Segment | Passengers, seats, departures scheduled/performed, distance, by carrier × origin × dest × month | TranStats `DL_SelectFields.aspx` form-post — **not** Socrata, see below | One month: **May 2026** |
| BTS On-Time Performance (ASQP) | Flight-level delay by cause, taxi-out, cancellations | Same TranStats form-post mechanism, different table | One month: May 2026; **131 of 132** investable-hub airports matched |
| OurAirports | Coordinates, runway counts, state | Public CSV, no form | Full US airport set with an IATA code |
| FAA Terminal Area Forecast | Forward enplanement/operations forecast | Not yet ingested | Not implemented |
| OpenSky Network | Live state vectors (illustrative only) | Not yet integrated | Not implemented — belongs to the agent/tool layer, not this build |

### Correction: not Socrata

This document originally assumed T-100 Segment and On-Time Performance were
reachable via the BTS Socrata REST API at `data.bts.gov`. Recon
([docs/DATA_RECON.md](docs/DATA_RECON.md)) found that's wrong for the grain
this thesis needs: every T-100/On-Time asset on that Socrata domain is
either a pre-aggregated summary (no carrier/destination breakdown) or a
non-tabular dashboard object that 403s on query. The real row-level tables
live on the legacy TranStats system (`transtats.bts.gov`), behind an
ASP.NET download form (`DL_SelectFields.aspx`) that posts
`__VIEWSTATE`/`__EVENTVALIDATION` plus field checkboxes and returns a
freshly generated zip. [`ingest/t100.py`](ingest/t100.py) and
[`ingest/ontime.py`](ingest/ontime.py) replicate that form-post; see
`docs/DATA_RECON.md` for the full investigation, including the
ROT13-obfuscated field codes and On-Time Performance's export columns not
matching its own field-selection checkbox names.

### Correction: single month, not trailing-12-month

CLAUDE.md scopes this build to one recent T-100/On-Time month (currently
May 2026, the latest published on TranStats as of this write-up), not the
trailing-12-month window §2's assumptions describe. The ingest pipeline is
parameterised by year/month (`config/ingest.yaml`), so extending to a
multi-month window is a config and unioning change in `ingest/t100.py` /
`ingest/ontime.py`, not a rewrite — but `growth_trajectory` and two
`demand_pressure` metrics stay unimplemented until that happens (§4,
`docs/LIMITATIONS.md`).

### FAA TAF and OpenSky: not yet built

Neither has been ingested. FAA TAF recon hasn't started
(`docs/DATA_RECON.md`'s open items). OpenSky/live traffic belongs to the
agent tool layer (`live_traffic_snapshot`), which this freeze point
explicitly defers — see the status note at the top of this document.

---

## 4. Scoring methodology

### 4.1 Structure

Four independently computed **pillars**, recombined under named **weight
profiles**. This build implements a subset of the metrics originally
specified per pillar. Implementation status is not tracked as a config
flag — [`scoring/score.py`](scoring/score.py)'s `compute_metric_percentiles`
treats "is this metric a real column in `mart_airport_metrics`" as the
single source of truth for "implemented," so a metric can't claim
implemented in `config/weights.yaml` while its computation is missing or
renamed in `scoring/metrics.py`.

**Pillar A — Demand Pressure**
- `load_factor` (passengers ÷ seats) — **implemented**
- `peak_month_concentration` — not implemented (needs multi-month T-100 history)
- `upgauge_gap` — not implemented (replaces this draft's original "passengers per gate," which needs gate-count data this build never sourced)

`load_factor` alone systematically understates pressure at large,
high-frequency hubs. See `docs/LIMITATIONS.md` for the evidence (BOS 17.2
vs. BDL 53.6, both investable hubs in the same region).

**Pillar B — Capacity Strain**
- `nas_delay_per_departure`, `taxi_out_p80`, `pct_delayed_15`,
  `cancellation_rate` — **all four implemented**, sourced entirely from
  On-Time Performance (`ingest/ontime.py`), not T-100.
- This draft's original metric, `departures performed ÷ departures
  scheduled` (`completion_gap`), was implemented, investigated, and
  **removed**: T-100's `DEPARTURES_SCHEDULED` proved unreliable at scale
  (62% of surviving airports show performed > scheduled, worst at major
  hubs — JFK +23%). See `docs/LIMITATIONS.md` and `docs/DATA_RECON.md` for
  the full investigation.
- Uses **NAS delay** specifically — isolating locally caused congestion
  from delay propagated in from other airports — never total delay, per
  CLAUDE.md.

**Pillar C — Growth Trajectory**
- **Not implemented.** Needs multi-year T-100 history (3y CAGR) and an FAA
  TAF ingest that hasn't been built. `None` for every airport this build;
  its weight is redistributed proportionally across the other three
  pillars per airport (`compute_composite`'s renormalization), never
  silently dropped or zeroed.

**Pillar D — Expansion Feasibility**
- `runways_per_mpax` (runway count normalized by passenger volume, not a
  raw runway count — comparable across airport sizes) — **implemented**
- `slot_controlled` (boolean flag from `config/ingest.yaml`'s static list)
  — **implemented**
- This draft's original land-constraint proxy (operations density against
  airport land area) — not implemented; no land-area data source was
  identified.

### 4.2 Normalisation

Matches the original design: each metric becomes a percentile rank within
its hub-size cohort, computed nationally at build time
(`scoring/score.py`'s `compute_metric_percentiles`, grouped by
`hub_size`) — never within a region-filtered subset, since that would make
the best airport in any region score 100 by definition. Enforced by
[`tests/test_reproducible.py`](tests/test_reproducible.py)'s
`test_region_filter_after_scoring_matches_national`, which asserts that
scoring a region-prefiltered DataFrame produces different (wrong)
composites than filtering an already-nationally-scored one.

One correction: hub-size cohort itself is derived from **one month's**
passenger share (`ingest/airports.py`'s `add_hub_size`), not the FAA's own
full-year hub-size designation this document originally assumed — see
`docs/LIMITATIONS.md`.

### 4.3 Composition

```
Score = wA·DemandPressure + wB·CapacityStrain + wC·GrowthTrajectory + wD·ExpansionFeasibility
```

Matches the original design: weights live in `config/weights.yaml`, three
profiles (`terminal_expansion`, `airfield_expansion`, `general`). One
addition beyond the original draft: because `growth_trajectory` is `None`
for every airport this build, `compute_composite` renormalizes each
airport's composite over only the pillars with a real score for that
specific airport, rather than leaving every composite systematically
depressed by a pillar that's structurally absent for everyone.

### 4.4 Constraint diagnosis

**Not implemented in this build.** The rule-based bottleneck classifier
(airfield-constrained / terminal-constrained / weather-vulnerable /
regulatory-constrained / unconstrained) described in the original design is
deferred to the tool layer (`diagnose_unmet_demand`, per CLAUDE.md's tool
list) — this build ends at the scoring model, not the agent/tools that
would consume it. See the status note at the top of this document.

### 4.5 Confidence

Implemented as `scoring/score.py`'s `compute_confidence`: a weighted blend
of three independent per-airport reliability signals — `metric_coverage`
(fraction of this build's implemented metrics with a real value for that
airport), `sample_volume` (May-2026 departure volume, scaled to
`config/weights.yaml`'s `full_confidence_departures`), and
`carrier_diversity` (distinct OTP-reporting carriers, scaled to
`full_confidence_carriers`) — rather than a pillar-availability count
alone, which was tried first and rejected: it was constant (0.75) across
the entire dataset, since `growth_trajectory` is absent for everyone and
the other three pillars always had at least one non-null metric.

`confidence` is a **structured field**, not a bare float:
`{value, reason}`, where `reason` is a short, deterministic sentence naming
the single biggest driver of that airport's score (e.g. `"no OTP match for
May 2026"`, `"only 2 reporting carriers"`) — generated entirely in Python
from the same signals that produce `value`, never composed or estimated by
the language model. The agent is meant to surface `reason` verbatim rather
than interpret the number itself. Example, HVN (the one investable hub with
no May 2026 OTP match):

```
composite:   40.5   confidence: 0.30 (no OTP match for May 2026)
pillars:    demand_pressure= 37.3  capacity_strain=unavailable  growth_trajectory=unavailable  feasibility= 48.0
```

---

## 5. Architecture

```
React chat UI
      │
FastAPI  /chat
      │
Agent loop (OpenAI function calling)
      │
Tool layer  ──  rank_airports · compare_airports · airport_profile
                flight_mix · explain_score · resolve_location
      │
Scoring engine (pure Python, deterministic)
      │
Metrics layer (pandas / DuckDB)
      │
Cached parquet  ←  ingestion scripts  ←  BTS · FAA · OurAirports
```

Every arrow below the tool layer is deterministic and independently testable without any model call.

---

## 6. Where AI is used — and where it is not

| Task | Handled by |
|---|---|
| Interpreting natural language intent | **LLM** |
| Resolving "New England" → state list → airport set | **Deterministic** (lookup table) |
| Resolving ambiguity ("LA" → LAX only, or the LA basin?) | **LLM asks the user** |
| Choosing which tool to call, with what arguments | **LLM** |
| Computing every metric | **Deterministic** |
| Ranking and scoring | **Deterministic** |
| Diagnosing bottleneck type | **Deterministic** |
| Narrating results in prose | **LLM** |
| Maintaining conversational context | **LLM** |

**The language model never produces a number.** It selects tools and explains their output. Every figure surfaced to the user is traceable to a specific tool call, and the UI exposes those raw calls beneath each response so this is verifiable rather than merely asserted.

Temperature is set near zero: tool selection should be reproducible, not creative.

---

## 7. Key tradeoffs

**Cached data over live APIs.** Loses recency, gains authority and decision-relevance. Justified in §3.

**Opportunity ranking over ROI modelling.** A dollar-denominated return figure would be more directly useful and entirely fabricated. Ranking relative opportunity is defensible with available data.

**Percentile-within-cohort over absolute scoring.** Loses cross-cohort comparability — a small airport at the 90th percentile is not equivalent to a large hub at the 90th. The agent states cohort explicitly to prevent this misreading.

**Delay as a strain proxy.** The best throughput-versus-capacity data (FAA ASPM/OPSNET) requires credentialed access unavailable in this timeframe. Delay is the strongest public substitute, with the caveat that network-propagated delay is not locally caused.

**Hand-rolled agent loop over a framework.** LangChain or similar would add abstraction without adding capability at this scale, and would obscure the control flow this document needs to explain clearly.

**Breadth of airports over depth per airport.** Covering all ~350 commercial airports costs nothing extra (the data arrives as one file) and is required for regional questions to return meaningful sets. Per-airport depth — terminal layouts, capital plans, local politics — is sacrificed.

---

## 8. Known limitations

Superseded by [docs/LIMITATIONS.md](docs/LIMITATIONS.md), which documents
each limitation against this build's own output — specific airports,
percentiles, row counts — rather than as a general disclaimer list.
Summary:

1. `demand_pressure` rests on `load_factor` alone — understates pressure at
   high-frequency hubs. Evidence: BOS scores 17.2 while BDL scores 53.6,
   despite BOS being a well-known constrained facility.
2. `growth_trajectory` is unimplemented — needs multi-month/multi-year
   T-100 history and an FAA TAF ingest neither of which this build has.
   The pipeline is parameterised by month, so adding months is a config
   change, not a code change.
3. `capacity_strain`'s original T-100-based metric (`completion_gap`) was
   specified, implemented, and removed: 62% of airports report
   `DEPARTURES_PERFORMED > DEPARTURES_SCHEDULED`, worst at major hubs
   (JFK +23%), confirmed not a small-airport artifact. It measured carrier
   schedule-filing behaviour, not airport capacity strain. Replaced
   entirely by four On-Time Performance metrics.
4. `hub_size` is derived from one month's passenger share; the FAA uses a
   full year.
5. Class L (non-scheduled) traffic is included in the passenger-service
   filter — charter traffic doesn't reflect scheduled capacity
   constraints, and inflates volume-based metrics with non-scheduled
   demand.
6. On-Time Performance (capacity_strain's only data source) covers 131 of
   132 investable hub airports for May 2026; HVN has none — now reflected
   per-airport by the structured `confidence.reason` field (§4.5).

This build's gate-count and land-constraint-proxy gaps in the original
draft's limitations list no longer apply: neither a gate-count metric nor a
land-constraint metric was ever implemented — they were replaced in the
real scoring model (§4.1) by `upgauge_gap` / `peak_month_concentration`, and
dropped entirely, respectively, before any code was written against them.
The original draft's remaining limitations (delay causation not
decomposed, data lag, small-airport confidence) are unaffected by what was
actually built and still apply.

---

## 9. What I would build next

- Ingest FAA NPIAS capital-needs data to compare opportunity against planned spend.
- Model catchment-area overlap — expanding one airport may cannibalise a neighbour.
- Fare-premium analysis as an independent suppressed-demand signal.
- Backtest scores against airports that actually expanded, to validate predictive value.
