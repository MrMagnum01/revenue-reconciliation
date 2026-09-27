#!/usr/bin/env bash
# One-command demo run: generate the synthetic orders/payments/refunds
# corpus, then reconcile it into DuckDB. Assumes the venv is already
# created and activated (see README "Setup"), or falls back to the system
# python3 if not.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

RAW_DIR="${1:-data/raw}"
OUT_DIR="${2:-data/output}"

python3 -m revenue_reconciliation generate --out "$RAW_DIR" --seed 42
python3 -m revenue_reconciliation reconcile \
  --orders-fixture "$RAW_DIR/orders.json" \
  --payments "$RAW_DIR/payments.csv" \
  --refunds "$RAW_DIR/refunds.csv" \
  --db "$OUT_DIR/reconciliation.duckdb" \
  --report-dir "$OUT_DIR"

echo
echo "Done. DuckDB at $OUT_DIR/reconciliation.duckdb, mismatch report at $OUT_DIR/mismatch_report.json."
