# Project: Airport Investment Intelligence Agent

Take-home exam. A chat agent that ranks US airports as expansion investment
candidates for an infrastructure investment firm.

## The one rule that matters

**The language model must never produce a number.**

It selects tools and narrates their output. Every metric, score, ranking and
diagnosis is computed by deterministic Python. Same query twice = identical
scores, enforced by a test.

If a suggestion would have the model computing, comparing, or estimating
anything numeric, reject it and say so.

## Thesis

Renovation pays off only where demand is suppressed by a constraint capital
can remove. Three conditions must hold together:

- Demand pressure — more people want in than can get in
- Capacity strain — the constraint is actively binding
- Feasibility — money can actually remove it

Congestion alone is a trap: it surfaces airports that cannot expand.

## Stack

- Python 3.11+, FastAPI, DuckDB over parquet, PyYAML
- React single page, one chat view (later)
- OpenAI function calling, **hand-rolled loop**
- No LangChain, LangGraph, CrewAI, or any agent framework
- No RAG, no embeddings, no vector store — the data is structured

## Data sources

| Source | Grain | Gives |
|---|---|---|
| BTS T-100 Segment | carrier × origin × dest × month | seats, passengers, departures scheduled/performed, distance |
| BTS On-Time Performance | one flight | delay by cause, taxi-out, cancellations |
| OurAirports | airport | coordinates, state, runways |
| FAA TAF | airport × year | forecast growth |

T-100 Segment and On-Time Performance are **not** on the Socrata REST API at
data.bts.gov — that domain only hosts pre-aggregated summaries and
non-queryable dashboard objects for these tables. The real row-level data
lives on the legacy TranStats system (transtats.bts.gov) behind an ASP.NET
download form. See `docs/DATA_RECON.md` and `ingest/t100.py`. Cached as
parquet.

Non-negotiable filters:
- Passenger service only. Exclude all-cargo, or Anchorage results measure freighters.
- One volume definition (departing passengers OR total throughput). Never mix.

### Current implementation scope

This build uses **one recent month** of T-100 Segment and On-Time
Performance (May 2026, the latest published on TranStats — see
`docs/DATA_RECON.md`), not a multi-month history. A deliberate scoping
decision, not a gap:

- **Capacity strain is implemented** from On-Time Performance: NAS delay per
  departure, taxi-out p80, pct delayed 15+, cancellation rate. `completion
  gap` was implemented and removed — `DEPARTURES_SCHEDULED` is unreliable at
  scale (see `docs/LIMITATIONS.md`).
- **Growth trajectory** (3y passenger CAGR, TAF forecast growth) and
  **peak-month concentration** (part of Demand pressure) are specified in
  the scoring model below but not implemented — both need multi-month
  history this build does not ingest.

## Layout

```
ingest/      one module per source, over a shared TranStats form client
data/
  raw/       as downloaded, never edited
  staging/   typed, filtered
  marts/     mart_airport_metrics
scoring/     metrics.py builds the mart; score.py is pure scoring
tools/       the seven agent tools, grouped by question type
             (lookup, ranking, diagnostics, traffic) over data.py
agent/       loop, system prompt
api/         FastAPI wrapper
config/      weights, regions, aliases, thresholds
tests/
web/         React chat view
```

## Scoring

Four pillars → percentile rank **within FAA hub-size cohort** → weighted sum.

| Pillar | Metrics |
|---|---|
| Demand pressure | load factor, upgauge gap, peak-month concentration |
| Capacity strain | NAS delay per departure, taxi-out p80, completion gap |
| Growth trajectory | 3y passenger CAGR, TAF forecast growth |
| Feasibility | runways per million pax, slot-controlled flag |

Use **NAS delay**, never total delay — it isolates locally caused congestion
from delay propagated in from other airports.

Percentiles are computed **nationally at build time**, then filtered by region
at query time. Never rank within a filtered subset — that would make the best
airport in any region score 100 by definition.

Every metric declares a polarity (+1 / -1) in config so higher always means
stronger candidate.

Weights live in `config/scoring.yaml` with three profiles:
`terminal_expansion`, `airfield_expansion`, `general`. They are a documented
judgment call, not a fitted parameter.

Ties break deterministically: `ORDER BY score DESC, code ASC`.

## Tools

`resolve_airports`, `rank_airports`, `compare_airports`, `airport_profile`,
`flight_mix`, `diagnose_unmet_demand`, `explain_score`

Rules:
- **No generic SQL tool, ever.** The model must not compose queries.
- Arguments constrained by enum, never free strings.
- Every return payload carries `as_of`, `confidence`, `caveats`.
- Descriptions state when to use the tool AND when not to.

## Conventions

- Type hints everywhere; pure functions in `scoring/`
- No magic numbers in code — thresholds and weights go in `config/`
- Stubs return obviously fake values (`99.9`, `"STUB"`, `as_of: "1999-01"`)
- Ask before adding a dependency

## Working style

Small steps. Show the plan before writing more than ~50 lines. Do not
scaffold ahead — build only what the current task needs.
