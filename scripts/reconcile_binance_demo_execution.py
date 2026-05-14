from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db
from binance_demo_reconciliation import (
    REMOTE_SYMBOLS,
    SAFE_DB_REMOTE_CLOSED_FROM,
    build_reconciliation_snapshot,
)


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Binance demo execution reconciliation.")
    parser.add_argument(
        "--apply-db-status",
        action="store_true",
        help="Only update safe DB mapping statuses such as EXECUTED -> REMOTE_CLOSED when the local trade is closed and no remote state remains.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    snapshot = build_reconciliation_snapshot()
    config = snapshot["config"]

    print("Binance Demo Execution Reconciliation")
    print("=====================================")
    print(f"read_only_default=true")
    print(f"apply_db_status={_fmt_bool(args.apply_db_status)}")
    print(f"base_url={config.base_url}")
    print(f"symbols={','.join(REMOTE_SYMBOLS)}")
    print(f"rate_limit_cooldown_active={_fmt_bool(snapshot['rate_limit_cooldown_active'])}")
    print(f"cooldown_until={snapshot['rate_limit_cooldown_until'] or '<none>'}")
    print(f"last_rate_limit_error={snapshot['last_rate_limit_error'] or '<none>'}")
    print(f"last_rate_limit_seen_at={snapshot['last_rate_limit_seen_at'] or '<none>'}")

    by_class = Counter(row["reconciliation_class"] for row in snapshot["reconciliation_rows"])
    print("reconciliation_class_counts:")
    if by_class:
        for status in sorted(by_class):
            print(f"  {status}={by_class[status]}")
    else:
        print("  <none>")

    safe_updates: list[tuple[int, str, str]] = []
    blocked_updates: list[str] = []
    for row in snapshot["reconciliation_rows"]:
        mapping = row["mapping"]
        trade = row["trade"]
        remote = row["remote"]
        status = str(mapping.get("status") or "").upper()
        if status not in SAFE_DB_REMOTE_CLOSED_FROM:
            continue
        if not trade or str(trade.get("status")) != "CLOSED":
            continue
        if remote.has_position or remote.has_orders:
            blocked_updates.append(
                f"trade_id={mapping['vortex_trade_id']} symbol={row['symbol']} status={status} remote_state_still_present"
            )
            continue
        safe_updates.append((int(mapping["vortex_trade_id"]), status, "REMOTE_CLOSED"))

    print("safe_db_status_updates:")
    if safe_updates:
        for trade_id, current_status, next_status in safe_updates:
            print(f"  trade_id={trade_id} {current_status}->{next_status}")
    else:
        print("  <none>")

    print("blocked_db_status_updates:")
    if blocked_updates:
        for item in blocked_updates:
            print(f"  {item}")
    else:
        print("  <none>")

    if args.apply_db_status:
        updated = []
        for trade_id, _, next_status in safe_updates:
            row = db.update_binance_demo_execution_status(
                trade_id,
                next_status,
                error="reconciled_remote_closed",
            )
            updated.append(row)
        print(f"db_rows_updated={len(updated)}")
    else:
        print("db_rows_updated=0")

    print("warnings:")
    if snapshot["warnings"]:
        for warning in snapshot["warnings"]:
            print(f"  - {warning}")
    else:
        print("  <none>")

    print(f"recommended_action={snapshot['recommended_action']}")
    print(f"order_calls_attempted={snapshot['adapter_order_calls_attempted']}")
    print(f"order_endpoints_called={_fmt_bool(snapshot['adapter_order_endpoints_called'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
