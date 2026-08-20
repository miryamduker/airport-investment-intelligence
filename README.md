# Airport Investment Intelligence Agent

A chat agent that ranks US airports as candidates for expansion capital. Every
number it reports is computed by deterministic Python; the language model only
picks tools and narrates their output.

**Start with [DESIGN.md](DESIGN.md)**: scoring methodology, tradeoffs, and where
AI is used. This file is setup and how to run things.

Also: [docs/LIMITATIONS.md](docs/LIMITATIONS.md) (what the numbers can and can't
support) and [docs/DATA_RECON.md](docs/DATA_RECON.md) (how the data was obtained).

## Demo

[Watch the demo]([https://drive.google.com/...](https://drive.google.com/file/d/1-BmnHdGlUlgsa7cyA1xAUgvl2AM_Pc8b/view?usp=sharing))

## Setup

```
python -m pip install -r requirements.txt
```

The API needs an OpenAI key in a `.env` at the repo root:

```
OPENAI_API_KEY=sk-...
```

It refuses to start without one, rather than failing on the first question.

The agent reads the parquet files in `data/`. If yours is empty, build them
first: see [Rebuilding the data](#rebuilding-the-data-from-source).

## Run it

Two processes, two terminals.

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

Then open the URL Vite prints, `http://localhost:5173` or the next free port if
something already holds that one. The UI checks the API on load, so an
unreachable backend is visible before you type a question rather than after
waiting on one.

For the same agent in the terminal, with no `npm` involved:

```
python -m cli
```

`cli` prints every tool call (name, arguments, full JSON result) above each
reply, so a transcript shows exactly which number came from where. The web UI
deliberately shows only the prose reply; `/chat` still returns the tool calls,
and `cli` is the place to check the "model never produces a number" rule.

## Tests

```
python -m pytest tests/ -v
```

`test_reproducible.py` and `test_config_consistency.py` cover the scoring model.
`test_agent_loop.py` and `test_api.py` cover the agent loop and the HTTP layer
against a fake OpenAI client, so the suite needs no API key and no network.

## Rebuilding the data from source

Only needed from scratch, or to change the month. Run in this order: the airport
dimension derives `hub_size` from T-100 passenger share, so T-100 must land
first.

```
python -m ingest.t100          # BTS T-100 Segment
python -m ingest.ontime        # BTS On-Time Performance
python -m ingest.airports      # OurAirports dimension
python -m scoring.metrics      # build the mart
python -m scripts.show_ranking # sanity-check the output
```

Each ingest module takes `--year` / `--month` (default May 2026) and prints a
validation report.

| Step | Writes | Notes |
|---|---|---|
| `ingest.t100` | `data/staging/t100_segment.parquet` | Carrier × origin × dest × month. Caches the raw zip in `data/raw/`, filters to passenger service, types the columns. See the docstring in [ingest/t100.py](ingest/t100.py) for why this is an ASP.NET form-post rather than a REST call. |
| `ingest.ontime` | `data/staging/ontime_airport_monthly.parquet` | Same form-post against a different TranStats table, aggregated to airport-month: `nas_delay_per_departure`, `taxi_out_p80`, `pct_delayed_15`, `cancellation_rate`, `reporting_carrier_count`. Source for the whole `capacity_strain` pillar. |
| `ingest.airports` | `data/staging/dim_airport.parquet` | OurAirports CSVs: IATA code, name, state, region (`config/regions.yaml`), coordinates, runway count, `hub_size`, `slot_controlled`. |
| `scoring.metrics` | `data/marts/mart_airport_metrics.parquet` | Joins the three above into one row per surviving origin airport. |
| `scripts.show_ranking` | stdout | National top 10 and the full New England ranking. |

Read [docs/DATA_RECON.md](docs/DATA_RECON.md) before touching the ingest layer.
Two things will bite you otherwise: the On-Time export's column names don't
match its own field-selection checkboxes, and OTP covers far fewer airports
than T-100.

**Behind a TLS-inspecting proxy**, `requests` to `transtats.bts.gov` will fail
on certificate verification, because the proxy's root CA is trusted by the OS
but not by Python's `certifi` bundle. Point `BTS_CA_BUNDLE` at a PEM containing
both the `certifi` roots and your proxy's CA; `ingest/common.py` passes it as
`requests`'s `verify=`. Unset, it falls back to `certifi`, which is correct on a
normal network.

## API reference

`POST /chat` takes `{message, history}` and returns `{reply, tool_calls, as_of}`.
`message` is capped at 2,000 characters and `history` at 40 turns, so an
oversized request is rejected before it becomes a paid model call. The backend
is stateless per request: the UI resends history every time.

`GET /health` returns the model name and the data vintage.

A failure at the model provider comes back as a readable status and `detail`
(502 for a bad key, 429 for rate limiting, 504 for a timeout) rather than a bare
500. Set `LOG_LEVEL=DEBUG` to see every model call and tool call.

The frontend talks to `http://localhost:8000` by default. To point it elsewhere,
copy `web/.env.example` to `web/.env` and set `VITE_API_URL`. If the backend is
unreachable the UI shows an error banner naming the URL it tried. A request in
flight can be cancelled with the Stop button, and times out client-side after
120s so a wedged backend never leaves the view animating forever.

**No auth, no rate limiting.** This is a localhost tool running against the
operator's own API key, deliberately not hardened for public exposure. Do not
put it on a public interface without putting something in front of it.

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
