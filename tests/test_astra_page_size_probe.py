"""Regression test derived from Astra's probe script (vault:
40-sessions/2026-09-27-astra-revenue-page-size-probe.py), asserting the
CORRECTED behaviour: page_size, like total, is bound from page 1, and a
later page advertising a different page_size is a categorised
malformed_response failure rather than a value silently adopted.
"""
from __future__ import annotations

from revenue_reconciliation.api_client import OrdersApiClient


def _record(n: int) -> dict:
    return dict(
        order_id=str(n),
        customer_id="s",
        order_date="2026-06-01",
        currency="USD",
        amount_cents=100,
        status="completed",
    )


def test_page_size_change_between_pages_is_a_categorised_failure():
    client = OrdersApiClient("http://unused.invalid")
    client._get = lambda p: (
        {"orders": [_record(1)], "page_size": 1, "total": 3}
        if p.endswith("=1")
        else {"orders": [_record(2), _record(3)], "page_size": 2, "total": 3}
    )
    orders, failures = client.fetch_all_orders()

    # Only page 1's records are accepted: the fetch stops there, since a
    # page_size change means the remaining page count is no longer known.
    assert [o.order_id for o in orders] == ["1"]

    # The change is reported as one categorised, page_size-specific
    # malformed_response failure - not silently followed, and not merged
    # with the (unrelated, unchanged) total-mismatch check.
    assert len(failures) == 1
    assert failures[0].reason == "malformed_response"
    assert "page_size" in failures[0].detail
    assert "1" in failures[0].detail and "2" in failures[0].detail

    # The total binding and delivered-coverage accounting are unaffected:
    # a page_size change is caught before the fetch can misdeclare success
    # off a stale expected_total (3 records advertised, only 1 delivered).
    assert len(orders) != 3
