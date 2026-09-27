"""Regression tests derived from the independent review's probe script.

Each test is one probe scenario, asserting the CORRECT behaviour. The
scenarios are the reviewer's own (same records, same calls); only the temp
paths were adapted to pytest's `tmp_path`.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path

import duckdb
import pytest

from revenue_reconciliation.api_client import OrdersApiClient, _order_from_json
from revenue_reconciliation.loaders import load_payments
from revenue_reconciliation.models import ApiFailure, MalformedRecordError, Order, Payment
from revenue_reconciliation.pipeline import run_pipeline
from revenue_reconciliation.reconcile import reconcile

D = date(2026, 6, 1)
HEADER = "payment_id,order_id,payment_date,currency,amount_cents,method\n"


def order(i="o", cur="USD", amt=10000):
    return Order(i, "synthetic", D, cur, amt, "completed")


def pay(i="p", o="o", cur="USD", amt=10000):
    return Payment(i, o, D, cur, amt, "card")


@pytest.fixture
def csvs(tmp_path: Path):
    p = tmp_path / "payments.csv"
    r = tmp_path / "refunds.csv"
    r.write_text("refund_id,order_id,refund_date,currency,amount_cents,reason\n")
    return tmp_path, p, r


def _all_ints(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _all_ints(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_ints(v)
    elif isinstance(obj, int) and not isinstance(obj, bool):
        yield obj


def test_mixed_currency_totals_are_partitioned_by_currency():
    result = reconcile([order("u"), order("e", "EUR")], [pay("u", "u"), pay("e", "e", "EUR")], [])
    totals = result.totals()
    assert totals["USD"]["paid_cents"] == 10000
    assert totals["EUR"]["paid_cents"] == 10000
    for cur in ("USD", "EUR"):
        assert totals[cur] == {
            "gross_cents": 10000, "net_cents": 10000, "refunds_cents": 0,
            "paid_cents": 10000, "ordered_cents": 10000, "unmatched_cents": 0,
        }
    assert totals["unmatched_count"] == 0
    # No combined USD+EUR figure anywhere in the structure.
    assert 20000 not in list(_all_ints(totals))
    # KPI rows are per (day, currency) too.
    assert sorted((k.day, k.currency, k.paid_cents) for k in result.daily_kpis) == [(D, "EUR", 10000), (D, "USD", 10000)]


def test_cross_currency_excess_is_only_a_currency_mismatch():
    mismatches = [asdict(m) for m in reconcile([order()], [pay(cur="EUR", amt=20000)], []).mismatches]
    assert len(mismatches) == 1
    assert mismatches[0]["category"] == "currency_mismatch"
    assert mismatches[0]["currency"] == "EUR"
    assert mismatches[0]["amount_cents"] == 20000
    assert not any(m["category"] == "overpayment" for m in mismatches)


def test_short_row_is_a_categorised_error_not_a_typeerror(csvs):
    _, p, _ = csvs
    p.write_text(HEADER + "p,o,2026-06-01\n")
    # Default mode (the loaders' skip-and-report convention): no exception
    # escapes; the row is skipped and reported with its line and cause.
    payments, errors = load_payments(p)
    assert payments == []
    assert len(errors) == 1
    assert errors[0].line_number == 2
    assert errors[0].category == "wrong_field_count"
    assert "expected 6 fields" in errors[0].reason and "got 3" in errors[0].reason
    assert errors[0].raw_row == "p,o,2026-06-01"
    # Strict mode: a clear, named exception (not TypeError), whose message
    # was built without error.
    with pytest.raises(MalformedRecordError) as exc_info:
        load_payments(p, strict=True)
    assert not isinstance(exc_info.value, TypeError)
    msg = str(exc_info.value)
    assert ":2: wrong_field_count:" in msg and "got 3" in msg


def test_extra_column_row_is_a_categorised_error(csvs):
    _, p, _ = csvs
    p.write_text(HEADER + "p,o,2026-06-01,USD,10000,card,EXTRA\np2,o,2026-06-01,USD,500,card\n")
    payments, errors = load_payments(p)
    assert [x.payment_id for x in payments] == ["p2"]
    assert len(errors) == 1
    assert errors[0].line_number == 2
    assert errors[0].category == "wrong_field_count"
    assert "got 7" in errors[0].reason
    with pytest.raises(MalformedRecordError):
        load_payments(p, strict=True)


def test_repeated_payment_id_report_and_db_agree(csvs):
    t, p, r = csvs
    p.write_text(HEADER + "p,o,2026-06-01,USD,10000,card\np,o,2026-06-01,USD,10000,card\n")
    out = run_pipeline([order()], [], p, r, t / "duplicate.db", t / "dup", run_id="dup")
    report_paid = out["report"]["totals_cents"]["USD"]["paid_cents"]
    con = duckdb.connect(str(t / "duplicate.db"))
    try:
        db_paid = con.execute("select sum(amount_cents) from payments").fetchone()[0]
        kpi_paid = con.execute("select sum(paid_cents) from daily_kpis where run_id='dup'").fetchone()[0]
    finally:
        con.close()
    assert report_paid == db_paid == kpi_paid == 10000
    assert out["report"]["counts"]["exact_duplicates_dropped"]["payments"] == 1
    assert out["report"]["conflicts_count"] == 0
    assert out["report"]["status"] == "complete"
    # An exact duplicate row is one event, not a second payment.
    assert "duplicate" not in out["report"]["mismatches"]


def test_conflicting_payment_id_is_rejected_not_guessed(csvs):
    t, p, r = csvs
    p.write_text(HEADER + "p,o,2026-06-01,USD,10000,card\np,o,2026-06-01,USD,20000,card\nq,o2,2026-06-01,USD,700,card\n")
    orders = [order(), order("o2", amt=700)]
    out = run_pipeline(orders, [], p, r, t / "conflict.db", t / "conflict", run_id="conflict")
    report = out["report"]
    assert report["conflicts_count"] == 1
    assert report["conflicts"][0]["record_id"] == "p"
    assert report["conflicts"][0]["row_count"] == 2
    # Neither conflicting row is counted; only q is.
    assert report["totals_cents"]["USD"]["paid_cents"] == 700
    assert report["status"] == "incomplete"
    assert report["sources"]["payments"] == "partial"
    # Order o's payment was rejected, so "no payment" is not asserted.
    assert "missing_payment" not in report["mismatches"]
    assert report["mismatches"]["indeterminate_incomplete_source"]["count"] == 1
    con = duckdb.connect(str(t / "conflict.db"))
    try:
        assert con.execute("select sum(amount_cents) from payments").fetchone()[0] == 700
        assert con.execute("select count(*) from payments where payment_id='p'").fetchone()[0] == 0
        assert con.execute("select kind, record_id from run_issues").fetchall() == [("id_conflict", "p")]
        assert con.execute("select status from runs").fetchone()[0] == "incomplete"
    finally:
        con.close()


def test_outage_is_recorded_as_incomplete_and_nothing_is_asserted_unmatched(csvs):
    t, p, r = csvs
    p.write_text(HEADER + "p,o,2026-06-01,USD,10000,card\n")
    out = run_pipeline([], [ApiFailure(None, "http_error", "synthetic outage")], p, r, t / "outage.db", t / "outage", run_id="outage")
    report = out["report"]
    assert report["status"] in ("incomplete", "failed")
    assert report["ok"] is False
    assert report["totals_cents"]["unmatched_count"] == 0
    assert report["totals_cents"]["USD"]["unmatched_cents"] == 0
    assert report["mismatches"]["indeterminate_incomplete_source"]["count"] == 1
    finding = out["result"].mismatches[0]
    assert finding.category == "indeterminate_incomplete_source"
    assert finding.payment_id == "p"
    con = duckdb.connect(str(t / "outage.db"))
    try:
        run_columns = [a[1] for a in con.execute("pragma table_info('runs')").fetchall()]
        assert "status" in run_columns
        status, incomplete = con.execute("select status, incomplete_sources from runs where run_id='outage'").fetchone()
        assert status in ("incomplete", "failed")
        assert "orders" in incomplete.split(",")
        assert con.execute("select source, kind, category, detail from run_issues where run_id='outage'").fetchall() == [
            ("orders", "api_failure", "http_error", "synthetic outage")
        ]
        assert con.execute("select sum(unmatched_count) from daily_kpis where run_id='outage'").fetchone()[0] == 0
    finally:
        con.close()


def test_historical_kpis_survive_a_later_run(csvs):
    t, p, r = csvs
    for rid, amt in [("first", 10000), ("second", 20000)]:
        p.write_text(HEADER + f"p,o,2026-06-01,USD,{amt},card\n")
        run_pipeline([order(amt=amt)], [], p, r, t / "history.db", t / "history", run_id=rid)
    con = duckdb.connect(str(t / "history.db"))
    try:
        assert con.execute("select paid_cents from daily_kpis where run_id='first'").fetchone()[0] == 10000
        assert con.execute("select paid_cents from daily_kpis where run_id='second'").fetchone()[0] == 20000
        # Documented behaviour: raw tables hold the latest ingested state only.
        assert con.execute("select amount_cents from payments").fetchall() == [(20000,)]
    finally:
        con.close()


def test_failed_write_commits_nothing(csvs):
    t, p, r = csvs
    p.write_text(HEADER + f"p,o,2026-06-01,USD,{10**30},card\n")
    with pytest.raises(Exception):
        run_pipeline([order()], [], p, r, t / "failed.db", t / "failed", run_id="failed")
    con = duckdb.connect(str(t / "failed.db"))
    try:
        assert con.execute("select count(*) from runs").fetchone()[0] == 0
        assert con.execute("select count(*) from orders").fetchone()[0] == 0
        for table in ("payments", "refunds", "daily_kpis", "mismatches", "run_issues"):
            assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0, table
    finally:
        con.close()
    # No report is written for a run that did not commit.
    assert not (t / "failed" / "mismatch_report.json").exists()


def test_failed_write_leaves_earlier_runs_intact(csvs):
    t, p, r = csvs
    p.write_text(HEADER + "p,o,2026-06-01,USD,10000,card\n")
    run_pipeline([order()], [], p, r, t / "mixed.db", t / "ok", run_id="good")
    p.write_text(HEADER + f"p,o,2026-06-01,USD,{10**30},card\n")
    with pytest.raises(Exception):
        run_pipeline([order(amt=5)], [], p, r, t / "mixed.db", t / "bad", run_id="bad")
    con = duckdb.connect(str(t / "mixed.db"))
    try:
        assert con.execute("select run_id from runs").fetchall() == [("good",)]
        assert con.execute("select amount_cents from orders").fetchall() == [(10000,)]
        assert con.execute("select amount_cents from payments").fetchall() == [(10000,)]
        assert con.execute("select count(*) from daily_kpis where run_id='bad'").fetchone()[0] == 0
    finally:
        con.close()


def test_malformed_api_record_is_categorised_not_a_keyerror():
    client = OrdersApiClient("http://unused.invalid")
    client._get = lambda path: {"orders": [{"order_id": "x"}], "page_size": 1, "total": 1}
    orders, failures = client.fetch_all_orders()  # must not raise
    assert orders == []
    assert len(failures) == 1
    assert failures[0].reason == "malformed_record"
    assert failures[0].order_id == "x"
    assert "customer_id" in failures[0].detail


def test_fractional_api_cents_are_rejected_not_truncated():
    raw = {"order_id": "o", "customer_id": "c", "order_date": "2026-06-01", "currency": "USD", "amount_cents": 100.75, "status": "completed"}
    with pytest.raises(MalformedRecordError) as exc_info:
        _order_from_json(raw)
    assert "amount_cents" in str(exc_info.value)
    assert "100.75" in str(exc_info.value)


def test_failure_on_last_write_rolls_back_all_earlier_writes(tmp_path: Path):
    from revenue_reconciliation import db

    result = reconcile([order()], [pay()], [])
    con = db.connect(tmp_path / "late.db")
    try:
        with pytest.raises(KeyError):
            db.write_run(
                con, "late", "2026-06-01T00:00:00", [order()], [pay()], [], result.daily_kpis, result.mismatches,
                issues=[{"source": "orders"}],  # malformed issue: fails on the final (run_issues) insert
            )
        for table in ("runs", "orders", "payments", "daily_kpis", "mismatches", "run_issues"):
            assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0, table
    finally:
        con.close()


def test_database_from_older_schema_is_refused_clearly(tmp_path: Path):
    from revenue_reconciliation import db

    old = duckdb.connect(str(tmp_path / "old.db"))
    old.execute("CREATE TABLE runs (run_id VARCHAR PRIMARY KEY, run_at TIMESTAMP NOT NULL, source_note VARCHAR)")
    old.close()
    with pytest.raises(db.SchemaVersionError, match="older schema"):
        db.connect(tmp_path / "old.db")
