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

**Provenance:** This repository was implemented by AI coding agents and
independently reviewed by a separate AI reviewer across two review rounds
(findings and fixes are not narrated here; see the repo history). Everything
described below as built has working code and a passing test in this repo;
nothing here is described as done unless it is. No claim is made about
review or approval by anyone outside that AI-agent/AI-reviewer loop.

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
     query. The report carries a top-level `status` (`complete` /
     `incomplete` / `failed`) and `ok` flag, and the CLI exits non-zero
     unless the run is `complete` (see "Run status" below).
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

### Money is never combined across currencies

There is no FX conversion anywhere. Every money total - report totals,
daily KPIs (one row per `(run_id, day, currency)`), and per-category
mismatch amounts - is partitioned by currency. A USD 100 payment and a
EUR 100 payment are reported as `USD 10000c` and `EUR 10000c`, never as
one `20000c` figure. Only counts (e.g. `unmatched_count`) are summed across
currencies.

### Mismatch categories

| Category | Meaning |
|---|---|
| `missing_payment` | A completed order with zero payments recorded (asserted only when the payments source is complete). |
| `overpayment` | A payment greater than its order's amount, **same currency only** (amount shown = excess). |
| `orphan_refund` | A refund whose `order_id` matches no known order (asserted only when the orders source is complete). |
| `currency_mismatch` | A payment whose currency differs from its order's. This is the only finding for such a payment - amounts in different currencies are never compared, so no over/under-payment is computed. |
| `duplicate` | An order with more than one (distinct) payment recorded for it. |
| `indeterminate_incomplete_source` | A finding that would rest on a record being *absent* (missing payment, orphan refund, unmatched payment) while the source that should hold it is incomplete this run. Reported separately instead of asserted. |

"Unmatched" (a KPI, not a mismatch category) is a payment whose order_id
matches no order at all - see [`docs/schema.md`](docs/schema.md) for why
that's kept separate.

### Run status

Each source (orders API, payments CSV, refunds CSV) ends a run as `ok`,
`partial` (some records rejected: API failure mid-listing, malformed
record/row, conflicting ids) or `failed` (error and nothing usable). The
run is `complete` only if every source is `ok`, `failed` if every source
failed, otherwise `incomplete`. Status and the list of incomplete sources
are written to the `runs` table, each failure/rejection to `run_issues`,
and both to the report. `reconcile` / `run` exit `0` for `complete`, `2`
for `incomplete`, `1` for `failed` or an error. A database consumer can
therefore tell an orders-API outage from a clean run with real unmatched
payments without reading the JSON report.

### One row per source id

The pipeline treats one row per source id (`order_id`, `payment_id`,
`refund_id`) as one event:

- **Exact duplicates** (same id, identical fields) are collapsed to one
  row before anything else, so the report and the database count it once.
- **Conflicting ids** (same id, different fields) are a data error: all
  rows with that id are rejected (not guessed between), excluded from
  totals and the database, reported under `conflicts` / `conflicts_count`
  and in `run_issues`, and the source is marked `partial`.
- The same accepted rows feed both the report and the database.

**Known limitation:** the pipeline assumes one payment id per order. A
genuine split tender - two distinct charges, each with its **own distinct
payment id**, paying one order - is not treated as a conflict; it is
matched normally and, since more than one payment now exists for that
order, every payment after the first is flagged `duplicate`. Two rows
sharing the *same* payment id with different contents remain a rejected id
conflict. The pipeline does not attempt to distinguish a legitimate
multi-payment order from a true duplicate.

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

60/60 passing. Covers:
- **Known-total reconciliation** (`test_generator_truth.py`) - the
  generator plants a disjoint set of mismatches per category and records
  ground truth independently; `reconcile()`'s output is asserted equal to
  that ground truth, exactly, to the cent.
- **API failure handling** (`test_api_failures.py`) - per-order and
  bulk-endpoint HTTP 500s and timeouts, a closed port (connection refused),
  successful pagination across multiple pages, malformed records (missing
  field, string/bool/fractional cents) skipped and reported as
  `malformed_record`, and bounded pagination (missing page size, empty
  pages, a listing that never reaches its advertised total).
- **Malformed CSV handling** (`test_malformed_csv.py`) - bad amount, bad
  date, missing column, empty body - every case is skipped and reported as
  a `ParseError`, never a crash.
- **Review probes** (`test_astra_probes.py`) - regression tests from an
  independent review: per-currency totals, no cross-currency overpayment,
  short and extra-column CSV rows, exact-duplicate and conflicting ids
  (report and DB agree), API outage recorded as `incomplete` with nothing
  asserted as unmatched, per-run KPI history preserved, all-or-nothing DB
  writes, malformed API records, fractional cents rejected.
- **Re-review probes** (`test_astra_rereview_probes.py`) - a second
  independent-review round: a zero-byte source file is never treated as a
  complete empty source, a duplicate CSV header column is rejected instead
  of silently overwriting an earlier column's value, a currency string that
  collides with a reserved report key (e.g. `unmatched_count`) is rejected
  at ingestion in both the CSV loaders and the API client, a paginated API
  fetch that stops short of its advertised total (a truncated or empty
  last page) is reported incomplete rather than counted as success, and a
  server that changes its advertised total partway through a fetch (e.g.
  page 1 says 3, page 2 says 2) is reported incomplete rather than judged
  against whichever total the last page happened to say.
- **Reconciliation unit tests** (`test_reconcile_unit.py`) - one
  handcrafted case per mismatch category, isolated from the generator.
- **Pipeline + CLI** (`test_pipeline_cli.py`) - end-to-end DuckDB write
  (report totals = DB KPI sums = raw table sums, per currency), an
  API-outage run recorded as `incomplete` in both report and `runs`, and a
  subprocess smoke test of the CLI itself (`generate`, `reconcile`, `run`,
  and a non-zero exit when the orders API is down).

## Limits

- **No FX conversion.** Currency is read as the printed 3-letter code;
  `currency_mismatch` is detected, never auto-corrected or converted, and
  totals are reported per currency rather than as one grand total.
- **Only `USD`, `EUR` and `GBP` are accepted currencies** (CSV rows and API
  order records alike). Anything else - a typo, an unsupported code, or a
  string chosen to collide with a report key such as `unmatched_count` - is
  rejected at ingestion as an invalid record, not silently accepted.
- **Raw tables are latest-state, not history.** `daily_kpis`,
  `mismatches`, `runs` and `run_issues` are kept per `run_id`, so earlier
  runs' KPIs stay as they were computed. The raw `orders`, `payments` and
  `refunds` tables are upserted by id and hold only the most recently
  ingested version of each record; they cannot be used to replay or
  recompute an earlier run. Per-run raw snapshots are **not built**.
- **Writes are all-or-nothing per run** (one transaction); a failed write
  leaves no rows from that run. A database created by an earlier schema
  version is refused with a clear error - point `--db` at a new file.
- **Split tender across distinct payment ids is not distinguished from a
  legitimate multi-payment order** - both are matched normally, and every
  payment after the first for an order is flagged `duplicate`. Split tender
  under **one shared** payment id is rejected as an id conflict instead (see
  "One row per source id").
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
  reconcile.py    one-row-per-id dedupe, matching, categorisation, per-currency daily KPIs
  db.py           DuckDB schema + transactional writer (see docs/schema.md)
  pipeline.py     orchestrates one run: fetch -> load -> dedupe -> reconcile -> write, sets run status
  cli.py          `generate` / `reconcile` / `run` subcommands
tests/            pytest suite (see "Tests" above)
docs/schema.md    DuckDB schema, documented for reuse by a later dashboard demo
docs/scheduling.md  cron + systemd-timer examples (not installed here)
LICENSES.md       every open-source library used and its licence
```
