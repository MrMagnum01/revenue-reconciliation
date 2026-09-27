"""CSV loaders for payments and refunds.

A malformed row (wrong number of fields, non-integer amount, unparseable
date, empty required value) never crashes the run: by default it is
skipped and recorded as a categorised `ParseError` so the mismatch report
can say exactly what was dropped and why. Pass `strict=True` to raise a
`MalformedRecordError` on the first malformed row instead.

Error messages and `raw_row` are built only from plain strings, so
describing a malformed row can never itself raise.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Callable, TypeVar

from .models import SUPPORTED_CURRENCIES, MalformedRecordError, ParseError, Payment, Refund

PAYMENTS_FIELDS = ["payment_id", "order_id", "payment_date", "currency", "amount_cents", "method"]
REFUNDS_FIELDS = ["refund_id", "order_id", "refund_date", "currency", "amount_cents", "reason"]

T = TypeVar("T")


def _parse_date(raw: str) -> date:
    return date.fromisoformat(raw.strip())


def _parse_cents(raw: str) -> int:
    # Integer cents only: "12.5", "12.50", "1e3" and "" are all rejected.
    text = raw.strip()
    if not text.lstrip("-").isdigit():
        raise ValueError(f"amount_cents must be an integer number of cents, got {raw!r}")
    return int(text)


def _text(raw: str, field: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError(f"{field} is empty")
    return value


def _parse_currency(raw: str) -> str:
    value = _text(raw, "currency")
    if value not in SUPPORTED_CURRENCIES:
        raise ValueError(f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}, got {value!r}")
    return value


def _load(
    path: Path,
    required: list[str],
    build: Callable[[dict[str, str]], T],
    strict: bool,
) -> tuple[list[T], list[ParseError]]:
    records: list[T] = []
    errors: list[ParseError] = []
    source = str(path)

    def fail(line: int, raw: list[str], category: str, reason: str) -> None:
        if strict:
            raise MalformedRecordError(source, line, category, reason)
        errors.append(ParseError(source, line, ",".join(raw), reason, category))

    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            # A zero-byte / headerless file is not the same thing as a valid
            # header-only (empty-table) file: there is no way to tell what
            # columns it was supposed to have, so it must not be treated as
            # a complete, empty source.
            fail(1, [], "missing_columns", "file is empty: no header row found")
            return records, errors
        header = [h.strip() for h in header]
        duplicate_columns = sorted({h for h in header if header.count(h) > 1})
        if duplicate_columns:
            # dict(zip(header, raw)) below would silently let a later
            # duplicate column's value overwrite an earlier one's (e.g. a
            # second amount_cents column replacing the real amount), so
            # duplicate column names are refused outright.
            fail(1, header, "duplicate_columns", f"duplicate column name(s) in header: {duplicate_columns}")
            return records, errors
        missing = [f for f in required if f not in header]
        if missing:
            fail(1, header, "missing_columns", f"missing columns: {missing}")
            return records, errors
        for raw in reader:
            line = reader.line_num
            if not raw:
                continue  # blank line
            if len(raw) != len(header):
                fail(
                    line,
                    raw,
                    "wrong_field_count",
                    f"expected {len(header)} fields ({','.join(header)}), got {len(raw)}",
                )
                continue
            row = dict(zip(header, raw))
            try:
                records.append(build(row))
            except ValueError as exc:
                fail(line, raw, "invalid_value", str(exc))
    return records, errors


def _build_payment(row: dict[str, str]) -> Payment:
    return Payment(
        payment_id=_text(row["payment_id"], "payment_id"),
        order_id=_text(row["order_id"], "order_id"),
        payment_date=_parse_date(row["payment_date"]),
        currency=_parse_currency(row["currency"]),
        amount_cents=_parse_cents(row["amount_cents"]),
        method=_text(row["method"], "method"),
    )


def _build_refund(row: dict[str, str]) -> Refund:
    return Refund(
        refund_id=_text(row["refund_id"], "refund_id"),
        order_id=_text(row["order_id"], "order_id"),
        refund_date=_parse_date(row["refund_date"]),
        currency=_parse_currency(row["currency"]),
        amount_cents=_parse_cents(row["amount_cents"]),
        reason=_text(row["reason"], "reason"),
    )


def load_payments(path: Path, *, strict: bool = False) -> tuple[list[Payment], list[ParseError]]:
    return _load(Path(path), PAYMENTS_FIELDS, _build_payment, strict)


def load_refunds(path: Path, *, strict: bool = False) -> tuple[list[Refund], list[ParseError]]:
    return _load(Path(path), REFUNDS_FIELDS, _build_refund, strict)
