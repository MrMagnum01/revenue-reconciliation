"""Deterministic synthetic order/payment/refund corpus generator.

Everything here is fake: order ids, customer ids, amounts, dates. A fixed
seed makes every run byte-for-byte identical, which is what lets the test
suite assert "the reconciliation pipeline finds exactly these planted
mismatches and these exact totals" (see `tests/test_generator_truth.py`).

The generator plants a known, disjoint set of problems and returns a
`truth` dict recording the ground truth it built - independent bookkeeping,
not derived from `reconcile.py` - so comparing the pipeline's output to
`truth` is a real check of the reconciliation logic, not a tautology.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from .models import Order, Payment, Refund

BASE_DATE = date(2026, 6, 1)
NUM_DAYS = 14
CURRENCIES = ["USD", "EUR", "GBP"]
CURRENCY_WEIGHTS = [0.6, 0.25, 0.15]

N_CANCELLED = 10
N_MISSING_PAYMENT = 6
N_OVERPAYMENT = 5
N_CURRENCY_MISMATCH = 4
N_DUPLICATE = 5
N_NORMAL = 100
N_COMPLETED = N_MISSING_PAYMENT + N_OVERPAYMENT + N_CURRENCY_MISMATCH + N_DUPLICATE + N_NORMAL  # 120
N_ORPHAN_REFUND = 4
N_UNMATCHED_PAYMENT = 3
N_NORMAL_REFUND = 10

REFUND_REASONS = ["customer_return", "duplicate_charge", "service_issue"]
PAYMENT_METHODS = ["card", "bank_transfer", "wallet"]


def _rand_date(rng: random.Random, start: date, spread_days: int) -> date:
    return start + timedelta(days=rng.randint(0, spread_days))


def _other_currency(rng: random.Random, currency: str) -> str:
    return rng.choice([c for c in CURRENCIES if c != currency])


def generate_corpus(seed: int = 42) -> tuple[list[Order], list[Payment], list[Refund], dict]:
    rng = random.Random(seed)

    orders: list[Order] = []
    order_ids_pool: list[str] = []
    for i in range(1, N_COMPLETED + N_CANCELLED + 1):
        order_ids_pool.append(f"ORD-{i:05d}")
    rng.shuffle(order_ids_pool)

    completed_ids = order_ids_pool[:N_COMPLETED]
    cancelled_ids = order_ids_pool[N_COMPLETED:]

    # Disjoint slices of the completed pool for each planted category.
    idx = 0
    missing_ids = completed_ids[idx : idx + N_MISSING_PAYMENT]; idx += N_MISSING_PAYMENT
    overpay_ids = completed_ids[idx : idx + N_OVERPAYMENT]; idx += N_OVERPAYMENT
    curmis_ids = completed_ids[idx : idx + N_CURRENCY_MISMATCH]; idx += N_CURRENCY_MISMATCH
    dup_ids = completed_ids[idx : idx + N_DUPLICATE]; idx += N_DUPLICATE
    normal_ids = completed_ids[idx : idx + N_NORMAL]; idx += N_NORMAL
    assert idx == N_COMPLETED

    customer_ids = [f"CUST-{i:04d}" for i in range(1, 61)]

    def make_order(order_id: str, status: str) -> Order:
        currency = rng.choices(CURRENCIES, weights=CURRENCY_WEIGHTS)[0]
        return Order(
            order_id=order_id,
            customer_id=rng.choice(customer_ids),
            order_date=_rand_date(rng, BASE_DATE, NUM_DAYS - 1),
            currency=currency,
            amount_cents=rng.randint(1000, 50000),
            status=status,
        )

    orders_by_id: dict[str, Order] = {}
    for oid in completed_ids:
        orders_by_id[oid] = make_order(oid, "completed")
    for oid in cancelled_ids:
        orders_by_id[oid] = make_order(oid, "cancelled")
    orders = [orders_by_id[oid] for oid in order_ids_pool]
    orders.sort(key=lambda o: o.order_id)

    payments: list[Payment] = []
    pay_seq = 1

    def next_payment_id() -> str:
        nonlocal pay_seq
        pid = f"PAY-{pay_seq:05d}"
        pay_seq += 1
        return pid

    truth_missing_amount = 0
    for oid in missing_ids:
        truth_missing_amount += orders_by_id[oid].amount_cents  # no payment created

    truth_overpay_amount = 0
    for oid in overpay_ids:
        o = orders_by_id[oid]
        extra = rng.randint(500, 3000)
        payments.append(
            Payment(
                payment_id=next_payment_id(),
                order_id=oid,
                payment_date=_rand_date(rng, o.order_date, 2),
                currency=o.currency,
                amount_cents=o.amount_cents + extra,
                method=rng.choice(PAYMENT_METHODS),
            )
        )
        truth_overpay_amount += extra

    truth_curmis_amount = 0
    for oid in curmis_ids:
        o = orders_by_id[oid]
        bad_currency = _other_currency(rng, o.currency)
        payments.append(
            Payment(
                payment_id=next_payment_id(),
                order_id=oid,
                payment_date=_rand_date(rng, o.order_date, 2),
                currency=bad_currency,
                amount_cents=o.amount_cents,
                method=rng.choice(PAYMENT_METHODS),
            )
        )
        truth_curmis_amount += o.amount_cents

    truth_dup_amount = 0
    for oid in dup_ids:
        o = orders_by_id[oid]
        first_date = _rand_date(rng, o.order_date, 1)
        second_date = first_date + timedelta(days=rng.randint(1, 2))
        payments.append(
            Payment(next_payment_id(), oid, first_date, o.currency, o.amount_cents, rng.choice(PAYMENT_METHODS))
        )
        payments.append(
            Payment(next_payment_id(), oid, second_date, o.currency, o.amount_cents, rng.choice(PAYMENT_METHODS))
        )
        truth_dup_amount += o.amount_cents  # the extra (2nd) payment's amount

    for oid in normal_ids:
        o = orders_by_id[oid]
        payments.append(
            Payment(
                payment_id=next_payment_id(),
                order_id=oid,
                payment_date=_rand_date(rng, o.order_date, 2),
                currency=o.currency,
                amount_cents=o.amount_cents,
                method=rng.choice(PAYMENT_METHODS),
            )
        )

    truth_unmatched_amount = 0
    unmatched_payment_ids = []
    for i in range(1, N_UNMATCHED_PAYMENT + 1):
        ghost_order_id = f"ORD-UNMATCHED-{i:04d}"
        amount = rng.randint(1000, 50000)
        payments.append(
            Payment(
                payment_id=next_payment_id(),
                order_id=ghost_order_id,
                payment_date=_rand_date(rng, BASE_DATE, NUM_DAYS - 1),
                currency=rng.choice(CURRENCIES),
                amount_cents=amount,
                method=rng.choice(PAYMENT_METHODS),
            )
        )
        truth_unmatched_amount += amount
        unmatched_payment_ids.append(ghost_order_id)

    # Refunds
    refunds: list[Refund] = []
    ref_seq = 1

    def next_refund_id() -> str:
        nonlocal ref_seq
        rid = f"REF-{ref_seq:05d}"
        ref_seq += 1
        return rid

    refund_targets = rng.sample(normal_ids, N_NORMAL_REFUND)
    normal_payment_by_order = {p.order_id: p for p in payments if p.order_id in normal_ids}
    for oid in refund_targets:
        o = orders_by_id[oid]
        pay = normal_payment_by_order[oid]
        portion = rng.uniform(0.2, 1.0)
        amount = max(1, int(o.amount_cents * portion))
        refunds.append(
            Refund(
                refund_id=next_refund_id(),
                order_id=oid,
                refund_date=pay.payment_date + timedelta(days=rng.randint(1, 5)),
                currency=o.currency,
                amount_cents=amount,
                reason=rng.choice(REFUND_REASONS),
            )
        )

    truth_orphan_refund_amount = 0
    for i in range(1, N_ORPHAN_REFUND + 1):
        ghost_order_id = f"ORD-ORPHAN-{i:04d}"
        amount = rng.randint(500, 20000)
        refunds.append(
            Refund(
                refund_id=next_refund_id(),
                order_id=ghost_order_id,
                refund_date=_rand_date(rng, BASE_DATE, NUM_DAYS - 1 + 5),
                currency=rng.choice(CURRENCIES),
                amount_cents=amount,
                reason=rng.choice(REFUND_REASONS),
            )
        )
        truth_orphan_refund_amount += amount

    total_ordered_cents = sum(orders_by_id[oid].amount_cents for oid in completed_ids)
    total_gross_cents = sum(p.amount_cents for p in payments)
    total_refunds_cents = sum(r.amount_cents for r in refunds)

    truth = {
        "seed": seed,
        "counts": {
            "orders_completed": N_COMPLETED,
            "orders_cancelled": N_CANCELLED,
            "payments": len(payments),
            "refunds": len(refunds),
        },
        "totals_cents": {
            "gross": total_gross_cents,
            "net": total_gross_cents - total_refunds_cents,
            "refunds": total_refunds_cents,
            "paid": total_gross_cents,
            "ordered": total_ordered_cents,
            "unmatched": truth_unmatched_amount,
        },
        "unmatched_count": N_UNMATCHED_PAYMENT,
        "mismatches": {
            "missing_payment": {"count": N_MISSING_PAYMENT, "amount_cents": truth_missing_amount},
            "overpayment": {"count": N_OVERPAYMENT, "amount_cents": truth_overpay_amount},
            "currency_mismatch": {"count": N_CURRENCY_MISMATCH, "amount_cents": truth_curmis_amount},
            "duplicate": {"count": N_DUPLICATE, "amount_cents": truth_dup_amount},
            "orphan_refund": {"count": N_ORPHAN_REFUND, "amount_cents": truth_orphan_refund_amount},
        },
    }

    payments.sort(key=lambda p: p.payment_id)
    refunds.sort(key=lambda r: r.refund_id)
    return orders, payments, refunds, truth


def write_corpus(out_dir: Path, seed: int = 42) -> dict:
    """Writes orders.json (mock-API fixture), payments.csv, refunds.csv and
    truth.json into `out_dir`. Returns the truth dict."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    orders, payments, refunds, truth = generate_corpus(seed)

    orders_path = out_dir / "orders.json"
    orders_path.write_text(
        json.dumps(
            [
                {**asdict(o), "order_date": o.order_date.isoformat()}
                for o in orders
            ],
            indent=2,
        )
    )

    import csv

    payments_path = out_dir / "payments.csv"
    with payments_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["payment_id", "order_id", "payment_date", "currency", "amount_cents", "method"])
        for p in payments:
            writer.writerow([p.payment_id, p.order_id, p.payment_date.isoformat(), p.currency, p.amount_cents, p.method])

    refunds_path = out_dir / "refunds.csv"
    with refunds_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["refund_id", "order_id", "refund_date", "currency", "amount_cents", "reason"])
        for r in refunds:
            writer.writerow([r.refund_id, r.order_id, r.refund_date.isoformat(), r.currency, r.amount_cents, r.reason])

    truth_path = out_dir / "truth.json"
    truth_path.write_text(json.dumps(truth, indent=2))

    return truth


def load_orders_fixture(path: Path) -> list[Order]:
    raw = json.loads(Path(path).read_text())
    return [
        Order(
            order_id=r["order_id"],
            customer_id=r["customer_id"],
            order_date=date.fromisoformat(r["order_date"]),
            currency=r["currency"],
            amount_cents=int(r["amount_cents"]),
            status=r["status"],
        )
        for r in raw
    ]
