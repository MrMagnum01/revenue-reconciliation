"""Shared dataclasses for orders, payments, refunds and reconciliation output.

All money amounts are stored as **integer cents** (`*_cents` fields), never
floats. Summing floating-point dollar amounts drifts by fractions of a cent
across a few hundred rows, which would make the "totals match exactly"
checks in this project's test suite flaky. Integer cents means every sum is
exact; dollars-and-cents strings are only ever formatted at the CSV/report
edge (see `loaders.py`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    order_date: date
    currency: str
    amount_cents: int
    status: str  # "completed" | "cancelled"


@dataclass(frozen=True)
class Payment:
    payment_id: str
    order_id: str
    payment_date: date
    currency: str
    amount_cents: int
    method: str


@dataclass(frozen=True)
class Refund:
    refund_id: str
    order_id: str
    refund_date: date
    currency: str
    amount_cents: int
    reason: str


@dataclass(frozen=True)
class ParseError:
    source: str  # file path or logical source name
    line_number: int
    raw_row: str
    reason: str


@dataclass(frozen=True)
class ApiFailure:
    order_id: str | None
    reason: str  # "timeout" | "http_error" | "connection_error"
    detail: str


@dataclass(frozen=True)
class Mismatch:
    category: str  # missing_payment | overpayment | orphan_refund | currency_mismatch | duplicate
    day: date
    order_id: str | None
    payment_id: str | None
    refund_id: str | None
    currency: str
    amount_cents: int
    details: str


@dataclass(frozen=True)
class DailyKPI:
    day: date
    gross_cents: int
    net_cents: int
    refunds_cents: int
    paid_cents: int
    ordered_cents: int
    unmatched_count: int
    unmatched_cents: int
