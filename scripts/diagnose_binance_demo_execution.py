from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from binance_demo_reconciliation import REMOTE_SYMBOLS, build_reconciliation_snapshot


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _fmt_decimal(value) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def main() -> int:
    snapshot = build_reconciliation_snapshot()
    config = snapshot["config"]

    print("Binance Demo Execution Diagnostics")
    print("==================================")

    print("\n[1] Runtime Flags")
    print(f"BINANCE_DEMO_MODE={_fmt_bool(config.demo_mode)}")
    print(f"BINANCE_EXECUTION_ENABLED={_fmt_bool(config.execution_enabled)}")
    print(f"BINANCE_AUTO_DEMO_EXECUTION_ENABLED={_fmt_bool(config.auto_execution_enabled)}")
    print(f"base_url={config.base_url}")
    print(f"allowed_strategies={','.join(config.allowed_strategies)}")
    print(f"max_notional={config.max_notional_usdt}")
    print(f"last_auto_startup_at={snapshot['startup_at'] or '<missing>'}")
    print(f"rate_limit_cooldown_active={_fmt_bool(snapshot['rate_limit_cooldown_active'])}")
    print(f"cooldown_until={snapshot['rate_limit_cooldown_until'] or '<none>'}")
    print(f"last_rate_limit_error={snapshot['last_rate_limit_error'] or '<none>'}")
    print(f"last_rate_limit_seen_at={snapshot['last_rate_limit_seen_at'] or '<none>'}")

    print("\n[2] Mapping Summary")
    mappings = snapshot["mappings"]
    print(f"total_mapping_rows={len(mappings)}")
    by_status = Counter(str(row.get("status") or "<missing>") for row in mappings)
    print("count_by_mapping_status:")
    for status in sorted(by_status):
        print(f"  {status}={by_status[status]}")
    by_class = Counter(row["reconciliation_class"] for row in snapshot["reconciliation_rows"])
    print("count_by_reconciliation_class:")
    for status in sorted(by_class):
        print(f"  {status}={by_class[status]}")

    print("\n[3] Per-Symbol Remote State")
    for symbol in REMOTE_SYMBOLS:
        remote = snapshot["remote_by_symbol"][symbol]
        active_dirs = ",".join(remote.active_direction_values) if remote.active_direction_values else "NONE"
        print(
            f"{symbol} remote_position_direction={remote.position_direction} positionAmt={_fmt_decimal(remote.position_amt)} "
            f"notional={_fmt_decimal(remote.notional)} entryPrice={_fmt_decimal(remote.entry_price)} "
            f"unrealizedPnL={_fmt_decimal(remote.unrealized_pnl)} standard_open_orders={len(remote.standard_orders)} "
            f"open_algo_orders={len(remote.algo_orders)} TP_algo_count={remote.tp_algo_count} SL_algo_count={remote.sl_algo_count} "
            f"active_mirrored_legs={len(remote.active_mappings)} active_mirrored_quantity={_fmt_decimal(remote.active_quantity_sum)} "
            f"active_mirrored_directions={active_dirs} protection_complete={_fmt_bool(remote.protection_complete)}"
        )
        if remote.remote_error:
            print(f"  remote_error={remote.remote_error}")

    print("\n[4] Per Open Vortex Trade Mirror Status")
    if snapshot["open_trade_rows"]:
        for row in snapshot["open_trade_rows"]:
            trade = row["trade"]
            mapping = row["mapping"]
            print(
                f"id={trade['id']} pair={trade['pair']} strategy={trade['strategy']} direction={trade['direction']} "
                f"entry={trade['entry']} sl={trade['sl']} tp={trade['tp']} risk_usd={trade.get('risk_usd')} "
                f"local_status={trade['status']} mapping_id={(mapping or {}).get('id', '<none>')} "
                f"mapping_status={(mapping or {}).get('status', '<none>')} mapping_error={(mapping or {}).get('error', '<none>')} "
                f"mirror_status={row['mirror_status']}"
            )
    else:
        print("<none>")

    print("\n[5] Protection Status Per Active Mapping")
    if snapshot["active_mapping_rows"]:
        for row in snapshot["active_mapping_rows"]:
            mapping = row["mapping"]
            trade = row["trade"]
            print(
                f"trade_id={mapping['vortex_trade_id']} symbol={row['symbol']} strategy={(trade or {}).get('strategy', mapping.get('strategy'))} "
                f"direction={mapping.get('direction')} mapping_status={mapping.get('status')} protection_status={row['protection_status']} "
                f"tp_order_live={_fmt_bool(row['tp_order_live'])} sl_order_live={_fmt_bool(row['sl_order_live'])} "
                f"position_live={_fmt_bool(row['position_live'])}"
            )
    else:
        print("<none>")

    print("\n[6] Binance UI Behavior")
    print(snapshot["diagnostic_note"])

    print("\n[7] Inconsistency Warnings")
    if snapshot["warnings"]:
        for warning in snapshot["warnings"]:
            print(f"- {warning}")
    else:
        print("NO_INCONSISTENCIES")

    print("\n[8] Recommended Action")
    print(snapshot["recommended_action"])

    print("\nRead-only Safety")
    print(f"order_calls_attempted={snapshot['adapter_order_calls_attempted']}")
    print(f"order_endpoints_called={_fmt_bool(snapshot['adapter_order_endpoints_called'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
