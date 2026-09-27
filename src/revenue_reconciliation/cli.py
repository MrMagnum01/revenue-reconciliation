"""Command-line entry point.

    python -m revenue_reconciliation generate  --out data/raw [--seed 42]
    python -m revenue_reconciliation reconcile --orders-fixture data/raw/orders.json \
        --payments data/raw/payments.csv --refunds data/raw/refunds.csv \
        --db data/output/reconciliation.duckdb --report-dir data/output
    python -m revenue_reconciliation run --raw-dir data/raw --out data/output [--seed 42]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .generator import write_corpus
from .pipeline import fetch_orders_from_api, fetch_orders_from_fixture, run_pipeline


def _cmd_generate(args: argparse.Namespace) -> None:
    truth = write_corpus(Path(args.out), seed=args.seed)
    print(f"Generated synthetic corpus in {args.out} (seed={args.seed}).")
    print(f"  orders: {truth['counts']['orders_completed']} completed + {truth['counts']['orders_cancelled']} cancelled")
    print(f"  payments: {truth['counts']['payments']}  refunds: {truth['counts']['refunds']}")
    print(f"  true gross: {truth['totals_cents']['gross']}c  true net: {truth['totals_cents']['net']}c")


def _cmd_reconcile(args: argparse.Namespace) -> None:
    if args.orders_fixture:
        orders, failures = fetch_orders_from_fixture(Path(args.orders_fixture))
    else:
        orders, failures = fetch_orders_from_api(args.orders_api_url)

    outcome = run_pipeline(
        orders,
        failures,
        Path(args.payments),
        Path(args.refunds),
        Path(args.db),
        Path(args.report_dir),
        source_note=args.orders_fixture or args.orders_api_url or "",
    )
    report = outcome["report"]
    print(f"Reconciled {report['counts']['orders_ingested']} orders, "
          f"{report['counts']['payments_loaded']} payments, {report['counts']['refunds_loaded']} refunds.")
    for cat, agg in report["mismatches"].items():
        print(f"  {cat}: {agg['count']} ({agg['amount_cents']}c)")
    if report["api_failures"]:
        print(f"  API failures: {len(report['api_failures'])}")
    if report["parse_errors"]:
        print(f"  CSV parse errors: {len(report['parse_errors'])}")
    print(f"Wrote {args.db} and {args.report_dir}/mismatch_report.json")


def _cmd_run(args: argparse.Namespace) -> None:
    raw_dir = Path(args.raw_dir)
    write_corpus(raw_dir, seed=args.seed)
    orders, failures = fetch_orders_from_fixture(raw_dir / "orders.json")
    run_pipeline(
        orders,
        failures,
        raw_dir / "payments.csv",
        raw_dir / "refunds.csv",
        Path(args.out) / "reconciliation.duckdb",
        Path(args.out),
        source_note="scheduled run: generate + reconcile",
    )
    print(f"Full run complete: raw data in {raw_dir}, output in {args.out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="revenue_reconciliation",
        description="Synthetic multi-source revenue reconciliation demo (orders API + payments/refunds CSV -> DuckDB).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate the synthetic orders/payments/refunds corpus.")
    gen.add_argument("--out", required=True, help="Output directory for orders.json, payments.csv, refunds.csv, truth.json.")
    gen.add_argument("--seed", type=int, default=42, help="Random seed (default: 42).")
    gen.set_defaults(func=_cmd_generate)

    rec = sub.add_parser("reconcile", help="Reconcile orders (API) against payments/refunds (CSV) into DuckDB.")
    src = rec.add_mutually_exclusive_group(required=True)
    src.add_argument("--orders-api-url", help="Base URL of an already-running orders API.")
    src.add_argument("--orders-fixture", help="Path to an orders.json fixture; starts an in-process mock API to serve it.")
    rec.add_argument("--payments", required=True, help="Path to payments.csv")
    rec.add_argument("--refunds", required=True, help="Path to refunds.csv")
    rec.add_argument("--db", required=True, help="Path to the DuckDB database file to write.")
    rec.add_argument("--report-dir", required=True, help="Directory to write mismatch_report.json / mismatches.csv into.")
    rec.set_defaults(func=_cmd_reconcile)

    run = sub.add_parser("run", help="generate + reconcile in one step (what the scheduled job runs).")
    run.add_argument("--raw-dir", required=True, help="Directory for generated orders.json/payments.csv/refunds.csv.")
    run.add_argument("--out", required=True, help="Directory for reconciliation.duckdb and the mismatch report.")
    run.add_argument("--seed", type=int, default=42, help="Random seed (default: 42).")
    run.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
