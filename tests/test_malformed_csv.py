"""Astra checklist item 2 (CSV half): malformed rows are skipped and
reported as ParseErrors, never a crash."""
from __future__ import annotations

from pathlib import Path

from revenue_reconciliation.loaders import load_payments, load_refunds


def test_malformed_payments_rows_are_skipped_and_reported(tmp_path: Path):
    csv_path = tmp_path / "payments.csv"
    csv_path.write_text(
        "payment_id,order_id,payment_date,currency,amount_cents,method\n"
        "PAY-00001,ORD-00001,2026-06-01,USD,1000,card\n"       # good
        "PAY-00002,ORD-00002,2026-06-02,USD,not-a-number,card\n"  # bad amount
        "PAY-00003,ORD-00003,not-a-date,USD,2000,card\n"       # bad date
        "PAY-00004,ORD-00004,2026-06-03,USD,3000,wallet\n"     # good
    )
    payments, errors = load_payments(csv_path)
    assert [p.payment_id for p in payments] == ["PAY-00001", "PAY-00004"]
    assert len(errors) == 2
    assert errors[0].line_number == 3
    assert errors[1].line_number == 4


def test_payments_csv_missing_column_reported_not_raised(tmp_path: Path):
    csv_path = tmp_path / "payments.csv"
    csv_path.write_text("payment_id,order_id,payment_date,currency,method\nPAY-1,ORD-1,2026-06-01,USD,card\n")
    payments, errors = load_payments(csv_path)
    assert payments == []
    assert len(errors) == 1
    assert "missing columns" in errors[0].reason


def test_malformed_refunds_rows_are_skipped_and_reported(tmp_path: Path):
    csv_path = tmp_path / "refunds.csv"
    csv_path.write_text(
        "refund_id,order_id,refund_date,currency,amount_cents,reason\n"
        "REF-00001,ORD-00001,2026-06-05,USD,500,customer_return\n"
        "REF-00002,ORD-00002,2026-06-06,USD,,customer_return\n"  # empty amount
        "REF-00003,ORD-00003,2026-06-07,USD,12.50,customer_return\n"  # float, not int cents
    )
    refunds, errors = load_refunds(csv_path)
    assert [r.refund_id for r in refunds] == ["REF-00001"]
    assert len(errors) == 2


def test_empty_csv_body_yields_no_rows_no_crash(tmp_path: Path):
    csv_path = tmp_path / "payments.csv"
    csv_path.write_text("payment_id,order_id,payment_date,currency,amount_cents,method\n")
    payments, errors = load_payments(csv_path)
    assert payments == []
    assert errors == []
