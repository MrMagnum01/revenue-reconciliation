"""Orchestrates one reconciliation run: fetch orders, load CSVs, dedupe,
reconcile, write DuckDB, write the mismatch report.

Run status
----------
Each source (orders, payments, refunds) ends the run as one of:

  ok       - everything it delivered was accepted.
  partial  - some of it was rejected (API failure after some pages,
             malformed record/row, conflicting ids), but some was accepted.
  failed   - it reported an error and nothing usable was accepted.

The run is `complete` when every source is ok, `failed` when every source
failed, and `incomplete` otherwise. Status is written to the `runs` table
and to the report (`status`, `ok`); findings that would depend on the
absence of a record from an incomplete source are reported as
`indeterminate_incomplete_source` instead of being asserted.

One-row-per-id policy
---------------------
Exactly duplicated rows (same id, identical fields) are collapsed to one
event; rows sharing an id with different contents are all rejected as an
id conflict. The SAME accepted rows feed both the report and the database,
so the two always agree.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .api_client import OrdersApiClient
from .api_server import OrdersApiServer
from .generator import load_orders_fixture
from .loaders import load_payments, load_refunds
from .models import ApiFailure, IdConflict, Order, ParseError
from .reconcile import ReconciliationResult, dedupe_by_id, reconcile


def fetch_orders_from_api(api_url: str, *, timeout: float = 2.0, retries: int = 1) -> tuple[list[Order], list[ApiFailure]]:
    client = OrdersApiClient(api_url, timeout=timeout, retries=retries)
    return client.fetch_all_orders()


def fetch_orders_from_fixture(
    fixture_path: Path,
    *,
    fail_bulk: bool = False,
    timeout_bulk: bool = False,
    timeout: float = 1.0,
) -> tuple[list[Order], list[ApiFailure]]:
    """Spins up the in-process mock orders API from a JSON fixture, fetches
    the corpus over real HTTP, then tears the server down. `fail_bulk`
    /`timeout_bulk` let callers (tests, demo runs) inject an API outage."""
    orders = load_orders_fixture(fixture_path)
    server = OrdersApiServer(orders, fail_bulk=fail_bulk, timeout_bulk=timeout_bulk)
    server.start()
    try:
        client = OrdersApiClient(server.base_url, timeout=timeout, retries=1)
        return client.fetch_all_orders()
    finally:
        server.stop()


def _source_state(had_error: bool, accepted: int) -> str:
    if not had_error:
        return "ok"
    return "partial" if accepted else "failed"


def _run_status(states: dict[str, str]) -> str:
    if all(s == "ok" for s in states.values()):
        return "complete"
    if all(s == "failed" for s in states.values()):
        return "failed"
    return "incomplete"


def run_pipeline(
    orders: list[Order],
    order_failures: list[ApiFailure],
    payments_csv: Path,
    refunds_csv: Path,
    db_path: Path,
    report_dir: Path,
    run_id: str | None = None,
    source_note: str = "",
) -> dict:
    raw_payments, payment_errors = load_payments(Path(payments_csv))
    raw_refunds, refund_errors = load_refunds(Path(refunds_csv))

    # One-row-per-id policy, applied once; everything downstream (report
    # AND database) uses these accepted lists.
    orders, orders_dup, order_conflicts = dedupe_by_id(orders, lambda o: o.order_id, "orders")
    payments, payments_dup, payment_conflicts = dedupe_by_id(raw_payments, lambda p: p.payment_id, "payments")
    refunds, refunds_dup, refund_conflicts = dedupe_by_id(raw_refunds, lambda r: r.refund_id, "refunds")
    conflicts: list[IdConflict] = order_conflicts + payment_conflicts + refund_conflicts

    sources = {
        "orders": _source_state(bool(order_failures or order_conflicts), len(orders)),
        "payments": _source_state(bool(payment_errors or payment_conflicts), len(payments)),
        "refunds": _source_state(bool(refund_errors or refund_conflicts), len(refunds)),
    }
    status = _run_status(sources)
    incomplete_sources = [name for name, state in sources.items() if state != "ok"]

    result: ReconciliationResult = reconcile(
        orders,
        payments,
        refunds,
        orders_complete=sources["orders"] == "ok",
        payments_complete=sources["payments"] == "ok",
    )

    issues: list[dict] = []
    for f in order_failures:
        issues.append({"source": "orders", "kind": "api_failure", "category": f.reason,
                       "record_id": f.order_id, "line_number": None, "detail": f.detail})
    for name, errs in (("payments", payment_errors), ("refunds", refund_errors)):
        for e in errs:
            issues.append({"source": name, "kind": "parse_error", "category": e.category,
                           "record_id": None, "line_number": e.line_number, "detail": e.reason})
    for c in conflicts:
        issues.append({"source": c.source, "kind": "id_conflict", "category": "conflicting_id",
                       "record_id": c.record_id, "line_number": None, "detail": c.detail})

    run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(Path(db_path))
    try:
        db.write_run(
            con,
            run_id,
            datetime.now(timezone.utc).isoformat(),
            orders,
            payments,
            refunds,
            result.daily_kpis,
            result.mismatches,
            source_note,
            status=status,
            incomplete_sources=incomplete_sources,
            issues=issues,
        )
    finally:
        con.close()

    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    def _err_dict(e: ParseError) -> dict:
        return {"source": e.source, "line_number": e.line_number, "raw_row": e.raw_row,
                "category": e.category, "reason": e.reason}

    def _api_fail_dict(f: ApiFailure) -> dict:
        return {"order_id": f.order_id, "reason": f.reason, "detail": f.detail}

    def _conflict_dict(c: IdConflict) -> dict:
        return {"source": c.source, "record_id": c.record_id, "row_count": c.row_count, "detail": c.detail}

    report = {
        "run_id": run_id,
        "status": status,
        "ok": status == "complete",
        "sources": sources,
        "incomplete_sources": incomplete_sources,
        "totals_cents": result.totals(),
        "mismatches": result.mismatch_totals(),
        "api_failures": [_api_fail_dict(f) for f in order_failures],
        "parse_errors": [_err_dict(e) for e in (payment_errors + refund_errors)],
        "conflicts": [_conflict_dict(c) for c in conflicts],
        "conflicts_count": len(conflicts),
        "counts": {
            "orders_ingested": len(orders),
            "payments_loaded": len(payments),
            "refunds_loaded": len(refunds),
            "exact_duplicates_dropped": {"orders": orders_dup, "payments": payments_dup, "refunds": refunds_dup},
            "rows_rejected_for_conflict": sum(c.row_count for c in conflicts),
        },
    }
    (report_dir / "mismatch_report.json").write_text(json.dumps(report, indent=2, default=str))

    with (report_dir / "mismatches.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["category", "day", "order_id", "payment_id", "refund_id", "currency", "amount_cents", "details"])
        for m in result.mismatches:
            writer.writerow([m.category, m.day.isoformat(), m.order_id, m.payment_id, m.refund_id, m.currency, m.amount_cents, m.details])

    return {
        "result": result,
        "report": report,
        "payment_errors": payment_errors,
        "refund_errors": refund_errors,
        "conflicts": conflicts,
    }
