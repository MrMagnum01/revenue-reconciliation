"""Tiny in-process mock orders REST API, bound to 127.0.0.1 only.

Stands in for a Shopify/Stripe-style "list orders" endpoint. Serving the
synthetic order corpus over real HTTP (rather than reading it off disk)
means the reconciliation pipeline exercises a genuine client/server path:
pagination, HTTP error codes, and timeouts, which is what this demo's
failure-handling tests target.

This is not, and does not claim to be, an integration with any real
payments or e-commerce platform.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from urllib.parse import parse_qs, urlparse

from .models import Order


def _order_to_json(o: Order) -> dict:
    d = asdict(o)
    d["order_date"] = o.order_date.isoformat()
    return d


class OrdersApiServer:
    """Serves ``GET /orders?page=N`` (paginated list) and ``GET /orders/<id>``.

    Failure injection, for testing the client's error handling:
      - `fail_ids`: GET /orders/<id> for these ids returns HTTP 500.
      - `timeout_ids`: GET /orders/<id> for these ids never responds.
      - `fail_bulk`: GET /orders (any page) returns HTTP 500.
      - `timeout_bulk`: GET /orders (any page) never responds.
    """

    def __init__(
        self,
        orders: Iterable[Order],
        *,
        fail_ids: frozenset[str] = frozenset(),
        timeout_ids: frozenset[str] = frozenset(),
        fail_bulk: bool = False,
        timeout_bulk: bool = False,
        page_size: int = 25,
        hang_seconds: float = 5.0,
    ) -> None:
        self._orders = list(orders)
        self._by_id = {o.order_id: o for o in self._orders}
        self._fail_ids = fail_ids
        self._timeout_ids = timeout_ids
        self._fail_bulk = fail_bulk
        self._timeout_bulk = timeout_bulk
        self._page_size = page_size
        self._hang_seconds = hang_seconds
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("server not started")
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self) -> "OrdersApiServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> "OrdersApiServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:  # silence access log
                pass

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]

                if parts == ["health"]:
                    self._send_json(200, {"status": "ok"})
                    return

                if parts == ["orders"]:
                    if server._timeout_bulk:
                        time.sleep(server._hang_seconds)
                        return
                    if server._fail_bulk:
                        self._send_json(500, {"error": "internal_error"})
                        return
                    qs = parse_qs(parsed.query)
                    page = int(qs.get("page", ["1"])[0])
                    size = server._page_size
                    start = (page - 1) * size
                    chunk = server._orders[start : start + size]
                    self._send_json(
                        200,
                        {
                            "page": page,
                            "page_size": size,
                            "total": len(server._orders),
                            "orders": [_order_to_json(o) for o in chunk],
                        },
                    )
                    return

                if len(parts) == 2 and parts[0] == "orders":
                    order_id = parts[1]
                    if order_id in server._timeout_ids:
                        time.sleep(server._hang_seconds)
                        return
                    if order_id in server._fail_ids:
                        self._send_json(500, {"error": "internal_error", "order_id": order_id})
                        return
                    order = server._by_id.get(order_id)
                    if order is None:
                        self._send_json(404, {"error": "not_found", "order_id": order_id})
                        return
                    self._send_json(200, _order_to_json(order))
                    return

                self._send_json(404, {"error": "not_found"})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
