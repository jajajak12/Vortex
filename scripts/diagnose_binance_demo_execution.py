from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db
from exchange.binance_demo import BinanceDemoAdapter, load_dotenv_file
from exchange.binance_demo_executor import ALLOWED_SYMBOLS, BinanceDemoExecutionConfig


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _trade_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def main() -> int:
    load_dotenv_file()
    config = BinanceDemoExecutionConfig.from_env()
    adapter = BinanceDemoAdapter()

    print("Binance Demo Auto Execution Diagnostic")
    print("--------------------------------------")
    print(f"BINANCE_DEMO_MODE={_fmt_bool(config.demo_mode)}")
    print(f"BINANCE_EXECUTION_ENABLED={_fmt_bool(config.execution_enabled)}")
    print(f"BINANCE_AUTO_DEMO_EXECUTION_ENABLED={_fmt_bool(config.auto_execution_enabled)}")
    print(f"base_url={config.base_url}")
    print(f"allowed_strategies={','.join(config.allowed_strategies)}")
    print(f"max_notional={config.max_notional_usdt}")

    startup_at = db.get_binance_demo_auto_startup()
    print(f"last_auto_startup_at={startup_at or '<missing>'}")
    print(f"mapping_table_exists={_fmt_bool(db.binance_demo_execution_table_exists())}")
    print(f"mapping_row_count={db.count_binance_demo_executions()}")

    print("recent_mapping_rows:")
    recent_rows = db.list_recent_binance_demo_executions(limit=10)
    if recent_rows:
        for row in recent_rows:
            print(json.dumps(row, sort_keys=True))
    else:
        print("<none>")

    open_trades = db.get_open_trades()
    strat_counts = Counter(str(trade.get("strategy") or "?") for trade in open_trades)
    print("open_vortex_trades_by_strategy:")
    if strat_counts:
        for strategy in sorted(strat_counts):
            print(f"{strategy}={strat_counts[strategy]}")
    else:
        print("<none>")

    print("manual_only_open_trades_predating_auto_startup:")
    if startup_at:
        startup_dt = datetime.fromisoformat(startup_at)
        printed = False
        for trade in sorted(open_trades, key=lambda item: item["time"]):
            execution_row = db.get_binance_demo_execution(int(trade["id"]))
            if _trade_time(str(trade["time"])) < startup_dt.replace(tzinfo=None) and execution_row is None:
                printed = True
                print(
                    f"trade_id={trade['id']} pair={trade['pair']} strategy={trade['strategy']} "
                    f"direction={trade['direction']} opened_at={trade['time']}"
                )
        if not printed:
            print("<none>")
    else:
        print("<startup timestamp missing>")

    print("binance_positions_and_orders:")
    for symbol in sorted(ALLOWED_SYMBOLS):
        try:
            position_payload = adapter.get_position_risk(symbol)
            open_orders = adapter.get_open_orders(symbol)
            open_algo_orders = adapter.get_open_algo_orders(symbol)
            print(
                f"{symbol} position={json.dumps(position_payload, sort_keys=True)} "
                f"open_orders={len(open_orders or [])} open_algo_orders={len(open_algo_orders or [])}"
            )
        except Exception as exc:
            print(f"{symbol} error={exc}")

    print(f"order_calls_attempted={adapter.order_calls_attempted}")
    print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
