"""DuckDB schema and writer for reconciled output.

Schema is documented in `docs/schema.md` - keep the two in sync, since a
later KPI-dashboard demo reads this same database.

Every run's writes (runs row, raw upserts, daily_kpis, mismatches,
run_issues) happen in ONE transaction: either all of them commit, or -
on any exception - none of them do.
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
    currency         VARCHAR NOT NULL,
    gross_cents      BIGINT NOT NULL,
    net_cents        BIGINT NOT NULL,
    refunds_cents    BIGINT NOT NULL,
    paid_cents       BIGINT NOT NULL,
    ordered_cents    BIGINT NOT NULL,
    unmatched_count  BIGINT NOT NULL,
    unmatched_cents  BIGINT NOT NULL,
    PRIMARY KEY (run_id, day, currency)
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
    run_id              VARCHAR PRIMARY KEY,
    run_at              TIMESTAMP NOT NULL,
    source_note         VARCHAR,
    status              VARCHAR NOT NULL,   -- complete | incomplete | failed
    incomplete_sources  VARCHAR NOT NULL,   -- comma-separated, '' when complete
    issue_count         BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_issues (
    run_id       VARCHAR NOT NULL,
    id           BIGINT NOT NULL,
    source       VARCHAR NOT NULL,   -- orders | payments | refunds
    kind         VARCHAR NOT NULL,   -- api_failure | parse_error | id_conflict
    category     VARCHAR NOT NULL,   -- e.g. http_error, malformed_record, wrong_field_count
    record_id    VARCHAR,
    line_number  BIGINT,
    detail       VARCHAR NOT NULL,
    PRIMARY KEY (run_id, id)
);
"""

# Columns a database must have to be written by this version. A database
# created by an earlier schema (before per-currency KPIs and run status)
# is refused with a clear message instead of failing mid-run.
_REQUIRED_COLUMNS = {
    "daily_kpis": {"currency"},
    "runs": {"status", "incomplete_sources", "issue_count"},
}


class SchemaVersionError(RuntimeError):
    pass


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(db_path))
    try:
        con.execute(SCHEMA_SQL)
        for table, needed in _REQUIRED_COLUMNS.items():
            have = {row[1] for row in con.execute(f"pragma table_info('{table}')").fetchall()}
            missing = needed - have
            if missing:
                raise SchemaVersionError(
                    f"{db_path}: table {table!r} lacks columns {sorted(missing)} - it was created by an older "
                    "schema version; write to a new --db file"
                )
    except Exception:
        con.close()
        raise
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
    *,
    status: str = "complete",
    incomplete_sources: list[str] | None = None,
    issues: list[dict] | None = None,
) -> None:
    """Write one run atomically. `issues` rows are dicts with keys
    source, kind, category, record_id, line_number, detail."""
    issues = issues or []
    incomplete_sources = incomplete_sources or []

    def _executemany(sql: str, rows: list[list]) -> None:
        if rows:
            con.executemany(sql, rows)

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, run_at, source_note, status, ",".join(incomplete_sources), len(issues)],
        )
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
            "INSERT INTO daily_kpis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [run_id, k.day, k.currency, k.gross_cents, k.net_cents, k.refunds_cents, k.paid_cents,
                 k.ordered_cents, k.unmatched_count, k.unmatched_cents]
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
        _executemany(
            "INSERT INTO run_issues VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                [run_id, i, x["source"], x["kind"], x["category"], x.get("record_id"), x.get("line_number"), x["detail"]]
                for i, x in enumerate(issues, start=1)
            ],
        )
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")
