"""HTTP client for the mock orders API, with a bounded timeout and retry.

Every transport failure (HTTP error status, timeout, connection refused) is
turned into an `ApiFailure` for the caller to record as a categorised
ingestion failure. Nothing here raises out of `fetch_all_orders`; a broken
API degrades the reconciliation run instead of crashing it.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from datetime import date

from .models import ApiFailure, Order


class OrdersApiClient:
    def __init__(self, base_url: str, *, timeout: float = 1.0, retries: int = 1) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        last_exc: Exception = RuntimeError("unreachable")
        attempts = self.retries + 1
        for _ in range(attempts):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                # Deterministic server-side error: retrying won't help.
                raise exc
            except (urllib.error.URLError, socket.timeout, OSError) as exc:
                last_exc = exc
                continue
        raise last_exc

    def fetch_all_orders(self) -> tuple[list[Order], list[ApiFailure]]:
        """Paginate through GET /orders. A failure on any page aborts the
        fetch (there is no way to know how many pages remain) and is
        reported as a single ApiFailure rather than raised."""
        orders: list[Order] = []
        page = 1
        while True:
            try:
                body = self._get(f"/orders?page={page}")
            except Exception as exc:  # noqa: BLE001 - classify below
                return orders, [ApiFailure(None, _classify(exc), str(exc))]
            for raw in body["orders"]:
                orders.append(_order_from_json(raw))
            if page * body["page_size"] >= body["total"]:
                return orders, []
            page += 1

    def fetch_order(self, order_id: str) -> Order | ApiFailure:
        try:
            body = self._get(f"/orders/{order_id}")
        except Exception as exc:  # noqa: BLE001 - classify below
            return ApiFailure(order_id, _classify(exc), str(exc))
        return _order_from_json(body)


def _classify(exc: Exception) -> str:
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    return "connection_error"


def _order_from_json(raw: dict) -> Order:
    return Order(
        order_id=raw["order_id"],
        customer_id=raw["customer_id"],
        order_date=date.fromisoformat(raw["order_date"]),
        currency=raw["currency"],
        amount_cents=int(raw["amount_cents"]),
        status=raw["status"],
    )
