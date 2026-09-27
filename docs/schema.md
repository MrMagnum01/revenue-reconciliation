# DuckDB schema

Written by `src/revenue_reconciliation/db.py` (`SCHEMA_SQL`) into whatever
file `--db` points at (e.g. `data/output/reconciliation.duckdb`). This
schema is deliberately kept stable and documented here because a later
KPI-dashboard demo reads this same database - the dashboard should not need
its own ETL, just queries against these tables.

**All money columns are `BIGINT` cents, never floats or decimals.** A
`amount_cents` of `1234` is 12.34 in that row's currency. This project sums thousands of amounts
across totals, daily KPIs and mismatch reports; floating-point dollars
would drift by fractions of a cent, which breaks exact reconciliation.
Divide by 100 (or use DuckDB's `DECIMAL` cast) only at the presentation
layer, e.g. `amount_cents / 100.0`.

**Money is never summed across currencies.** There is no FX conversion.
Every money column sits next to a `currency` column, `daily_kpis` has one
row per `(run_id, day, currency)`, and any aggregate query must
`GROUP BY currency` - a `SUM(amount_cents)` over mixed currencies is
meaningless.

**What is historical and what is not.** Per-run tables (`runs`,
`run_issues`, `daily_kpis`, `mismatches`) are append-only by `run_id`:
an earlier run's KPIs and findings stay exactly as that run computed them.
The raw tables (`orders`, `payments`, `refunds`) are **latest-state only**:
they are upserted by id, so they hold the most recently ingested version
of each record. They can be used to recompute the latest run's metrics,
but **not** to replay or recompute an earlier run - that history is not
kept.

**Atomic runs.** One run's writes to all tables happen in a single
transaction. If any write fails, the transaction is rolled back and the
run leaves no rows anywhere (no `runs` row, no raw upserts).

**One row per source id.** Exact duplicate input rows are collapsed to one
before writing; rows that share an id but differ are all rejected (see
`run_issues`, kind `id_conflict`). The raw tables and the per-run tables
are written from the same accepted rows, so for the latest run
`SUM(payments.amount_cents)` per currency equals `SUM(daily_kpis.paid_cents)`
per currency.

**Schema version.** A database created by an earlier version of this
schema (no `daily_kpis.currency`, no `runs.status`) is refused on connect
with a clear error; write to a new database file.

## `orders`

One row per order, as ingested from the mock orders API for the most
recent run that touched it (`INSERT OR REPLACE`, keyed by `order_id`).

| Column | Type | Notes |
|---|---|---|
| `order_id` | VARCHAR PK | |
| `customer_id` | VARCHAR | |
| `order_date` | DATE | |
| `currency` | VARCHAR | 3-letter code, printed as received, never converted |
| `amount_cents` | BIGINT | |
| `status` | VARCHAR | `completed` \| `cancelled` |

## `payments`

One row per payment, as loaded from `payments.csv` (`INSERT OR REPLACE`,
keyed by `payment_id`).

| Column | Type | Notes |
|---|---|---|
| `payment_id` | VARCHAR PK | |
| `order_id` | VARCHAR | not a foreign key - an unmatched payment (no such order) is expected data, not a schema violation |
| `payment_date` | DATE | |
| `currency` | VARCHAR | |
| `amount_cents` | BIGINT | |
| `method` | VARCHAR | `card` \| `bank_transfer` \| `wallet` |

## `refunds`

One row per refund, as loaded from `refunds.csv` (`INSERT OR REPLACE`,
keyed by `refund_id`).

| Column | Type | Notes |
|---|---|---|
| `refund_id` | VARCHAR PK | |
| `order_id` | VARCHAR | not a foreign key, same reasoning as `payments.order_id` |
| `refund_date` | DATE | |
| `currency` | VARCHAR | |
| `amount_cents` | BIGINT | |
| `reason` | VARCHAR | `customer_return` \| `duplicate_charge` \| `service_issue` |

## `daily_kpis`

One row per `(run_id, day, currency)`. Kept per-run (not `INSERT OR
REPLACE`) so a dashboard can show a KPI's history across runs, not just
the latest one. All money columns are in that row's `currency`.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR | see `runs` below |
| `day` | DATE | |
| `currency` | VARCHAR | 3-letter code; amounts in different currencies are in different rows |
| `gross_cents` | BIGINT | sum of payments received on `day` (by `payment_date`) |
| `net_cents` | BIGINT | `gross_cents - refunds_cents` |
| `refunds_cents` | BIGINT | sum of refunds issued on `day` (by `refund_date`) |
| `paid_cents` | BIGINT | same as `gross_cents`; kept as its own column because "paid vs ordered" is one of the required KPIs and reads more clearly as two named columns |
| `ordered_cents` | BIGINT | sum of `completed` orders placed on `day` (by `order_date`) - **not** the same calendar basis as `paid_cents`, since a customer can pay a different day than they ordered; that gap is the point of tracking both |
| `unmatched_count` | BIGINT | payments on `day` in `currency` whose `order_id` matches no known order (only when the orders source was complete - otherwise such payments are `indeterminate_incomplete_source` mismatches and not counted here) |
| `unmatched_cents` | BIGINT | their total amount |

PRIMARY KEY `(run_id, day, currency)`.

## `mismatches`

One row per categorised problem found by a run. Kept per-run, not
deduplicated across runs.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR | |
| `id` | BIGINT | sequence number within the run |
| `category` | VARCHAR | `missing_payment` \| `overpayment` \| `orphan_refund` \| `currency_mismatch` \| `duplicate` \| `indeterminate_incomplete_source` |
| `day` | DATE | the date the mismatch is attributed to (see `reconcile.py` for which date field each category uses) |
| `order_id` | VARCHAR NULLABLE | |
| `payment_id` | VARCHAR NULLABLE | |
| `refund_id` | VARCHAR NULLABLE | |
| `currency` | VARCHAR | the currency of `amount_cents` |
| `amount_cents` | BIGINT | the amount at issue - see `reconcile.py` docstring for what this means per category (e.g. for `overpayment` it's the excess, not the full payment) |
| `details` | VARCHAR | human-readable explanation |

PRIMARY KEY `(run_id, id)`.

## `runs`

One row per committed pipeline run, so `daily_kpis` and `mismatches` rows
can be traced to when and from what source they came, and whether that
run's inputs were complete.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR PK | e.g. `run-20260601T120000` |
| `run_at` | TIMESTAMP | UTC |
| `source_note` | VARCHAR | the orders API URL or fixture path used for that run |
| `status` | VARCHAR | `complete` (every source fully accepted) \| `incomplete` (at least one source failed or had records rejected) \| `failed` (no source delivered usable data) |
| `incomplete_sources` | VARCHAR | comma-separated subset of `orders,payments,refunds`; `''` when complete |
| `issue_count` | BIGINT | number of `run_issues` rows for this run |

A dashboard should treat a run whose `status` is not `complete` as
partial: its `unmatched_*` / `missing_payment` / `orphan_refund` figures
exclude anything that could not be determined (see
`indeterminate_incomplete_source`).

## `run_issues`

One row per ingestion problem in a run: every API failure, CSV parse
error, and id conflict.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR | |
| `id` | BIGINT | sequence number within the run |
| `source` | VARCHAR | `orders` \| `payments` \| `refunds` |
| `kind` | VARCHAR | `api_failure` \| `parse_error` \| `id_conflict` |
| `category` | VARCHAR | api: `timeout` \| `http_error` \| `connection_error` \| `malformed_record` \| `malformed_response`; csv: `missing_columns` \| `wrong_field_count` \| `invalid_value`; conflict: `conflicting_id` |
| `record_id` | VARCHAR NULLABLE | the order/payment/refund id, when known |
| `line_number` | BIGINT NULLABLE | CSV line, for parse errors |
| `detail` | VARCHAR | human-readable explanation |

PRIMARY KEY `(run_id, id)`.

## Categorisation semantics (for `mismatches.category`)

- **`missing_payment`** - a `completed` order with zero payments recorded.
  `amount_cents` = the order's amount (the money never collected).
- **`overpayment`** - a payment greater than its order's amount, where
  both are in the same currency. `amount_cents` = the excess only
  (`payment - order`), not the full payment. Never computed across
  currencies.
- **`orphan_refund`** - a refund whose `order_id` matches no known order.
  `amount_cents` = the refund's amount.
- **`currency_mismatch`** - a payment whose currency differs from its
  order's currency. `amount_cents` = the payment's amount, in the payment's
  (mismatched) currency - no FX conversion happens anywhere in this project.
  This is the only finding emitted for such a payment: no overpayment or
  shortfall is derived by subtracting amounts in different currencies.
- **`duplicate`** - an order with more than one payment recorded for it;
  every payment after the first (ordered by date, then id) is flagged.
  `amount_cents` = that extra payment's amount. (Exact duplicate rows of
  one payment id are collapsed before this check and are not duplicates.)
- **`indeterminate_incomplete_source`** - a finding that would rest on a
  record's absence from a source that is incomplete this run: a completed
  order with no accepted payment while the payments source is incomplete,
  or a payment/refund whose order is not in this run's orders while the
  orders source is incomplete. `amount_cents` = the order's, payment's or
  refund's amount. Not a data error in itself - a statement that the run
  cannot decide.

"Unmatched" (`daily_kpis.unmatched_count` / `unmatched_cents`) is
deliberately **not** a `mismatches` category: it is a payment for an order
we have no record of at all, which is a different failure (bad or missing
order data) than any of the five categories above, all of which start from
a real order.
