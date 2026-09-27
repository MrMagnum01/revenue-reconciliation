"""Regression tests derived from the independent re-review's follow-up probe
script (vault: 40-sessions/2026-09-27-astra-revenue-reconciliation-rereview-
probes.py). Each test is one probe scenario, asserting the CORRECTED
behaviour; only the temp paths were adapted to pytest's `tmp_path`.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from revenue_reconciliation.api_client import OrdersApiClient, _order_from_json
from revenue_reconciliation.loaders import load_payments
from revenue_reconciliation.models import MalformedRecordError, Order
from revenue_reconciliation.pipeline import run_pipeline

D = date(2026, 6, 1)
PAYMENTS_HEADER = "payment_id,order_id,payment_date,currency,amount_cents,method\n"
REFUNDS_HEADER = "refund_id,order_id,refund_date,currency,amount_cents,reason\n"


def _order() -> Order:
    return Order("o", "synthetic", D, "USD", 10000, "completed")


@pytest.fixture
def csvs(tmp_path: Path):
    p = tmp_path / "payments.csv"
    r = tmp_path / "refunds.csv"
    r.write_text(REFUNDS_HEADER)
    return tmp_path, p, r


# --- High: invalid CSV schema can no longer silently replace money or
# fabricate source completeness (loaders.py).


def test_zero_byte_payments_file_is_not_a_complete_empty_source(csvs):
    t, p, r = csvs
    p.write_text("")  # zero-byte file: no header row at all
    payments, errors = load_payments(p)
    assert payments == []
    assert len(errors) == 1
    assert errors[0].category == "missing_columns"

    out = run_pipeline([_order()], [], p, r, t / "empty.db", t / "empty", run_id="empty-source")
    report = out["report"]
    # A headerless file must not be treated as the valid empty-table
    # representation: the payments source is failed, not "ok"/complete.
    assert report["sources"]["payments"] == "failed"
    assert report["status"] != "complete"
    # No fabricated money and no definite missing_payment finding from an
    # incomplete source.
    assert report["totals_cents"]["USD"]["paid_cents"] == 0
    assert "missing_payment" not in report["mismatches"]
    assert report["mismatches"]["indeterminate_incomplete_source"]["count"] == 1


def test_header_only_file_is_still_a_valid_empty_table(csvs):
    # Contrast case: a real header-only file is the explicit empty-table
    # representation and must keep working (test_malformed_csv.py also
    # covers this; re-asserted here next to the zero-byte case).
    t, p, r = csvs
    p.write_text(PAYMENTS_HEADER)
    payments, errors = load_payments(p)
    assert payments == []
    assert errors == []


def test_duplicate_header_column_is_rejected_not_silently_overwritten(csvs):
    t, p, r = csvs
    p.write_text(PAYMENTS_HEADER.strip() + ",amount_cents\np,o,2026-06-01,USD,10000,card,1\n")
    payments, errors = load_payments(p)
    assert payments == []
    assert len(errors) == 1
    assert errors[0].category == "duplicate_columns"
    assert "amount_cents" in errors[0].reason

    out = run_pipeline([_order()], [], p, r, t / "dup.db", t / "duphdr", run_id="dup-header")
    report = out["report"]
    assert report["sources"]["payments"] == "failed"
    # The real amount (10000) must never be replaced by the duplicate
    # column's value (1).
    assert report["totals_cents"]["USD"]["paid_cents"] == 0


# --- Medium: currency can no longer be an arbitrary string that collides
# with the report's reserved keys (loaders.py + api_client.py).


def test_currency_colliding_with_a_reserved_report_key_is_rejected(csvs):
    t, p, r = csvs
    p.write_text(PAYMENTS_HEADER + "p,o,2026-06-01,unmatched_count,10000,card\n")
    payments, errors = load_payments(p)
    assert payments == []
    assert len(errors) == 1
    assert errors[0].category == "invalid_value"
    assert "currency" in errors[0].reason

    out = run_pipeline([_order()], [], p, r, t / "cur.db", t / "curcollision", run_id="currency-collision")
    report = out["report"]
    # The reserved keys must stay counts (ints), never be replaced by a
    # per-currency totals dict.
    assert isinstance(report["totals_cents"]["unmatched_count"], int)
    assert isinstance(report["totals_cents"]["indeterminate_count"], int)
    assert "currency_mismatch" not in report["mismatches"]


def test_order_with_unsupported_currency_is_a_malformed_record():
    raw = {
        "order_id": "o", "customer_id": "c", "order_date": "2026-06-01",
        "currency": "unmatched_count", "amount_cents": 10000, "status": "completed",
    }
    with pytest.raises(MalformedRecordError) as exc_info:
        _order_from_json(raw)
    assert "currency" in str(exc_info.value)


# --- High: truncated API pages can no longer count as a successful full
# fetch (api_client.py).


def test_short_last_page_is_flagged_incomplete_not_a_silent_success():
    good = {
        "order_id": "o", "customer_id": "c", "order_date": "2026-06-01",
        "currency": "USD", "amount_cents": 10000, "status": "completed",
    }
    client = OrdersApiClient("http://unused.invalid")
    client._get = lambda path: {"orders": [good], "page_size": 100, "total": 2}
    orders, failures = client.fetch_all_orders()
    # The one delivered order is still kept...
    assert [o.order_id for o in orders] == ["o"]
    # ...but the fetch is reported incomplete: page*page_size >= total is
    # not, by itself, proof that all advertised records arrived.
    assert len(failures) == 1
    assert failures[0].reason == "malformed_response"
    assert "1" in failures[0].detail and "2" in failures[0].detail


def test_empty_last_page_short_of_total_is_flagged_incomplete():
    client = OrdersApiClient("http://unused.invalid")
    client._get = lambda path: {"orders": [], "page_size": 100, "total": 2}
    orders, failures = client.fetch_all_orders()
    assert orders == []
    assert len(failures) == 1
    assert failures[0].reason == "malformed_response"
