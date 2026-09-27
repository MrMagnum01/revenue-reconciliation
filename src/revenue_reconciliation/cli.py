"""Command-line entry point.

    python -m revenue_reconciliation generate  --out data/raw [--seed 42]
    python -m revenue_reconciliation reconcile --orders-fixture data/raw/orders.json \
        --payments data/raw/payments.csv --refunds data/raw/refunds.csv \
        --db data/output/reconciliation.duckdb --report-dir data/output
    python -m revenue_reconciliation run --raw-dir data/raw --out data/output [--seed 42]

Exit codes for `reconcile` and `run`:
    0  run status `complete`
    1  run status `failed` (no source delivered usable data), or an error
    2  run status `incomplete` (output written, but at least one source
       failed or had records rejected - see the report's `sources`)
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .generator import write_corpus
from .pipeline import fetch_orders_from_api, fetch_orders_from_fixture, run_pipeline

EXIT_CODES = {"complete": 0, "failed": 1, "incomplete": 2}


def _fmt_by_currency(by_cur: dict[str, int]) -> str:
    return ", ".join(f"{cur} {cents}c" for cur, cents in by_cur.items()) or "-"


def _print_report(report: dict) -> None:
    print(f"Run {report['run_id']}: status={report['status']}")
    if report["incomplete_sources"]:
        states = ", ".join(f"{s}={report['sources'][s]}" for s in report["incomplete_sources"])
        print(f"  INCOMPLETE SOURCES: {states} - absence-based findings are reported as indeterminate")
    print(f"Reconciled {report['counts']['orders_ingested']} orders, "
          f"{report['counts']['payments_loaded']} payments, {report['counts']['refunds_loaded']} refunds.")
    totals = report["totals_cents"]
    for cur, t in totals.items():
        if isinstance(t, dict):
            print(f"  {cur}: gross {t['gross_cents']}c  net {t['net_cents']}c  refunds {t['refunds_cents']}c  "
                  f"ordered {t['ordered_cents']}c  unmatched {t['unmatched_cents']}c")
    print(f"  unmatched payments: {totals['unmatched_count']}  indeterminate findings: {totals['indeterminate_count']}")
    for cat, agg in report["mismatches"].items():
        print(f"  {cat}: {agg['count']} ({_fmt_by_currency(agg['amount_cents_by_currency'])})")
    if report["api_failures"]:
        print(f"  API failures: {len(report['api_failures'])}")
    if report["parse_errors"]:
        print(f"  CSV parse errors: {len(report['parse_errors'])}")
    if report["conflicts_count"]:
        print(f"  conflicting ids rejected: {report['conflicts_count']}")


def _cmd_generate(args: argparse.Namespace) -> int:
    truth = write_corpus(Path(args.out), seed=args.seed)
    print(f"Generated synthetic corpus in {args.out} (seed={args.seed}).")
    print(f"  orders: {truth['counts']['orders_completed']} completed + {truth['counts']['orders_cancelled']} cancelled")
    print(f"  payments: {truth['counts']['payments']}  refunds: {truth['counts']['refunds']}")
    for cur, t in truth["totals_cents"].items():
        print(f"  {cur}: true gross {t['gross']}c  true net {t['net']}c")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
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
    _print_report(report)
    print(f"Wrote {args.db} and {args.report_dir}/mismatch_report.json")
    return EXIT_CODES[report["status"]]


def _cmd_run(args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir)
    write_corpus(raw_dir, seed=args.seed)
    orders, failures = fetch_orders_from_fixture(raw_dir / "orders.json")
    outcome = run_pipeline(
        orders,
        failures,
        raw_dir / "payments.csv",
        raw_dir / "refunds.csv",
        Path(args.out) / "reconciliation.duckdb",
        Path(args.out),
        source_note="scheduled run: generate + reconcile",
    )
    _print_report(outcome["report"])
    print(f"Full run finished: raw data in {raw_dir}, output in {args.out}")
    return EXIT_CODES[outcome["report"]["status"]]


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
