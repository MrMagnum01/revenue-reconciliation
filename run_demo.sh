#!/usr/bin/env bash
# One-command demo run: generate the synthetic orders/payments/refunds
# corpus, then reconcile it into DuckDB. Assumes the venv is already
# created and activated (see README "Setup"), or falls back to the system
# python3 if not.
#
# Sample output (seed 42) - every money figure is per currency, never a
# combined total:
#   Run run-...: status=complete
#   Reconciled 130 orders, 122 payments, 14 refunds.
#     EUR: gross 665423c  net 637707c  refunds 27716c  ordered 613121c  unmatched 48270c
#     GBP: gross 487405c  net 462293c  refunds 25112c  ordered 446324c  unmatched 63237c
#     USD: gross 1843833c  net 1692684c  refunds 151149c  ordered 1869579c  unmatched 0c
#     unmatched payments: 3  indeterminate findings: 0
#     missing_payment: 6 (EUR 64646c, GBP 11484c, USD 37428c)
#     ...
#
# The reconcile step exits 0 only for a `complete` run (2 = incomplete,
# 1 = failed); this script passes that exit code through.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

RAW_DIR="${1:-data/raw}"
OUT_DIR="${2:-data/output}"

python3 -m revenue_reconciliation generate --out "$RAW_DIR" --seed 42

status=0
python3 -m revenue_reconciliation reconcile \
  --orders-fixture "$RAW_DIR/orders.json" \
  --payments "$RAW_DIR/payments.csv" \
  --refunds "$RAW_DIR/refunds.csv" \
  --db "$OUT_DIR/reconciliation.duckdb" \
  --report-dir "$OUT_DIR" || status=$?

echo
if [ "$status" -eq 0 ]; then
  echo "Done. DuckDB at $OUT_DIR/reconciliation.duckdb, mismatch report at $OUT_DIR/mismatch_report.json."
else
  echo "Reconcile run was NOT complete (exit $status) - see \"status\" and \"sources\" in $OUT_DIR/mismatch_report.json." >&2
fi
exit "$status"
