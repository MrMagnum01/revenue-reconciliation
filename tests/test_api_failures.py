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
