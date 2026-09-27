"""HTTP client for the mock orders API, with a bounded timeout and retry.

Every failure is turned into an `ApiFailure` for the caller to record as a
categorised ingestion failure; nothing raises out of `fetch_all_orders` or
`fetch_order`. Categories:

  timeout / http_error / connection_error - transport failures.
  malformed_record   - one order record failed validation (missing field,
                       wrong type, non-integer cents...). That record is
                       skipped and reported; the rest of the page is kept.
                       Same skip-and-report convention as the CSV loaders.
  malformed_response - a page body is unusable (not an object, missing or
                       invalid pagination fields, empty page before the
                       advertised total, page limit exceeded). The fetch
                       stops there, since the remaining page count is
                       unknown.

A run with any ApiFailure has an incomplete orders source; the pipeline
marks it so.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from datetime import date

from .models import ApiFailure, MalformedRecordError, Order

ORDER_FIELDS = ("order_id", "customer_id", "order_date", "currency", "amount_cents", "status")
ORDER_STATUSES = ("completed", "cancelled")
DEFAULT_MAX_PAGES = 10_000


class OrdersApiClient:
    def __init__(
        self, base_url: str, *, timeout: float = 1.0, retries: int = 1, max_pages: int = DEFAULT_MAX_PAGES
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.max_pages = max_pages

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
        """Paginate through GET /orders.

        A transport failure or unusable page aborts the fetch (there is no
        way to know how many pages remain) and is reported as an
        ApiFailure rather than raised. A single malformed record is skipped
        and reported; the fetch continues. Pagination is bounded both by
        the server's advertised total and by `max_pages`, so a missing or
        nonsensical page count can never loop forever."""
        orders: list[Order] = []
        failures: list[ApiFailure] = []
        page = 1
        while True:
            if page > self.max_pages:
                failures.append(
                    ApiFailure(None, "malformed_response", f"page limit {self.max_pages} exceeded before total reached")
                )
                return orders, failures
            try:
                body = self._get(f"/orders?page={page}")
            except Exception as exc:  # noqa: BLE001 - classify below
                failures.append(ApiFailure(None, _classify(exc), str(exc)))
                return orders, failures
            try:
                records, page_size, total = _parse_page(body)
            except MalformedRecordError as exc:
                failures.append(ApiFailure(None, "malformed_response", f"page {page}: {exc.reason}"))
                return orders, failures

            for raw in records:
                try:
                    orders.append(_order_from_json(raw))
                except MalformedRecordError as exc:
                    failures.append(ApiFailure(_safe_id(raw), "malformed_record", f"page {page}: {exc.reason}"))

            if page * page_size >= total:
                return orders, failures
            if not records:
                failures.append(
                    ApiFailure(None, "malformed_response", f"page {page} is empty but total={total} not yet reached")
                )
                return orders, failures
            page += 1

    def fetch_order(self, order_id: str) -> Order | ApiFailure:
        try:
            body = self._get(f"/orders/{order_id}")
        except Exception as exc:  # noqa: BLE001 - classify below
            return ApiFailure(order_id, _classify(exc), str(exc))
        try:
            return _order_from_json(body)
        except MalformedRecordError as exc:
            return ApiFailure(order_id, "malformed_record", exc.reason)


def _classify(exc: Exception) -> str:
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_error"
    return "connection_error"


def _is_int(value: object) -> bool:
    # bool is a subclass of int in Python; True is not an amount.
    return isinstance(value, int) and not isinstance(value, bool)


def _safe_id(raw: object) -> str | None:
    if isinstance(raw, dict) and isinstance(raw.get("order_id"), str):
        return raw["order_id"]
    return None


def _parse_page(body: object) -> tuple[list, int, int]:
    def bad(reason: str) -> MalformedRecordError:
        return MalformedRecordError("orders_api", None, "malformed_response", reason)

    if not isinstance(body, dict):
        raise bad(f"page body is {type(body).__name__}, expected an object")
    records = body.get("orders")
    page_size = body.get("page_size")
    total = body.get("total")
    if not isinstance(records, list):
        raise bad("'orders' missing or not a list")
    if not _is_int(page_size) or page_size <= 0:
        raise bad(f"'page_size' missing or not a positive integer: {page_size!r}")
    if not _is_int(total) or total < 0:
        raise bad(f"'total' missing or not a non-negative integer: {total!r}")
    return records, page_size, total


def _order_from_json(raw: object) -> Order:
    """Validate one order record and build an Order.

    Raises MalformedRecordError (a ValueError) - never a raw KeyError or
    TypeError - on a missing field, wrong type, unparseable date, unknown
    status, or non-integer `amount_cents` (e.g. 100.75 is rejected, not
    truncated to 100: cents must be exact)."""

    def bad(reason: str) -> MalformedRecordError:
        return MalformedRecordError("orders_api", None, "malformed_record", reason)

    if not isinstance(raw, dict):
        raise bad(f"record is {type(raw).__name__}, expected an object")
    missing = [f for f in ORDER_FIELDS if f not in raw]
    if missing:
        raise bad(f"missing field(s) {missing} in order {raw.get('order_id')!r}")
    for f in ("order_id", "customer_id", "order_date", "currency", "status"):
        if not isinstance(raw[f], str) or not raw[f].strip():
            raise bad(f"{f} must be a non-empty string, got {raw[f]!r}")
    amount = raw["amount_cents"]
    if not _is_int(amount):
        raise bad(f"amount_cents must be an integer number of cents, got {amount!r}")
    if raw["status"] not in ORDER_STATUSES:
        raise bad(f"status must be one of {list(ORDER_STATUSES)}, got {raw['status']!r}")
    try:
        order_date = date.fromisoformat(raw["order_date"])
    except ValueError:
        raise bad(f"order_date is not an ISO date: {raw['order_date']!r}") from None
    return Order(
        order_id=raw["order_id"],
        customer_id=raw["customer_id"],
        order_date=order_date,
        currency=raw["currency"],
        amount_cents=amount,
        status=raw["status"],
    )
