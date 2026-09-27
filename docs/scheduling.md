# Scheduled run

This demo never installs a timer on the build box - both examples below are
documentation, to paste into a real deployment. The pipeline runs as one
CLI command either way:

```bash
cd /opt/revenue-reconciliation
source .venv/bin/activate
python -m revenue_reconciliation run \
  --raw-dir data/raw \
  --out data/output \
  --seed 42
```

In production, `run` would point at a real orders API (`--orders-api-url`
via a small wrapper, or extend `cli.py`'s `run` subcommand to accept it)
instead of regenerating a synthetic fixture every time; the synthetic
`generate` step here exists so this demo is self-contained and
reproducible without any real backend.

## Option A - cron

```cron
# /etc/cron.d/revenue-reconciliation - runs daily at 02:15
15 2 * * * appuser cd /opt/revenue-reconciliation && \
  /opt/revenue-reconciliation/.venv/bin/python -m revenue_reconciliation run \
  --raw-dir /var/lib/revenue-reconciliation/raw \
  --out /var/lib/revenue-reconciliation/output \
  --seed 42 >> /var/log/revenue-reconciliation.log 2>&1
```

## Option B - systemd timer

`/etc/systemd/system/revenue-reconciliation.service`:

```ini
[Unit]
Description=Revenue reconciliation run

[Service]
Type=oneshot
User=appuser
WorkingDirectory=/opt/revenue-reconciliation
ExecStart=/opt/revenue-reconciliation/.venv/bin/python -m revenue_reconciliation run \
  --raw-dir /var/lib/revenue-reconciliation/raw \
  --out /var/lib/revenue-reconciliation/output \
  --seed 42
```

`/etc/systemd/system/revenue-reconciliation.timer`:

```ini
[Unit]
Description=Run revenue reconciliation daily

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with `systemctl enable --now revenue-reconciliation.timer` - **not
run on this build box**; this is reference only.

## Alerting on mismatches

The CLI exits non-zero unless the run is `complete` (`2` = incomplete,
`1` = failed or error), so cron/systemd already surface an ingestion
outage as a failed job. `mismatch_report.json` (written to `--out`
alongside the DuckDB file) has a flat, greppable shape -
`report["status"]`, `report["ok"]`, `report["mismatches"]["missing_payment"]["count"]`,
and per-currency amounts under
`report["mismatches"][<category>]["amount_cents_by_currency"]` - so a
scheduled run can pipe it into a simple threshold check (e.g. `jq` in the
same cron line, or a follow-up script) without querying DuckDB at all.
Check `status` first: counts from an `incomplete` run exclude findings
that could not be determined. The DuckDB `mismatches` table is there for anyone who wants to
slice by date range or category with SQL instead.
