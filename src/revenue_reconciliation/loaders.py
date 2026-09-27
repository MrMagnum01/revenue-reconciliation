"""CSV loaders for payments and refunds.

A malformed row (missing column, non-integer amount, unparseable date)
never crashes the run: it is skipped and recorded as a `ParseError` so the
mismatch report can say exactly what was dropped and why.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .models import ParseError, Payment, Refund

PAYMENTS_FIELDS = ["payment_id", "order_id", "payment_date", "currency", "amount_cents", "method"]
REFUNDS_FIELDS = ["refund_id", "order_id", "refund_date", "currency", "amount_cents", "reason"]


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def _parse_cents(raw: str) -> int:
    # Reject floats-as-strings too ("12.5") - amounts are integer cents only.
    return int(raw.strip())


def load_payments(path: Path) -> tuple[list[Payment], list[ParseError]]:
    payments: list[Payment] = []
    errors: list[ParseError] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in PAYMENTS_FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            errors.append(ParseError(str(path), 1, ",".join(reader.fieldnames or []), f"missing columns: {missing}"))
            return payments, errors
        for i, row in enumerate(reader, start=2):
            try:
                payments.append(
                    Payment(
                        payment_id=row["payment_id"].strip(),
                        order_id=row["order_id"].strip(),
                        payment_date=_parse_date(row["payment_date"]),
                        currency=row["currency"].strip(),
                        amount_cents=_parse_cents(row["amount_cents"]),
                        method=row["method"].strip(),
                    )
                )
            except (ValueError, AttributeError, KeyError, TypeError) as exc:
                errors.append(ParseError(str(path), i, ",".join(row.values()), str(exc)))
    return payments, errors


def load_refunds(path: Path) -> tuple[list[Refund], list[ParseError]]:
    refunds: list[Refund] = []
    errors: list[ParseError] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in REFUNDS_FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            errors.append(ParseError(str(path), 1, ",".join(reader.fieldnames or []), f"missing columns: {missing}"))
            return refunds, errors
        for i, row in enumerate(reader, start=2):
            try:
                refunds.append(
                    Refund(
                        refund_id=row["refund_id"].strip(),
                        order_id=row["order_id"].strip(),
                        refund_date=_parse_date(row["refund_date"]),
                        currency=row["currency"].strip(),
                        amount_cents=_parse_cents(row["amount_cents"]),
                        reason=row["reason"].strip(),
                    )
                )
            except (ValueError, AttributeError, KeyError, TypeError) as exc:
                errors.append(ParseError(str(path), i, ",".join(row.values()), str(exc)))
    return refunds, errors
