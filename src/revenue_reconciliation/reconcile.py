"""Order/payment/refund matching, mismatch categorisation, and daily KPIs.

Categories (mismatch report):
  missing_payment   - a completed order with zero payments recorded.
  overpayment       - a payment greater than its order's amount.
  orphan_refund     - a refund whose order_id matches no known order.
  currency_mismatch - a payment whose currency differs from its order's.
  duplicate         - an order with more than one payment recorded for it
                       (the extra payment(s) beyond the first are flagged).

"Unmatched" (a KPI, not a mismatch category) is a payment whose order_id
matches no known order at all - money came in for an order we never heard
of, which is a different failure mode than an order that was never paid.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .models import DailyKPI, Mismatch, Order, Payment, Refund


@dataclass
class ReconciliationResult:
    daily_kpis: list[DailyKPI]
    mismatches: list[Mismatch]

    def totals(self) -> dict[str, int]:
        t = {
            "gross_cents": 0,
            "net_cents": 0,
            "refunds_cents": 0,
            "paid_cents": 0,
            "ordered_cents": 0,
            "unmatched_count": 0,
            "unmatched_cents": 0,
        }
        for k in self.daily_kpis:
            t["gross_cents"] += k.gross_cents
            t["net_cents"] += k.net_cents
            t["refunds_cents"] += k.refunds_cents
            t["paid_cents"] += k.paid_cents
            t["ordered_cents"] += k.ordered_cents
            t["unmatched_count"] += k.unmatched_count
            t["unmatched_cents"] += k.unmatched_cents
        return t

    def mismatch_totals(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = defaultdict(lambda: {"count": 0, "amount_cents": 0})
        for m in self.mismatches:
            out[m.category]["count"] += 1
            out[m.category]["amount_cents"] += m.amount_cents
        return dict(out)


def reconcile(orders: list[Order], payments: list[Payment], refunds: list[Refund]) -> ReconciliationResult:
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
                        details=f"payment {payment.amount_cents}c > order {order.amount_cents}c",
                    )
                )

    # --- payment-centric check: unmatched (payment for an unknown order)
    unmatched_by_day: dict[date, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for p in payments:
        if p.order_id not in orders_by_id:
            count, amount = unmatched_by_day[p.payment_date]
            unmatched_by_day[p.payment_date] = (count + 1, amount + p.amount_cents)

    # --- refund-centric check: orphan_refund
    for refund in refunds:
        if refund.order_id not in orders_by_id:
            mismatches.append(
                Mismatch(
                    category="orphan_refund",
                    day=refund.refund_date,
                    order_id=refund.order_id,
                    payment_id=None,
                    refund_id=refund.refund_id,
                    currency=refund.currency,
                    amount_cents=refund.amount_cents,
                    details=f"refund references unknown order_id {refund.order_id}",
                )
            )

    # --- daily KPIs
    days: set[date] = set()
    gross_by_day: dict[date, int] = defaultdict(int)
    for p in payments:
        gross_by_day[p.payment_date] += p.amount_cents
        days.add(p.payment_date)
    refunds_by_day: dict[date, int] = defaultdict(int)
    for r in refunds:
        refunds_by_day[r.refund_date] += r.amount_cents
        days.add(r.refund_date)
    ordered_by_day: dict[date, int] = defaultdict(int)
    for o in orders:
        if o.status == "completed":
            ordered_by_day[o.order_date] += o.amount_cents
            days.add(o.order_date)
    days.update(unmatched_by_day.keys())

    daily_kpis = []
    for day in sorted(days):
        gross = gross_by_day.get(day, 0)
        refunded = refunds_by_day.get(day, 0)
        u_count, u_cents = unmatched_by_day.get(day, (0, 0))
        daily_kpis.append(
            DailyKPI(
                day=day,
                gross_cents=gross,
                net_cents=gross - refunded,
                refunds_cents=refunded,
                paid_cents=gross,
                ordered_cents=ordered_by_day.get(day, 0),
                unmatched_count=u_count,
                unmatched_cents=u_cents,
            )
        )

    return ReconciliationResult(daily_kpis=daily_kpis, mismatches=mismatches)
