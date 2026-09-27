"""Order/payment/refund matching, mismatch categorisation, and daily KPIs.

Categories (mismatch report):
  missing_payment   - a completed order with zero payments recorded
                      (only asserted when the payments source is complete).
  overpayment       - a payment greater than its order's amount, in the
                      SAME currency. Amounts in different currencies are
                      never compared (there is no FX conversion).
  orphan_refund     - a refund whose order_id matches no known order
                      (only asserted when the orders source is complete).
  currency_mismatch - a payment whose currency differs from its order's.
                      This is the only finding emitted for such a payment;
                      no over/under-payment is computed across currencies.
  duplicate         - an order with more than one payment recorded for it
                      (the extra payment(s) beyond the first are flagged).
  indeterminate_incomplete_source
                    - a finding that would rest on the ABSENCE of a record
                      (missing_payment, orphan_refund, unmatched payment)
                      while the source that should contain that record is
                      incomplete (fetch failed, rows rejected). Absence in
                      an incomplete source is not evidence, so these are
                      reported separately instead of as definite findings.

"Unmatched" (a KPI, not a mismatch category) is a payment whose order_id
matches no known order at all - money came in for an order we never heard
of, which is a different failure mode than an order that was never paid.

Money is never summed across currencies: daily KPIs are keyed by
(day, currency) and every total is partitioned by currency.

Input contract: each source id (order_id / payment_id / refund_id) appears
at most once - one row is one event. `dedupe_by_id` enforces that before
reconciliation; `reconcile` raises ValueError if it is violated.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, TypeVar

from .models import DailyKPI, IdConflict, Mismatch, Order, Payment, Refund

T = TypeVar("T")

MONEY_FIELDS = ("gross_cents", "net_cents", "refunds_cents", "paid_cents", "ordered_cents", "unmatched_cents")
INDETERMINATE = "indeterminate_incomplete_source"


def dedupe_by_id(
    records: Iterable[T], id_of: Callable[[T], str], source: str
) -> tuple[list[T], int, list[IdConflict]]:
    """Apply the one-row-per-id policy.

    - Exact duplicates (same id, identical field values) collapse to one
      record: they are the same logical event delivered twice.
    - Conflicting ids (same id, different field values) are a data error:
      every row with that id is rejected and reported as an IdConflict.
      No row is silently preferred over another.

    Returns (accepted records in first-seen order, exact duplicate rows
    dropped, conflicts)."""
    groups: dict[str, list[T]] = {}
    for rec in records:
        groups.setdefault(id_of(rec), []).append(rec)

    accepted: list[T] = []
    dropped = 0
    conflicts: list[IdConflict] = []
    for rid, rows in groups.items():
        distinct = list(dict.fromkeys(rows))  # dataclasses are frozen -> hashable
        if len(distinct) == 1:
            accepted.append(rows[0])
            dropped += len(rows) - 1
        else:
            conflicts.append(
                IdConflict(
                    source=source,
                    record_id=rid,
                    row_count=len(rows),
                    detail=f"{len(rows)} rows share {source} id {rid!r} with {len(distinct)} different contents; all rejected",
                )
            )
    return accepted, dropped, conflicts


def _require_unique(records: Iterable[T], id_of: Callable[[T], str], source: str) -> None:
    seen: set[str] = set()
    for rec in records:
        rid = id_of(rec)
        if rid in seen:
            raise ValueError(f"duplicate {source} id {rid!r}: run dedupe_by_id() before reconcile()")
        seen.add(rid)


@dataclass
class ReconciliationResult:
    daily_kpis: list[DailyKPI]
    mismatches: list[Mismatch]

    def totals(self) -> dict:
        """Per-currency money totals plus currency-independent counts.

        Shape::

            {
              "USD": {"gross_cents": .., "net_cents": .., "refunds_cents": ..,
                      "paid_cents": .., "ordered_cents": .., "unmatched_cents": ..},
              "EUR": {...},
              "unmatched_count": 3,        # a count, safe to sum across currencies
              "indeterminate_count": 0,    # findings not asserted: source incomplete
            }

        Currency keys are ISO-style 3-letter codes, so they cannot collide
        with the two lower-case count keys."""
        by_currency: dict[str, dict[str, int]] = {}
        unmatched_count = 0
        for k in self.daily_kpis:
            t = by_currency.setdefault(k.currency, {f: 0 for f in MONEY_FIELDS})
            for f in MONEY_FIELDS:
                t[f] += getattr(k, f)
            unmatched_count += k.unmatched_count
        out: dict = {cur: by_currency[cur] for cur in sorted(by_currency)}
        out["unmatched_count"] = unmatched_count
        out["indeterminate_count"] = sum(1 for m in self.mismatches if m.category == INDETERMINATE)
        return out

    def mismatch_totals(self) -> dict[str, dict]:
        """{category: {"count": n, "amount_cents_by_currency": {cur: cents}}}.
        Counts are summed across currencies; amounts never are."""
        out: dict[str, dict] = {}
        for m in self.mismatches:
            agg = out.setdefault(m.category, {"count": 0, "amount_cents_by_currency": {}})
            agg["count"] += 1
            by_cur = agg["amount_cents_by_currency"]
            by_cur[m.currency] = by_cur.get(m.currency, 0) + m.amount_cents
        for agg in out.values():
            agg["amount_cents_by_currency"] = dict(sorted(agg["amount_cents_by_currency"].items()))
        return out


def reconcile(
    orders: list[Order],
    payments: list[Payment],
    refunds: list[Refund],
    *,
    orders_complete: bool = True,
    payments_complete: bool = True,
) -> ReconciliationResult:
    """Reconcile one run's accepted records.

    `orders_complete` / `payments_complete` say whether that source is known
    to be complete for this run. When it is not, findings that would rest on
    a record being absent from it are downgraded to
    `indeterminate_incomplete_source` instead of being asserted."""
    _require_unique(orders, lambda o: o.order_id, "order")
    _require_unique(payments, lambda p: p.payment_id, "payment")
    _require_unique(refunds, lambda r: r.refund_id, "refund")

    orders_by_id: dict[str, Order] = {o.order_id: o for o in orders}
    payments_by_order: dict[str, list[Payment]] = defaultdict(list)
    for p in payments:
        payments_by_order[p.order_id].append(p)

    mismatches: list[Mismatch] = []

    # --- order-centric checks: missing_payment, overpayment, currency_mismatch, duplicate
    for order in orders:
        if order.status != "completed":
            continue
        order_payments = payments_by_order.get(order.order_id, [])
        if not order_payments:
            if payments_complete:
                mismatches.append(
                    Mismatch(
                        category="missing_payment",
                        day=order.order_date,
                        order_id=order.order_id,
                        payment_id=None,
                        refund_id=None,
                        currency=order.currency,
                        amount_cents=order.amount_cents,
                        details="completed order has no recorded payment",
                    )
                )
            else:
                mismatches.append(
                    Mismatch(
                        category=INDETERMINATE,
                        day=order.order_date,
                        order_id=order.order_id,
                        payment_id=None,
                        refund_id=None,
                        currency=order.currency,
                        amount_cents=order.amount_cents,
                        details="completed order has no accepted payment, but the payments source is incomplete "
                        "this run - cannot assert missing_payment",
                    )
                )
            continue

        # Duplicate: more than one payment recorded for the same order.
        # The first (chronologically, then by payment_id) is treated as the
        # legitimate payment; each additional one is flagged.
        if len(order_payments) > 1:
            ordered_payments = sorted(order_payments, key=lambda p: (p.payment_date, p.payment_id))
            for extra in ordered_payments[1:]:
                mismatches.append(
                    Mismatch(
                        category="duplicate",
                        day=extra.payment_date,
                        order_id=order.order_id,
                        payment_id=extra.payment_id,
                        refund_id=None,
                        currency=extra.currency,
                        amount_cents=extra.amount_cents,
                        details=f"order {order.order_id} already has payment {ordered_payments[0].payment_id}",
                    )
                )

        for payment in order_payments:
            if payment.currency != order.currency:
                # Unlike currencies: the amounts are in different units, so
                # no over/under-payment comparison is meaningful. Report the
                # currency mismatch only.
                mismatches.append(
                    Mismatch(
                        category="currency_mismatch",
                        day=payment.payment_date,
                        order_id=order.order_id,
                        payment_id=payment.payment_id,
                        refund_id=None,
                        currency=payment.currency,
                        amount_cents=payment.amount_cents,
                        details=f"order currency {order.currency} != payment currency {payment.currency}",
                    )
                )
                continue
            if payment.amount_cents > order.amount_cents:
                mismatches.append(
                    Mismatch(
                        category="overpayment",
                        day=payment.payment_date,
                        order_id=order.order_id,
                        payment_id=payment.payment_id,
                        refund_id=None,
                        currency=payment.currency,
                        amount_cents=payment.amount_cents - order.amount_cents,
                        details=f"payment {payment.amount_cents}c > order {order.amount_cents}c ({payment.currency})",
                    )
                )

    # --- payment-centric check: unmatched (payment for an unknown order)
    unmatched: dict[tuple[date, str], list[int]] = defaultdict(lambda: [0, 0])
    for p in payments:
        if p.order_id in orders_by_id:
            continue
        if orders_complete:
            slot = unmatched[(p.payment_date, p.currency)]
            slot[0] += 1
            slot[1] += p.amount_cents
        else:
            mismatches.append(
                Mismatch(
                    category=INDETERMINATE,
                    day=p.payment_date,
                    order_id=p.order_id,
                    payment_id=p.payment_id,
                    refund_id=None,
                    currency=p.currency,
                    amount_cents=p.amount_cents,
                    details=f"payment references order_id {p.order_id} not in this run's orders, but the orders "
                    "source is incomplete - cannot assert unmatched",
                )
            )

    # --- refund-centric check: orphan_refund
    for refund in refunds:
        if refund.order_id in orders_by_id:
            continue
        mismatches.append(
            Mismatch(
                category="orphan_refund" if orders_complete else INDETERMINATE,
                day=refund.refund_date,
                order_id=refund.order_id,
                payment_id=None,
                refund_id=refund.refund_id,
                currency=refund.currency,
                amount_cents=refund.amount_cents,
                details=(
                    f"refund references unknown order_id {refund.order_id}"
                    if orders_complete
                    else f"refund references order_id {refund.order_id} not in this run's orders, but the orders "
                    "source is incomplete - cannot assert orphan_refund"
                ),
            )
        )

    # --- daily KPIs, keyed by (day, currency)
    keys: set[tuple[date, str]] = set()
    gross: dict[tuple[date, str], int] = defaultdict(int)
    for p in payments:
        gross[(p.payment_date, p.currency)] += p.amount_cents
        keys.add((p.payment_date, p.currency))
    refunded: dict[tuple[date, str], int] = defaultdict(int)
    for r in refunds:
        refunded[(r.refund_date, r.currency)] += r.amount_cents
        keys.add((r.refund_date, r.currency))
    ordered: dict[tuple[date, str], int] = defaultdict(int)
    for o in orders:
        if o.status == "completed":
            ordered[(o.order_date, o.currency)] += o.amount_cents
            keys.add((o.order_date, o.currency))
    keys.update(unmatched.keys())

    daily_kpis = []
    for key in sorted(keys):
        day, currency = key
        g = gross.get(key, 0)
        rf = refunded.get(key, 0)
        u_count, u_cents = unmatched.get(key, (0, 0))
        daily_kpis.append(
            DailyKPI(
                day=day,
                currency=currency,
                gross_cents=g,
                net_cents=g - rf,
                refunds_cents=rf,
                paid_cents=g,
                ordered_cents=ordered.get(key, 0),
                unmatched_count=u_count,
                unmatched_cents=u_cents,
            )
        )

    return ReconciliationResult(daily_kpis=daily_kpis, mismatches=mismatches)
