"""Small handcrafted cases, one per mismatch category, isolated from the
generator - checks the categorisation logic on its own."""
from __future__ import annotations

from datetime import date

from revenue_reconciliation.models import Order, Payment, Refund
from revenue_reconciliation.reconcile import reconcile

D = date(2026, 6, 1)


def test_missing_payment():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    result = reconcile(orders, [], [])
    assert [m.category for m in result.mismatches] == ["missing_payment"]
    assert result.mismatches[0].amount_cents == 1000


def test_cancelled_order_without_payment_is_not_a_mismatch():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "cancelled")]
    result = reconcile(orders, [], [])
    assert result.mismatches == []


def test_overpayment():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    payments = [Payment("PAY-1", "ORD-1", D, "USD", 1500, "card")]
    result = reconcile(orders, payments, [])
    assert [m.category for m in result.mismatches] == ["overpayment"]
    assert result.mismatches[0].amount_cents == 500


def test_exact_payment_is_not_a_mismatch():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    payments = [Payment("PAY-1", "ORD-1", D, "USD", 1000, "card")]
    result = reconcile(orders, payments, [])
    assert result.mismatches == []


def test_currency_mismatch():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    payments = [Payment("PAY-1", "ORD-1", D, "EUR", 1000, "card")]
    result = reconcile(orders, payments, [])
    assert [m.category for m in result.mismatches] == ["currency_mismatch"]


def test_currency_mismatch_with_larger_amount_is_not_an_overpayment():
    # EUR 20000 against a USD 10000 order: different units, not comparable.
    orders = [Order("ORD-1", "CUST-1", D, "USD", 10000, "completed")]
    payments = [Payment("PAY-1", "ORD-1", D, "EUR", 20000, "card")]
    result = reconcile(orders, payments, [])
    assert [m.category for m in result.mismatches] == ["currency_mismatch"]


def test_duplicate_payment():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    payments = [
        Payment("PAY-1", "ORD-1", D, "USD", 1000, "card"),
        Payment("PAY-2", "ORD-1", D, "USD", 1000, "card"),
    ]
    result = reconcile(orders, payments, [])
    assert [m.category for m in result.mismatches] == ["duplicate"]
    assert result.mismatches[0].payment_id == "PAY-2"


def test_orphan_refund():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    payments = [Payment("PAY-1", "ORD-1", D, "USD", 1000, "card")]
    refunds = [Refund("REF-1", "ORD-GHOST", D, "USD", 200, "customer_return")]
    result = reconcile(orders, payments, refunds)
    assert [m.category for m in result.mismatches] == ["orphan_refund"]


def test_unmatched_payment_is_a_kpi_not_a_mismatch():
    payments = [Payment("PAY-1", "ORD-GHOST", D, "USD", 500, "card")]
    result = reconcile([], payments, [])
    assert result.mismatches == []
    totals = result.totals()
    assert totals["unmatched_count"] == 1
    assert totals["USD"]["unmatched_cents"] == 500
    assert totals["USD"]["gross_cents"] == 500


def test_legitimate_refund_reduces_net_not_flagged():
    orders = [Order("ORD-1", "CUST-1", D, "USD", 1000, "completed")]
    payments = [Payment("PAY-1", "ORD-1", D, "USD", 1000, "card")]
    refunds = [Refund("REF-1", "ORD-1", D, "USD", 300, "customer_return")]
    result = reconcile(orders, payments, refunds)
    assert result.mismatches == []
    totals = result.totals()
    assert totals["USD"]["gross_cents"] == 1000
    assert totals["USD"]["refunds_cents"] == 300
    assert totals["USD"]["net_cents"] == 700
