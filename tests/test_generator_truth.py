"""Astra checklist item 1: known-total reconciliation.

Every mismatch the generator plants must be found by the independent
reconcile() logic, with exactly the same counts and amounts the generator
recorded as ground truth.
"""
from __future__ import annotations

from revenue_reconciliation.generator import generate_corpus
from revenue_reconciliation.reconcile import reconcile


def test_known_totals_match_exactly():
    orders, payments, refunds, truth = generate_corpus(seed=42)
    result = reconcile(orders, payments, refunds)
    got = result.totals()

    expected = truth["totals_cents"]
    got_currencies = {k for k, v in got.items() if isinstance(v, dict)}
    assert got_currencies == set(expected) == {"USD", "EUR", "GBP"}
    for cur, exp in expected.items():
        assert got[cur]["gross_cents"] == exp["gross"], cur
        assert got[cur]["net_cents"] == exp["net"], cur
        assert got[cur]["refunds_cents"] == exp["refunds"], cur
        assert got[cur]["paid_cents"] == exp["paid"], cur
        assert got[cur]["ordered_cents"] == exp["ordered"], cur
        assert got[cur]["unmatched_cents"] == exp["unmatched"], cur
    assert got["unmatched_count"] == truth["unmatched_count"]
    assert got["indeterminate_count"] == 0


def test_mismatch_categories_match_exactly():
    orders, payments, refunds, truth = generate_corpus(seed=42)
    result = reconcile(orders, payments, refunds)

    assert result.mismatch_totals() == truth["mismatches"]


def test_generator_is_deterministic():
    a = generate_corpus(seed=42)
    b = generate_corpus(seed=42)
    assert a[3] == b[3]  # truth dict identical
    assert [o.order_id for o in a[0]] == [o.order_id for o in b[0]]


def test_different_seed_changes_corpus():
    _, _, _, truth_a = generate_corpus(seed=42)
    _, _, _, truth_b = generate_corpus(seed=7)
    assert truth_a["totals_cents"] != truth_b["totals_cents"]


def test_daily_kpis_sum_to_totals():
    orders, payments, refunds, truth = generate_corpus(seed=42)
    result = reconcile(orders, payments, refunds)
    totals = result.totals()
    for cur in ("USD", "EUR", "GBP"):
        rows = [k for k in result.daily_kpis if k.currency == cur]
        assert rows, cur
        assert sum(k.gross_cents for k in rows) == totals[cur]["gross_cents"]
        assert sum(k.refunds_cents for k in rows) == totals[cur]["refunds_cents"]
        assert sum(k.net_cents for k in rows) == totals[cur]["net_cents"]
    # one KPI row per (day, currency), never a combined-currency row
    keys = [(k.day, k.currency) for k in result.daily_kpis]
    assert len(keys) == len(set(keys))
