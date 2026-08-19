# Airport Investment Intelligence Agent

See [CLAUDE.md](CLAUDE.md) for the project brief and [docs/DATA_RECON.md](docs/DATA_RECON.md)
for how the data access paths were confirmed.

## Setup

```
python -m pip install -r requirements.txt
```

### TLS-intercepting network filters

If your network runs a TLS-inspecting proxy or content filter (common on
corporate and managed networks), its root CA is trusted at the OS level but
not by Python's `certifi` trust store, so `requests` calls to
`transtats.bts.gov` will fail with an SSL certificate error. Fix it with a CA
bundle that includes both the standard `certifi` roots and your proxy's root
CA.

Build one once (replace `YOUR_PROXY_NAME` with a substring that matches your
proxy's certificate in the Windows trust store -- check
`Cert:\LocalMachine\Root` in PowerShell if you're not sure what to search for):

```powershell
$out = "$PWD\.cache"
New-Item -ItemType Directory -Force -Path $out | Out-Null

$certifiPath = python -c "import certifi; print(certifi.where())"
Copy-Item $certifiPath "$out\combined_ca_bundle.pem" -Force

$proxyCerts = Get-ChildItem -Path Cert:\LocalMachine\Root | Where-Object { $_.Subject -like "*YOUR_PROXY_NAME*" }
foreach ($c in $proxyCerts) {
  $b64 = [System.Convert]::ToBase64String($c.GetRawCertData(), [System.Base64FormattingOptions]::InsertLineBreaks)
  "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----" | Out-File -Append -Encoding ascii "$out\combined_ca_bundle.pem"
}
```

Then point ingest scripts at it:

```powershell
$env:BTS_CA_BUNDLE = "$PWD\.cache\combined_ca_bundle.pem"
```

`BTS_CA_BUNDLE` is read by `ingest/common.py`'s `ca_bundle_verify()`, used by
every `ingest/` module, and passed as `requests`'s `verify=` argument. If
unset, requests falls back to the default `certifi` trust store, which is
the right behavior on a network without TLS interception.

`.cache/` is machine-specific and is gitignored -- regenerate it on any new
machine rather than copying it.

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
surviving origin airport) and prints threshold/coverage diagnostics.
`scoring/score.py` turns that into percentile-based pillar scores and a
weighted composite per `config/weights.yaml`; `scripts/show_ranking.py`
prints the national top 10 and the full New England ranking.

## Tests

```
python -m pytest tests/ -v
```
