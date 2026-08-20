# Airport Investment Intelligence Agent

**Start with [DESIGN.md](DESIGN.md)**: scoring methodology, tradeoffs, and where
AI is used. This file is setup and how to run things.

Also: [docs/LIMITATIONS.md](docs/LIMITATIONS.md) (what the numbers can and can't
support) and [docs/DATA_RECON.md](docs/DATA_RECON.md) (how the data was obtained).

## Setup

```
python -m pip install -r requirements.txt
```

## Quick start

Runs the chat UI against the marts already in `data/`. Building those from
source is the Ingest sections below; you only need them starting from scratch.

Two processes, two terminals. The API needs `OPENAI_API_KEY` in a `.env` at the
repo root -- it refuses to start without one.

Terminal 1, the API on port 8000:

```
python -m uvicorn api.main:app --port 8000
```

Terminal 2, the web UI:

```
cd web
npm install
npm run dev
```

Then open the URL Vite prints -- `http://localhost:5173`, or the next free port
if something already holds that one. The UI checks the API on load and tells you
if it isn't up.

For the same agent in the terminal, with every tool call printed above each
reply and no `npm` involved:

```
python -m cli
```

Both are covered in more detail under [Running the chat UI](#running-the-chat-ui).

## Ingest: T-100 Segment

```
python -m ingest.t100
python -m ingest.t100 --year 2026 --month 5
```

Downloads one month of BTS T-100 Segment (carrier x origin x dest x month)
from the TranStats download form, caches the raw zip in `data/raw/`, filters
to passenger service, types the columns, and writes
`data/staging/t100_segment.parquet`. Prints a validation report. See the
module docstring in [ingest/t100.py](ingest/t100.py) for why this goes
through an ASP.NET form-post instead of a REST call.

## Ingest: On-Time Performance

```
python -m ingest.ontime
python -m ingest.ontime --year 2026 --month 5
```

Downloads one month of BTS On-Time Performance (ASQP), the same form-post
approach as T-100 against a different TranStats table, and aggregates
straight to airport-month grain in
`data/staging/ontime_airport_monthly.parquet` (`nas_delay_per_departure`,
`taxi_out_p80`, `pct_delayed_15`, `cancellation_rate`,
`reporting_carrier_count`). Prints a validation report. See
[docs/DATA_RECON.md](docs/DATA_RECON.md)'s On-Time Performance section for
two things worth knowing first (the export CSV's column names don't match
the field-selection checkboxes, and OTP covers far fewer airports than
T-100).

## Ingest: OurAirports (airport dimension)

```
python -m ingest.airports
```

Downloads OurAirports' airports/runways CSVs and writes
`data/staging/dim_airport.parquet`: IATA code, name, state, region
(`config/regions.yaml`), coordinates, runway count, `hub_size` (from T-100
passenger share -- requires `ingest.t100` to have run first), and
`slot_controlled`.

## Build the marts and score

```
python -m scoring.metrics
python -m scripts.show_ranking
```

`scoring/metrics.py` joins T-100, the airport dimension, and On-Time
Performance into `data/marts/mart_airport_metrics.parquet` (one row per
surviving origin airport) and prints a build report. `scoring/score.py`
turns that into percentile-based pillar scores and a weighted composite per
`config/scoring.yaml`; `scripts/show_ranking.py` prints the national top 10
and the full New England ranking.

## Terminal chat

```
python -m cli
```

Prints every tool call (name, arguments, full JSON result) above each
reply, so a transcript shows exactly which numbers came from where.

## Tests

```
python -m pytest tests/ -v
```

`test_reproducible.py` and `test_config_consistency.py` cover the scoring
model; `test_agent_loop.py` and `test_api.py` cover the agent loop and the HTTP
layer against a fake OpenAI client, so the whole suite runs with no API key and
no network.

## Running the chat UI

Two processes: the FastAPI backend and the Vite dev server for `web/`.

### 1. Backend

Needs `OPENAI_API_KEY` set (`.env` in the repo root, read via `python-dotenv`).

```
python -m uvicorn api.main:app --port 8000
```

It refuses to start without `OPENAI_API_KEY` rather than failing on the first
question. Two endpoints:

- `POST /chat`, taking `{message, history}` and returning
  `{reply, tool_calls, as_of}`. `message` is capped at 2,000 characters and
  `history` at 40 turns, so an oversized request is rejected before it becomes
  a paid model call.
- `GET /health`, returning the model name and the data vintage. The web UI
  calls it on load, so an unreachable backend is visible before you type a
  question rather than after waiting on one.

A failure at the model provider comes back as a readable status and `detail`
(502 for a bad key, 429 for rate limiting, 504 for a timeout) rather than a
bare 500. Set `LOG_LEVEL=DEBUG` to see every model call and tool call.

**No auth, no rate limiting.** This is a localhost tool running against the
operator's own API key -- deliberately not hardened for public exposure. Do not
put it on a public interface without putting something in front of it.

### 2. Frontend

```
cd web
npm install
npm run dev
```

Opens on `http://localhost:5173` and talks to the backend at `http://localhost:8000` by
default. To point it at a different backend URL, copy `web/.env.example` to `web/.env`
and set `VITE_API_URL`.

The page is a single chat view: message history is resent with every request (the
backend is stateless per request). The UI itself only shows the prose reply -- the
underlying tool calls (name, arguments, full JSON result, `as_of`/`confidence`/
`caveats`) are still returned by `/chat` and are what the terminal transcript
(`python -m cli`) is for checking the "the model never produces a number" rule
against, rather than the end-user chat view.

If the backend is unreachable the UI shows an error banner naming the URL it tried,
rather than failing silently. A request in flight can be cancelled with the Stop
button, and times out client-side after 120s so a wedged backend never leaves the
view animating forever.

## Layout

```
ingest/     one module per source, over a shared TranStats form client
scoring/    metrics.py builds the mart; score.py is pure scoring functions
tools/      the seven agent tools, grouped by question type:
              lookup       resolve free text to codes
              ranking      compare airports against each other
              diagnostics  explain one airport
              traffic      descriptive route mix
            data.py is the one cached reader of the mart and config
agent/      the hand-rolled function-calling loop and system prompt
api/        FastAPI wrapper over the loop
web/        React chat view
config/     scoring weights, regions, aliases, thresholds
```

