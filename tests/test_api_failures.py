"""Astra checklist item 2 (API half): HTTP errors and timeouts become
categorised failures, never a crash."""
from __future__ import annotations

from datetime import date

from revenue_reconciliation.api_client import OrdersApiClient
from revenue_reconciliation.api_server import OrdersApiServer
from revenue_reconciliation.models import ApiFailure, Order

ORDERS = [
    Order("ORD-00001", "CUST-0001", date(2026, 6, 1), "USD", 1000, "completed"),
    Order("ORD-00002", "CUST-0002", date(2026, 6, 2), "USD", 2000, "completed"),
]


def test_per_order_http_error_is_classified():
    with OrdersApiServer(ORDERS, fail_ids=frozenset({"ORD-00001"})) as server:
        client = OrdersApiClient(server.base_url, timeout=1.0, retries=0)
        result = client.fetch_order("ORD-00001")
        assert isinstance(result, ApiFailure)
        assert result.reason == "http_error"
        assert result.order_id == "ORD-00001"


def test_per_order_timeout_is_classified():
    with OrdersApiServer(ORDERS, timeout_ids=frozenset({"ORD-00002"}), hang_seconds=2.0) as server:
        client = OrdersApiClient(server.base_url, timeout=0.2, retries=0)
        result = client.fetch_order("ORD-00002")
        assert isinstance(result, ApiFailure)
        assert result.reason == "timeout"


def test_per_order_success_path_still_works():
    with OrdersApiServer(ORDERS) as server:
        client = OrdersApiClient(server.base_url, timeout=1.0)
        result = client.fetch_order("ORD-00001")
        assert isinstance(result, Order)
        assert result.order_id == "ORD-00001"


def test_bulk_endpoint_http_error_becomes_single_failure_not_a_crash():
    with OrdersApiServer(ORDERS, fail_bulk=True) as server:
        client = OrdersApiClient(server.base_url, timeout=1.0, retries=0)
        orders, failures = client.fetch_all_orders()
        assert orders == []
        assert len(failures) == 1
        assert failures[0].reason == "http_error"


def test_bulk_endpoint_timeout_becomes_single_failure_not_a_crash():
    with OrdersApiServer(ORDERS, timeout_bulk=True, hang_seconds=2.0) as server:
        client = OrdersApiClient(server.base_url, timeout=0.2, retries=0)
        orders, failures = client.fetch_all_orders()
        assert orders == []
        assert len(failures) == 1
        assert failures[0].reason == "timeout"


def test_connection_error_when_server_never_started():
    # Port 1 is a privileged, essentially-always-closed port on 127.0.0.1.
    client = OrdersApiClient("http://127.0.0.1:1", timeout=0.3, retries=0)
    orders, failures = client.fetch_all_orders()
    assert orders == []
    assert len(failures) == 1
    assert failures[0].reason == "connection_error"


def test_pagination_across_multiple_pages():
    many = [Order(f"ORD-{i:05d}", "CUST-0001", date(2026, 6, 1), "USD", 100 * i, "completed") for i in range(1, 61)]
    with OrdersApiServer(many, page_size=25) as server:
        client = OrdersApiClient(server.base_url, timeout=1.0)
        orders, failures = client.fetch_all_orders()
        assert failures == []
        assert len(orders) == 60
        assert {o.order_id for o in orders} == {o.order_id for o in many}


# --- malformed API data: categorised like malformed CSV rows, never a raw
# KeyError/TypeError, and pagination can never loop forever.

GOOD = {"order_id": "ORD-1", "customer_id": "CUST-1", "order_date": "2026-06-01",
        "currency": "USD", "amount_cents": 1000, "status": "completed"}


def _client_with_pages(pages):
    client = OrdersApiClient("http://unused.invalid", max_pages=50)
    calls = []

    def fake_get(path):
        calls.append(path)
        page = int(path.split("page=")[1])
        return pages(page)

    client._get = fake_get
    return client, calls


def test_malformed_record_is_skipped_and_rest_of_page_kept():
    bad = {**GOOD, "order_id": "ORD-2", "amount_cents": "1000"}  # string, not int cents
    client, _ = _client_with_pages(lambda page: {"orders": [GOOD, bad], "page_size": 2, "total": 2})
    orders, failures = client.fetch_all_orders()
    assert [o.order_id for o in orders] == ["ORD-1"]
    assert [(f.order_id, f.reason) for f in failures] == [("ORD-2", "malformed_record")]


def test_non_dict_record_and_bool_amount_are_malformed_records():
    client, _ = _client_with_pages(
        lambda page: {"orders": ["not-an-object", {**GOOD, "amount_cents": True}], "page_size": 2, "total": 2}
    )
    orders, failures = client.fetch_all_orders()
    assert orders == []
    assert [f.reason for f in failures] == ["malformed_record", "malformed_record"]


def test_missing_page_size_is_malformed_response_not_a_crash():
    client, calls = _client_with_pages(lambda page: {"orders": [GOOD], "total": 5})
    orders, failures = client.fetch_all_orders()
    assert orders == []
    assert len(failures) == 1 and failures[0].reason == "malformed_response"
    assert len(calls) == 1


def test_empty_pages_before_total_stop_the_fetch():
    # Server claims 1000 orders but serves nothing: must not loop forever.
    client, calls = _client_with_pages(lambda page: {"orders": [], "page_size": 25, "total": 1000})
    orders, failures = client.fetch_all_orders()
    assert orders == []
    assert failures[0].reason == "malformed_response"
    assert len(calls) == 1


def test_page_limit_bounds_a_never_ending_listing():
    # Every page is non-empty but the advertised total is never reached.
    client, calls = _client_with_pages(
        lambda page: {"orders": [{**GOOD, "order_id": f"ORD-{page}"}], "page_size": 1, "total": 10**9}
    )
    orders, failures = client.fetch_all_orders()
    assert len(calls) == 50
    assert len(orders) == 50
    assert failures[-1].reason == "malformed_response"
    assert "page limit" in failures[-1].detail


def test_fetch_order_malformed_body_is_a_categorised_failure():
    client = OrdersApiClient("http://unused.invalid")
    client._get = lambda path: {"order_id": "ORD-1"}
    result = client.fetch_order("ORD-1")
    assert isinstance(result, ApiFailure)
    assert result.reason == "malformed_record"
