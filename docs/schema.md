# DuckDB schema

Written by `src/revenue_reconciliation/db.py` (`SCHEMA_SQL`) into whatever
file `--db` points at (e.g. `data/output/reconciliation.duckdb`). This
schema is deliberately kept stable and documented here because a later
KPI-dashboard demo reads this same database - the dashboard should not need
its own ETL, just queries against these tables.

**All money columns are `BIGINT` cents, never floats or decimals.** A
`amount_cents` of `1234` is $12.34. This project sums thousands of amounts
across totals, daily KPIs and mismatch reports; floating-point dollars
would drift by fractions of a cent, which breaks exact reconciliation.
Divide by 100 (or use DuckDB's `DECIMAL` cast) only at the presentation
layer, e.g. `amount_cents / 100.0 AS amount_usd`.

Every table except `runs` carries the raw ingested rows or the
reconciliation *output*, so a dashboard can always recompute a metric from
first principles instead of trusting only the summary tables.

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

One row per `(run_id, day)`. Kept per-run (not `INSERT OR REPLACE`) so a
dashboard can show a KPI's history across runs, not just the latest one.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR | see `runs` below |
| `day` | DATE | |
| `gross_cents` | BIGINT | sum of payments received on `day` (by `payment_date`) |
| `net_cents` | BIGINT | `gross_cents - refunds_cents` |
| `refunds_cents` | BIGINT | sum of refunds issued on `day` (by `refund_date`) |
| `paid_cents` | BIGINT | same as `gross_cents`; kept as its own column because "paid vs ordered" is one of the required KPIs and reads more clearly as two named columns |
| `ordered_cents` | BIGINT | sum of `completed` orders placed on `day` (by `order_date`) - **not** the same calendar basis as `paid_cents`, since a customer can pay a different day than they ordered; that gap is the point of tracking both |
| `unmatched_count` | BIGINT | payments on `day` whose `order_id` matches no known order |
| `unmatched_cents` | BIGINT | their total amount |

PRIMARY KEY `(run_id, day)`.

## `mismatches`

One row per categorised problem found by a run. Kept per-run, not
deduplicated across runs.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR | |
| `id` | BIGINT | sequence number within the run |
| `category` | VARCHAR | `missing_payment` \| `overpayment` \| `orphan_refund` \| `currency_mismatch` \| `duplicate` |
| `day` | DATE | the date the mismatch is attributed to (see `reconcile.py` for which date field each category uses) |
| `order_id` | VARCHAR NULLABLE | |
| `payment_id` | VARCHAR NULLABLE | |
| `refund_id` | VARCHAR NULLABLE | |
| `currency` | VARCHAR | |
| `amount_cents` | BIGINT | the amount at issue - see `reconcile.py` docstring for what this means per category (e.g. for `overpayment` it's the excess, not the full payment) |
| `details` | VARCHAR | human-readable explanation |

PRIMARY KEY `(run_id, id)`.

## `runs`

One row per pipeline run, so `daily_kpis` and `mismatches` rows can be
traced to when and from what source they came.

| Column | Type | Notes |
|---|---|---|
| `run_id` | VARCHAR PK | e.g. `run-20260601T120000` |
| `run_at` | TIMESTAMP | UTC |
| `source_note` | VARCHAR | the orders API URL or fixture path used for that run |

## Categorisation semantics (for `mismatches.category`)

- **`missing_payment`** - a `completed` order with zero payments recorded.
  `amount_cents` = the order's amount (the money never collected).
- **`overpayment`** - a payment greater than its order's amount.
  `amount_cents` = the excess only (`payment - order`), not the full payment.
- **`orphan_refund`** - a refund whose `order_id` matches no known order.
  `amount_cents` = the refund's amount.
- **`currency_mismatch`** - a payment whose currency differs from its
  order's currency. `amount_cents` = the payment's amount, in the payment's
  (mismatched) currency - no FX conversion happens anywhere in this project.
- **`duplicate`** - an order with more than one payment recorded for it;
  every payment after the first (ordered by date, then id) is flagged.
  `amount_cents` = that extra payment's amount.

"Unmatched" (`daily_kpis.unmatched_count` / `unmatched_cents`) is
deliberately **not** a `mismatches` category: it is a payment for an order
we have no record of at all, which is a different failure (bad or missing
order data) than any of the five categories above, all of which start from
a real order.
