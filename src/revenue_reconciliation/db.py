"""DuckDB schema and writer for reconciled output.

Schema is documented in `docs/schema.md` - keep the two in sync, since a
later KPI-dashboard demo reads this same database.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .models import DailyKPI, Mismatch, Order, Payment, Refund

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id      VARCHAR PRIMARY KEY,
    customer_id   VARCHAR NOT NULL,
    order_date    DATE NOT NULL,
    currency      VARCHAR NOT NULL,
    amount_cents  BIGINT NOT NULL,
    status        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id    VARCHAR PRIMARY KEY,
    order_id      VARCHAR NOT NULL,
    payment_date  DATE NOT NULL,
    currency      VARCHAR NOT NULL,
    amount_cents  BIGINT NOT NULL,
    method        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id     VARCHAR PRIMARY KEY,
    order_id      VARCHAR NOT NULL,
    refund_date   DATE NOT NULL,
    currency      VARCHAR NOT NULL,
    amount_cents  BIGINT NOT NULL,
    reason        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_kpis (
    run_id           VARCHAR NOT NULL,
    day              DATE NOT NULL,
    gross_cents      BIGINT NOT NULL,
    net_cents        BIGINT NOT NULL,
    refunds_cents    BIGINT NOT NULL,
    paid_cents       BIGINT NOT NULL,
    ordered_cents    BIGINT NOT NULL,
    unmatched_count  BIGINT NOT NULL,
    unmatched_cents  BIGINT NOT NULL,
    PRIMARY KEY (run_id, day)
);

CREATE TABLE IF NOT EXISTS mismatches (
    run_id        VARCHAR NOT NULL,
    id            BIGINT NOT NULL,
    category      VARCHAR NOT NULL,
    day           DATE NOT NULL,
    order_id      VARCHAR,
    payment_id    VARCHAR,
    refund_id     VARCHAR,
    currency      VARCHAR NOT NULL,
    amount_cents  BIGINT NOT NULL,
    details       VARCHAR NOT NULL,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      VARCHAR PRIMARY KEY,
    run_at      TIMESTAMP NOT NULL,
    source_note VARCHAR
);
"""


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA_SQL)
    return con


def write_run(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    run_at: str,
    orders: list[Order],
    payments: list[Payment],
    refunds: list[Refund],
    daily_kpis: list[DailyKPI],
    mismatches: list[Mismatch],
    source_note: str = "",
) -> None:
    con.execute("INSERT INTO runs VALUES (?, ?, ?)", [run_id, run_at, source_note])

    def _executemany(sql: str, rows: list[list]) -> None:
        if rows:
            con.executemany(sql, rows)

    _executemany(
        "INSERT OR REPLACE INTO orders VALUES (?, ?, ?, ?, ?, ?)",
        [[o.order_id, o.customer_id, o.order_date, o.currency, o.amount_cents, o.status] for o in orders],
    )
    _executemany(
        "INSERT OR REPLACE INTO payments VALUES (?, ?, ?, ?, ?, ?)",
        [[p.payment_id, p.order_id, p.payment_date, p.currency, p.amount_cents, p.method] for p in payments],
    )
    _executemany(
        "INSERT OR REPLACE INTO refunds VALUES (?, ?, ?, ?, ?, ?)",
        [[r.refund_id, r.order_id, r.refund_date, r.currency, r.amount_cents, r.reason] for r in refunds],
    )
    _executemany(
        "INSERT INTO daily_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [run_id, k.day, k.gross_cents, k.net_cents, k.refunds_cents, k.paid_cents, k.ordered_cents, k.unmatched_count, k.unmatched_cents]
            for k in daily_kpis
        ],
    )
    _executemany(
        "INSERT INTO mismatches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            [run_id, i, m.category, m.day, m.order_id, m.payment_id, m.refund_id, m.currency, m.amount_cents, m.details]
            for i, m in enumerate(mismatches, start=1)
        ],
    )
