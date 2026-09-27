# revenue-reconciliation

Reconciles orders, payments and refunds pulled from three different
sources into daily KPIs and a categorised mismatch report, stored in
DuckDB. Built for Upwork job types like "data reconciliation", "ETL
pipeline", and "Shopify/Stripe-style payments reconciliation" - this
project does not integrate with Shopify or Stripe and makes no claim to;
it demonstrates the reconciliation logic those jobs actually need against
a synthetic orders API and synthetic CSVs shaped like what those platforms
export.

**Everything in this repo is synthetic.** The included generator produces
~130 fake orders, ~120 payments and ~14 refunds with a fixed random seed,
plants a known set of mismatches, and records the true totals it built -
so the whole pipeline can be demoed and tested without any real business
data. No code, data, or client project is reused here.

**Role:** Automation engineer - designed and directed the build (AI-assisted
coding). Everything described below as built has working code and a
passing test in this repo; nothing here is described as done unless it is.

## What it does

1. **`generate`** - builds the deterministic synthetic corpus: an
   `orders.json` fixture (served by the mock orders API), `payments.csv`
   and `refunds.csv`, plus `truth.json` recording the exact totals and
   mismatch counts the generator planted.
2. **`reconcile`** - fetches orders over HTTP from the mock orders API
   (either a fixture-backed in-process server, or a real running one via
   `--orders-api-url`), loads the two CSVs, matches everything, and writes:
   - `reconciliation.duckdb` - `orders`, `payments`, `refunds`,
     `daily_kpis`, `mismatches`, `runs` tables (schema in
     [`docs/schema.md`](docs/schema.md), shared with a later KPI-dashboard
     demo).
   - `mismatch_report.json` / `mismatches.csv` - the same mismatch rows and
     aggregate totals, in a shape that's easy to alert on without a SQL
     query.
3. **`run`** - `generate` + `reconcile` in one step; this is what the
   scheduled job (below) calls.

### The mock orders API

A tiny HTTP server (`src/revenue_reconciliation/api_server.py`), stdlib
only (`http.server`), bound to **127.0.0.1 only**. It serves
`GET /orders?page=N` (paginated) and `GET /orders/<id>`, and can be told to
return HTTP 500 or hang for specific ids or for the whole bulk endpoint -
that's what the failure tests use to check the client's timeout and retry
handling. `reconcile --orders-fixture` starts one of these in-process
against a JSON fixture and tears it down afterwards; `--orders-api-url`
talks to an already-running one (real deployment shape).

### Mismatch categories

| Category | Meaning |
|---|---|
| `missing_payment` | A completed order with zero payments recorded. |
| `overpayment` | A payment greater than its order's amount (amount shown = excess only). |
| `orphan_refund` | A refund whose `order_id` matches no known order. |
| `currency_mismatch` | A payment whose currency differs from its order's. |
| `duplicate` | An order with more than one payment recorded for it. |

"Unmatched" (a KPI, not a mismatch category) is a payment whose order_id
matches no order at all - see [`docs/schema.md`](docs/schema.md) for why
that's kept separate.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## One-command run

```bash
./run_demo.sh
```

Generates the synthetic corpus into `data/raw/` and writes
`reconciliation.duckdb` + the mismatch report to `data/output/`. Both
directories are git-ignored (they're generated, not source).

## CLI, step by step

```bash
export PYTHONPATH=src

python -m revenue_reconciliation generate --out data/raw --seed 42

python -m revenue_reconciliation reconcile \
  --orders-fixture data/raw/orders.json \
  --payments data/raw/payments.csv \
  --refunds data/raw/refunds.csv \
  --db data/output/reconciliation.duckdb \
  --report-dir data/output

# or, in one step:
python -m revenue_reconciliation run --raw-dir data/raw --out data/output --seed 42
```

## Scheduled run

Not installed on the build box. `docs/scheduling.md` has a documented cron
example and a systemd-timer example (service + timer unit), both wrapping
the same `revenue_reconciliation run` CLI command shown above.

## Tests

```bash
source .venv/bin/activate
export PYTHONPATH=src
pytest tests -v
```

29/29 passing. Covers:
- **Known-total reconciliation** (`test_generator_truth.py`) - the
  generator plants a disjoint set of mismatches per category and records
  ground truth independently; `reconcile()`'s output is asserted equal to
  that ground truth, exactly, to the cent.
- **API failure handling** (`test_api_failures.py`) - per-order and
  bulk-endpoint HTTP 500s and timeouts, a closed port (connection refused),
  and successful pagination across multiple pages.
- **Malformed CSV handling** (`test_malformed_csv.py`) - bad amount, bad
  date, missing column, empty body - every case is skipped and reported as
  a `ParseError`, never a crash.
- **Reconciliation unit tests** (`test_reconcile_unit.py`) - one
  handcrafted case per mismatch category, isolated from the generator.
- **Pipeline + CLI** (`test_pipeline_cli.py`) - end-to-end DuckDB write,
  an API-outage run that still completes, and a subprocess smoke test of
  the CLI itself (`generate`, `reconcile`, `run`).

## Limits

- **No FX conversion.** Currency is read as the printed 3-letter code;
  `currency_mismatch` is detected, never auto-corrected or converted.
- **Reconciliation is a single pass over one `payments.csv`/`refunds.csv`
  pair per run** - it does not track state across runs (e.g. a payment
  that finally arrives a week late would show up as a new row in the next
  run's input, not be reconciled against last week's `missing_payment`).
  A persistent cross-run ledger is a reasonable next step, marked
  **proposed**, not built.
- **The orders API client fetches the whole corpus per run** (paginated,
  but not incremental/`since`-filtered). A `?since=` cursor is **proposed**
  for a production version with a large, growing order history; the mock
  API doesn't implement one because nothing here exercises it yet.
- **Alerting on the mismatch report is manual** (`docs/scheduling.md`
  shows the shape of `mismatch_report.json` for a threshold check) - no
  notification integration (email/Slack/etc.) is built.

## Project layout

```
src/revenue_reconciliation/
  models.py       Order / Payment / Refund / Mismatch / DailyKPI dataclasses (amounts in integer cents)
  api_server.py   mock orders REST API, 127.0.0.1-only, stdlib http.server
  api_client.py   HTTP client: timeout + retry, classifies every failure
  generator.py    deterministic synthetic corpus + planted mismatches + truth.json
  loaders.py      payments.csv / refunds.csv readers, malformed rows -> ParseError
  reconcile.py    matching + categorisation + daily KPI aggregation
  db.py           DuckDB schema + writer (see docs/schema.md)
  pipeline.py     orchestrates one run: fetch -> load -> reconcile -> write
  cli.py          `generate` / `reconcile` / `run` subcommands
tests/            pytest suite (see "Tests" above)
docs/schema.md    DuckDB schema, documented for reuse by a later dashboard demo
docs/scheduling.md  cron + systemd-timer examples (not installed here)
LICENSES.md       every open-source library used and its licence
```
