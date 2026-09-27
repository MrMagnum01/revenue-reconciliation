"""Orchestrates one reconciliation run: fetch orders, load CSVs, reconcile,
write DuckDB, write the mismatch report."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .api_client import OrdersApiClient
from .api_server import OrdersApiServer
from .generator import load_orders_fixture
from .loaders import load_payments, load_refunds
from .models import ApiFailure, Order, ParseError
from .reconcile import ReconciliationResult, reconcile


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
    payments, payment_errors = load_payments(Path(payments_csv))
    refunds, refund_errors = load_refunds(Path(refunds_csv))

    result: ReconciliationResult = reconcile(orders, payments, refunds)

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
        )
    finally:
        con.close()

    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    def _err_dict(e: ParseError) -> dict:
        return {"source": e.source, "line_number": e.line_number, "raw_row": e.raw_row, "reason": e.reason}

    def _api_fail_dict(f: ApiFailure) -> dict:
        return {"order_id": f.order_id, "reason": f.reason, "detail": f.detail}

    report = {
        "run_id": run_id,
        "totals_cents": result.totals(),
        "mismatches": result.mismatch_totals(),
        "api_failures": [_api_fail_dict(f) for f in order_failures],
        "parse_errors": [_err_dict(e) for e in (payment_errors + refund_errors)],
        "counts": {
            "orders_ingested": len(orders),
            "payments_loaded": len(payments),
            "refunds_loaded": len(refunds),
        },
    }
    (report_dir / "mismatch_report.json").write_text(json.dumps(report, indent=2, default=str))

    with (report_dir / "mismatches.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["category", "day", "order_id", "payment_id", "refund_id", "currency", "amount_cents", "details"])
        for m in result.mismatches:
            writer.writerow([m.category, m.day.isoformat(), m.order_id, m.payment_id, m.refund_id, m.currency, m.amount_cents, m.details])

    return {"result": result, "report": report, "payment_errors": payment_errors, "refund_errors": refund_errors}
