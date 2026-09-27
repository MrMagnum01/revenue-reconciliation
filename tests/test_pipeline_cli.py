"""End-to-end pipeline test plus a subprocess smoke test of the CLI itself."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb

from revenue_reconciliation.generator import write_corpus
from revenue_reconciliation.pipeline import fetch_orders_from_fixture, run_pipeline

SRC = Path(__file__).resolve().parents[1] / "src"


def test_full_pipeline_writes_duckdb_and_report(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"
    write_corpus(raw, seed=42)
    orders, failures = fetch_orders_from_fixture(raw / "orders.json")
    assert failures == []

    outcome = run_pipeline(orders, failures, raw / "payments.csv", raw / "refunds.csv", out / "r.duckdb", out)
    report = outcome["report"]
    assert report["status"] == "complete"
    assert report["ok"] is True
    assert report["mismatches"]["missing_payment"]["count"] == 6

    con = duckdb.connect(str(out / "r.duckdb"))
    try:
        assert con.execute("select count(*) from orders").fetchone()[0] == 130
        assert con.execute("select count(*) from daily_kpis").fetchone()[0] > 0
        assert con.execute("select count(*) from mismatches").fetchone()[0] == sum(
            v["count"] for v in report["mismatches"].values()
        )
        assert con.execute("select status, incomplete_sources from runs").fetchall() == [("complete", "")]
        # DB per-currency KPI sums equal the report's per-currency totals.
        db_paid = dict(con.execute("select currency, sum(paid_cents) from daily_kpis group by currency").fetchall())
        assert db_paid == {cur: t["paid_cents"] for cur, t in report["totals_cents"].items() if isinstance(t, dict)}
        # ...and equal the raw payments table, per currency.
        raw_paid = dict(con.execute("select currency, sum(amount_cents) from payments group by currency").fetchall())
        assert raw_paid == db_paid
    finally:
        con.close()

    assert (out / "mismatch_report.json").exists()
    assert (out / "mismatches.csv").exists()


def test_reconcile_records_api_outage_without_crashing(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"
    write_corpus(raw, seed=42)
    orders, failures = fetch_orders_from_fixture(raw / "orders.json", fail_bulk=True)
    assert orders == []
    assert len(failures) == 1

    outcome = run_pipeline(orders, failures, raw / "payments.csv", raw / "refunds.csv", out / "r.duckdb", out)
    report = outcome["report"]
    assert report["api_failures"][0]["reason"] == "http_error"
    # No orders ingested: the run writes output instead of crashing, but it
    # must NOT look like a successful run.
    assert report["counts"]["orders_ingested"] == 0
    assert report["status"] == "incomplete"
    assert report["ok"] is False
    assert report["sources"]["orders"] == "failed"
    assert report["incomplete_sources"] == ["orders"]
    # With the orders source down, "no such order" is not evidence: nothing
    # is asserted as unmatched or orphaned.
    assert report["totals_cents"]["unmatched_count"] == 0
    assert "orphan_refund" not in report["mismatches"]
    assert report["totals_cents"]["indeterminate_count"] == outcome["report"]["counts"]["payments_loaded"] + outcome["report"]["counts"]["refunds_loaded"]
    assert (out / "mismatch_report.json").exists()

    con = duckdb.connect(str(out / "r.duckdb"))
    try:
        assert con.execute("select status, incomplete_sources from runs").fetchall() == [("incomplete", "orders")]
        assert con.execute("select source, kind, category from run_issues").fetchall() == [
            ("orders", "api_failure", "http_error")
        ]
        assert con.execute("select coalesce(sum(unmatched_count), 0) from daily_kpis").fetchone()[0] == 0
    finally:
        con.close()


def _run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    import os
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    return subprocess.run(
        [sys.executable, "-m", "revenue_reconciliation", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_generate_and_reconcile_subprocess(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"

    gen = _run_cli("generate", "--out", str(raw), "--seed", "42", cwd=tmp_path)
    assert gen.returncode == 0, gen.stderr
    assert "Generated synthetic corpus" in gen.stdout

    rec = _run_cli(
        "reconcile",
        "--orders-fixture", str(raw / "orders.json"),
        "--payments", str(raw / "payments.csv"),
        "--refunds", str(raw / "refunds.csv"),
        "--db", str(out / "r.duckdb"),
        "--report-dir", str(out),
        cwd=tmp_path,
    )
    assert rec.returncode == 0, rec.stderr
    assert "status=complete" in rec.stdout
    assert "missing_payment: 6" in rec.stdout
    assert (out / "mismatch_report.json").exists()


def test_cli_reconcile_exits_nonzero_when_orders_source_is_down(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"
    assert _run_cli("generate", "--out", str(raw), "--seed", "42", cwd=tmp_path).returncode == 0
    rec = _run_cli(
        "reconcile",
        "--orders-api-url", "http://127.0.0.1:1",  # closed port: connection refused
        "--payments", str(raw / "payments.csv"),
        "--refunds", str(raw / "refunds.csv"),
        "--db", str(out / "r.duckdb"),
        "--report-dir", str(out),
        cwd=tmp_path,
    )
    assert rec.returncode == 2, rec.stdout + rec.stderr
    assert "status=incomplete" in rec.stdout
    assert "INCOMPLETE SOURCES: orders=failed" in rec.stdout
    report = json.loads((out / "mismatch_report.json").read_text())
    assert report["ok"] is False


def test_cli_run_one_shot(tmp_path: Path):
    raw = tmp_path / "raw"
    out = tmp_path / "out"
    result = _run_cli("run", "--raw-dir", str(raw), "--out", str(out), "--seed", "7", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert (out / "reconciliation.duckdb").exists()
    assert (out / "mismatch_report.json").exists()
    report = json.loads((out / "mismatch_report.json").read_text())
    assert report["counts"]["orders_ingested"] == 130  # 120 completed + 10 cancelled
