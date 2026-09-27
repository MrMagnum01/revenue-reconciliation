"""Shared dataclasses for orders, payments, refunds and reconciliation output.

All money amounts are stored as **integer cents** (`*_cents` fields), never
floats. Summing floating-point dollar amounts drifts by fractions of a cent
across a few hundred rows, which would make the "totals match exactly"
checks in this project's test suite flaky. Integer cents means every sum is
exact; dollars-and-cents strings are only ever formatted at the CSV/report
edge (see `loaders.py`).

Every amount is also tagged with its `currency`. No FX conversion happens
anywhere in this project, so amounts in different currencies are never
added together: every monetary aggregate is partitioned by currency.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# The only currencies this demo's synthetic corpus and mock API ever use.
# Ingestion (CSV loaders and the API client) rejects anything else, so a
# malformed or adversarial currency string can never reach the report/DB
# result namespace (see reconcile.ReconciliationResult.totals).
SUPPORTED_CURRENCIES = frozenset({"USD", "EUR", "GBP"})


class MalformedRecordError(ValueError):
    """A single input record (CSV row or API record) is structurally or
    semantically invalid: wrong field count, missing field, non-integer
    cents, unparseable date, etc.

    Subclasses ValueError so existing `except ValueError` callers still
    catch it. The message is always built from plain strings, so
    constructing it can never itself raise."""

    def __init__(self, source: str, line_number: int | None, category: str, reason: str) -> None:
        self.source = source
        self.line_number = line_number
        self.category = category
        self.reason = reason
        where = f"{source}:{line_number}" if line_number is not None else source
        super().__init__(f"{where}: {category}: {reason}")


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
    # "missing_columns" | "wrong_field_count" | "invalid_value"
    category: str = "invalid_value"


@dataclass(frozen=True)
class ApiFailure:
    order_id: str | None
    # transport: "timeout" | "http_error" | "connection_error"
    # data:      "malformed_record" (one record rejected, others kept)
    #            "malformed_response" (page body unusable; fetch aborted)
    reason: str
    detail: str


@dataclass(frozen=True)
class IdConflict:
    """Two or more input rows share one source id but differ in content.

    The pipeline cannot tell which (if any) is correct, so every row with
    that id is rejected - none of them reaches totals or the database."""

    source: str  # "orders" | "payments" | "refunds"
    record_id: str
    row_count: int
    detail: str


@dataclass(frozen=True)
class Mismatch:
    # missing_payment | overpayment | orphan_refund | currency_mismatch |
    # duplicate | indeterminate_incomplete_source
    category: str
    day: date
    order_id: str | None
    payment_id: str | None
    refund_id: str | None
    currency: str
    amount_cents: int
    details: str


@dataclass(frozen=True)
class DailyKPI:
    """KPIs for one (day, currency). Amounts in different currencies are
    kept in separate rows and never summed together."""

    day: date
    currency: str
    gross_cents: int
    net_cents: int
    refunds_cents: int
    paid_cents: int
    ordered_cents: int
    unmatched_count: int
    unmatched_cents: int
