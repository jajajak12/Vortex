from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from db import DB_PATH
from exchange.binance_demo import BinanceDemoAdapter, load_dotenv_file
from risk_manager import RiskManager


ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}
ALLOWED_STRATEGIES = {"S5", "S7", "S8", "S9", "S10"}
ALLOWED_DIRECTIONS = {"LONG", "SHORT"}
DEFAULT_MAX_NOTIONAL_USDT = Decimal("5000")
MAX_STAGE3A_NOTIONAL_USDT = Decimal("5000")
ZERO_EPSILON = Decimal("0.00000001")
BRIDGE_LOG_PATH = REPO_ROOT / "data" / "binance_demo_bridge_executions.jsonl"


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _to_decimal(value: Any, *, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _decimal_to_str(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _order_identifier(order: dict[str, Any] | None) -> Any:
    if not order:
        return "<not placed>"
    return order.get("orderId", order.get("algoId", "<not placed>"))


def _order_status(order: dict[str, Any] | None) -> Any:
    if not order:
        return "<not placed>"
    return order.get("status", order.get("algoStatus", "<not placed>"))


def _round_down_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def _trade_db_uri() -> str:
    return f"file:{DB_PATH}?mode=ro"


def _fetch_trade(trade_id: int) -> dict[str, Any] | None:
    con = sqlite3.connect(_trade_db_uri(), uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT
                id, pair, direction, strategy, entry, sl, tp, rr,
                position_usdt, risk_usd, status, time
            FROM trades
            WHERE id = ?
            """,
            (trade_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _find_position_row(position_payload: Any, symbol: str) -> dict[str, Any] | None:
    rows = position_payload if isinstance(position_payload, list) else [position_payload]
    for row in rows or []:
        if str(row.get("symbol", "")).upper() == symbol:
            return row
    return None


def _position_amt_from_payload(position_payload: Any, symbol: str) -> Decimal:
    row = _find_position_row(position_payload, symbol)
    if not row:
        return Decimal("0")
    return _to_decimal(row.get("positionAmt", "0"))


def _load_bridge_log_entry(trade_id: int) -> dict[str, Any] | None:
    if not BRIDGE_LOG_PATH.exists():
        return None

    latest: dict[str, Any] | None = None
    with BRIDGE_LOG_PATH.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("vortex_trade_id") == trade_id:
                latest = payload
    return latest


def _append_bridge_log(payload: dict[str, Any]) -> None:
    BRIDGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BRIDGE_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _print_trade_summary(trade: dict[str, Any]) -> None:
    print(f"id={trade['id']}")
    print(f"pair={trade['pair']}")
    print(f"direction={trade['direction']}")
    print(f"strategy={trade['strategy']}")
    print(f"entry={trade['entry']}")
    print(f"sl={trade['sl']}")
    print(f"tp={trade['tp']}")
    print(f"rr={trade['rr']}")
    print(f"position_usdt={trade['position_usdt']}")
    print(f"risk_usd={trade['risk_usd']}")
    print(f"status={trade['status']}")
    print(f"open_time={trade['time']}")


def _print_binance_state(adapter: BinanceDemoAdapter, symbol: str) -> None:
    ticker = adapter.ticker_price(symbol)
    filters = adapter.get_symbol_filters(symbol)
    position_payload = adapter.get_position_risk(symbol)
    open_orders = adapter.get_open_orders(symbol)
    open_algo_orders = adapter.get_open_algo_orders(symbol)

    print(f"demo_mode={_fmt_bool(adapter.demo_mode)}")
    print(f"execution_enabled={_fmt_bool(adapter.execution_enabled)}")
    print(f"base_url={adapter.base_url}")
    print(f"current_ticker_price={ticker['price']}")
    print("symbol_filters=" + json.dumps(filters, sort_keys=True))
    print("current_position_risk=" + json.dumps(position_payload, sort_keys=True))
    print("open_orders=" + json.dumps(open_orders, sort_keys=True))
    print("open_algo_orders=" + json.dumps(open_algo_orders, sort_keys=True))
    print(f"open_orders_count={len(open_orders or []) + len(open_algo_orders or [])}")


def _validate_runtime_guard(adapter: BinanceDemoAdapter, confirmation_flag: bool, mode: str) -> None:
    if not adapter.demo_mode:
        raise RuntimeError(f"Refusing {mode} because BINANCE_DEMO_MODE is not true.")
    if not adapter.execution_enabled:
        raise RuntimeError(f"Refusing {mode} because BINANCE_EXECUTION_ENABLED is not true.")
    if adapter._is_mainnet_like_host():
        raise RuntimeError(f"Refusing {mode} because base URL '{adapter.base_url}' looks like mainnet.")
    if not confirmation_flag:
        raise RuntimeError(f"Refusing {mode} because the required confirmation flag is missing.")


def _entry_exit_sides(direction: str) -> tuple[str, str]:
    normalized = direction.strip().upper()
    if normalized == "LONG":
        return "BUY", "SELL"
    if normalized == "SHORT":
        return "SELL", "BUY"
    raise RuntimeError(f"Unsupported Vortex direction: {direction}")


def _validate_trade_for_execute(trade: dict[str, Any]) -> None:
    if trade["status"] != "OPEN":
        raise RuntimeError(f"Refusing execute because trade {trade['id']} status is {trade['status']}, not OPEN.")
    if trade["strategy"] not in ALLOWED_STRATEGIES:
        raise RuntimeError(
            f"Refusing execute because trade {trade['id']} strategy {trade['strategy']} is not eligible."
        )
    if trade["pair"] not in ALLOWED_SYMBOLS:
        raise RuntimeError(f"Refusing execute because trade {trade['id']} pair {trade['pair']} is not supported.")
    if trade["direction"] not in ALLOWED_DIRECTIONS:
        raise RuntimeError(
            f"Refusing execute because trade {trade['id']} direction {trade['direction']} is not supported."
        )


def _risk_usd_for_trade(trade: dict[str, Any]) -> Decimal:
    stored_risk = _to_decimal(trade.get("risk_usd"), default="0")
    if stored_risk > 0:
        return stored_risk
    manager = RiskManager()
    current_equity = _to_decimal(manager.get_current_equity())
    return _to_decimal(min(float(current_equity) * 0.02, 500.0))


def _safe_notional_cap(requested_cap: Decimal) -> Decimal:
    if requested_cap <= 0:
        raise RuntimeError("Refusing execute because --max-notional-usdt must be positive.")
    if requested_cap > MAX_STAGE3A_NOTIONAL_USDT:
        raise RuntimeError(
            f"Refusing execute because --max-notional-usdt cannot exceed {MAX_STAGE3A_NOTIONAL_USDT}."
        )
    return requested_cap


def _cleanup_symbol(adapter: BinanceDemoAdapter, symbol: str) -> dict[str, Any]:
    open_orders = adapter.get_open_orders(symbol)
    open_algo_orders = adapter.get_open_algo_orders(symbol)
    cancelled_order_ids: list[Any] = []
    cancelled_algo_order_ids: list[Any] = []
    for order in open_orders or []:
        order_id = order.get("orderId")
        if order_id is None:
            continue
        adapter.cancel_order(symbol, order_id)
        cancelled_order_ids.append(order_id)
    for algo_order in open_algo_orders or []:
        algo_id = algo_order.get("algoId")
        if algo_id is None:
            continue
        adapter.cancel_algo_order(algo_id)
        cancelled_algo_order_ids.append(algo_id)

    position_payload = adapter.get_position_risk(symbol)
    position_amt = _position_amt_from_payload(position_payload, symbol)
    close_order: dict[str, Any] | None = None

    if abs(position_amt) > ZERO_EPSILON:
        close_side = "SELL" if position_amt > 0 else "BUY"
        close_order = adapter.place_market_order(
            symbol=symbol,
            side=close_side,
            quantity=_decimal_to_str(abs(position_amt)),
            reduce_only=True,
        )

    final_position_payload = adapter.get_position_risk(symbol)
    final_position_amt = _position_amt_from_payload(final_position_payload, symbol)
    final_open_orders = adapter.get_open_orders(symbol)
    final_open_algo_orders = adapter.get_open_algo_orders(symbol)

    return {
        "cancelled_order_ids": cancelled_order_ids,
        "cancelled_algo_order_ids": cancelled_algo_order_ids,
        "close_order": close_order,
        "final_position_amt": final_position_amt,
        "final_open_orders_count": len(final_open_orders or []) + len(final_open_algo_orders or []),
    }


def _status_mode(args: argparse.Namespace) -> int:
    trade = _fetch_trade(args.trade_id)
    if trade is None:
        print(f"error=trade_not_found trade_id={args.trade_id}")
        return 1

    adapter = BinanceDemoAdapter()
    print("Manual Vortex Demo Bridge Status")
    print("--------------------------------")
    _print_trade_summary(trade)
    _print_binance_state(adapter, trade["pair"])
    print(f"order_calls_attempted={adapter.order_calls_attempted}")
    print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
    return 0


def _execute_mode(args: argparse.Namespace) -> int:
    adapter = BinanceDemoAdapter()
    entry_order: dict[str, Any] | None = None
    tp_order: dict[str, Any] | None = None
    sl_order: dict[str, Any] | None = None
    cleanup_result: dict[str, Any] | None = None
    final_position_amt = Decimal("0")
    open_orders_count = 0
    logged_status = "not_started"
    trade: dict[str, Any] | None = None
    symbol = ""
    current_price = Decimal("0")
    rounded_qty = Decimal("0")
    capped_qty = Decimal("0")
    risk_usd = Decimal("0")
    notional = Decimal("0")
    requested_cap = args.max_notional_usdt
    safe_cap = Decimal("0")
    cleanup_required = False
    decision_reason = "not_evaluated"

    print("Manual Vortex Demo Bridge Execute")
    print("---------------------------------")
    print("strategy_runtime_connected=false")

    try:
        _validate_runtime_guard(adapter, args.confirm_demo_execution, "execute")
        trade = _fetch_trade(args.trade_id)
        if trade is None:
            raise RuntimeError(f"Refusing execute because trade {args.trade_id} does not exist.")
        _validate_trade_for_execute(trade)

        symbol = str(trade["pair"]).upper()
        entry_side, exit_side = _entry_exit_sides(str(trade["direction"]))
        safe_cap = _safe_notional_cap(requested_cap)

        ticker = adapter.ticker_price(symbol)
        filters = adapter.get_symbol_filters(symbol)
        position_payload = adapter.get_position_risk(symbol)
        open_orders = adapter.get_open_orders(symbol)
        open_algo_orders = adapter.get_open_algo_orders(symbol)

        current_price = _to_decimal(ticker["price"])
        existing_position_amt = _position_amt_from_payload(position_payload, symbol)
        if abs(existing_position_amt) > ZERO_EPSILON:
            raise RuntimeError(
                f"Refusing execute because symbol {symbol} already has a demo position ({existing_position_amt})."
            )
        existing_order_count = len(open_orders or []) + len(open_algo_orders or [])
        if existing_order_count > 0:
            raise RuntimeError(
                f"Refusing execute because symbol {symbol} already has {existing_order_count} open demo orders."
            )

        sl = _to_decimal(trade["sl"])
        tp = _to_decimal(trade["tp"])
        if trade["direction"] == "LONG":
            if sl >= current_price:
                raise RuntimeError("Refusing execute because LONG stop loss is not below current market price.")
            if tp <= current_price:
                raise RuntimeError("Refusing execute because LONG take profit is not above current market price.")
            risk_per_unit = current_price - sl
        else:
            if sl <= current_price:
                raise RuntimeError("Refusing execute because SHORT stop loss is not above current market price.")
            if tp >= current_price:
                raise RuntimeError("Refusing execute because SHORT take profit is not below current market price.")
            risk_per_unit = sl - current_price

        if risk_per_unit <= 0:
            raise RuntimeError("Refusing execute because risk_per_unit <= 0.")

        risk_usd = _risk_usd_for_trade(trade)
        step_size = _to_decimal(filters["stepSize"])
        tick_size = _to_decimal(filters["tickSize"])
        min_qty = _to_decimal(filters["minQty"])
        min_notional = _to_decimal(filters["minNotional"])

        raw_qty = risk_usd / risk_per_unit
        rounded_qty = _round_down_to_increment(raw_qty, step_size)
        if rounded_qty <= 0:
            raise RuntimeError("Refusing execute because rounded quantity is zero.")

        max_qty_for_cap = _round_down_to_increment(safe_cap / current_price, step_size)
        if max_qty_for_cap <= 0:
            decision_reason = "max_notional_cap_below_minimum_executable_qty"
            raise RuntimeError("Refusing execute because max notional cap produces zero executable quantity.")

        capped_qty = min(rounded_qty, max_qty_for_cap)
        if capped_qty <= 0:
            raise RuntimeError("Refusing execute because capped quantity is zero.")

        notional = capped_qty * current_price
        rounded_tp = _round_down_to_increment(tp, tick_size)
        rounded_sl = _round_down_to_increment(sl, tick_size)

        print(f"trade_id={trade['id']}")
        print(f"symbol={symbol}")
        print(f"direction={trade['direction']}")
        print(f"strategy={trade['strategy']}")
        print(f"current_price={_decimal_to_str(current_price)}")
        print(f"vortex_entry={trade['entry']}")
        print(f"vortex_sl={trade['sl']}")
        print(f"vortex_tp={trade['tp']}")
        print(f"risk_usd={_decimal_to_str(risk_usd)}")
        print(f"risk_per_unit={_decimal_to_str(risk_per_unit)}")
        print(f"raw_qty={_decimal_to_str(raw_qty)}")
        print(f"rounded_qty={_decimal_to_str(rounded_qty)}")
        print(f"capped_qty={_decimal_to_str(capped_qty)}")
        print(f"notional={_decimal_to_str(notional)}")
        print(f"requested_max_notional_usdt={_decimal_to_str(requested_cap)}")
        print(f"hard_max_notional_usdt={_decimal_to_str(MAX_STAGE3A_NOTIONAL_USDT)}")
        print(f"computed_notional={_decimal_to_str(notional)}")
        print(f"max_notional_usdt={_decimal_to_str(safe_cap)}")

        if capped_qty < min_qty:
            decision_reason = "rounded_qty_below_min_qty"
            raise RuntimeError(
                f"Refusing execute because rounded quantity {capped_qty} is below minQty {min_qty}."
            )
        if notional < min_notional:
            decision_reason = "notional_below_min_notional"
            raise RuntimeError(
                f"Refusing execute because notional {notional} is below minNotional {min_notional}."
            )
        if notional > safe_cap:
            decision_reason = "notional_above_stage3a_cap"
            raise RuntimeError("Refusing execute because reason=notional_above_stage3a_cap.")

        decision_reason = "approved_capped_to_stage3a_notional" if capped_qty < rounded_qty else "approved"
        entry_order = adapter.place_market_order(
            symbol=symbol,
            side=entry_side,
            quantity=_decimal_to_str(capped_qty),
            reduce_only=False,
        )
        cleanup_required = True
        tp_order = adapter.place_take_profit_market_order(
            symbol=symbol,
            side=exit_side,
            stop_price=_decimal_to_str(rounded_tp),
            quantity=_decimal_to_str(capped_qty),
            reduce_only=True,
        )
        sl_order = adapter.place_stop_market_order(
            symbol=symbol,
            side=exit_side,
            stop_price=_decimal_to_str(rounded_sl),
            quantity=_decimal_to_str(capped_qty),
            reduce_only=True,
        )
        logged_status = "executed"

    except Exception as exc:
        if decision_reason == "not_evaluated":
            decision_reason = "rejected_before_notional_gate"
        print(f"error={exc}")
        logged_status = "refused" if entry_order is None else "entry_cleanup_required"
        return_code = 1
    else:
        return_code = 0
    finally:
        cleanup_error: str | None = None
        if cleanup_required and (tp_order is None or sl_order is None):
            try:
                cleanup_result = _cleanup_symbol(adapter, symbol)
                final_position_amt = cleanup_result["final_position_amt"]
                open_orders_count = cleanup_result["final_open_orders_count"]
                logged_status = "cleanup_after_partial_failure"
            except Exception as exc:
                cleanup_error = str(exc)
                logged_status = "cleanup_failed"

        if tp_order is not None and sl_order is not None:
            try:
                final_position_payload = adapter.get_position_risk(symbol)
                final_position_amt = _position_amt_from_payload(final_position_payload, symbol)
                latest_open_orders = adapter.get_open_orders(symbol)
                latest_open_algo_orders = adapter.get_open_algo_orders(symbol)
                open_orders_count = len(latest_open_orders or []) + len(latest_open_algo_orders or [])
            except Exception as exc:
                cleanup_error = str(exc)
                return_code = 1

        print(f"entry_order_id={_order_identifier(entry_order)}")
        print(f"entry_order_status={_order_status(entry_order)}")
        print(f"tp_order_id={_order_identifier(tp_order)}")
        print(f"tp_order_status={_order_status(tp_order)}")
        print(f"sl_order_id={_order_identifier(sl_order)}")
        print(f"sl_order_status={_order_status(sl_order)}")
        print(f"final_position_amount={_decimal_to_str(final_position_amt)}")
        print(f"open_orders_count={open_orders_count}")
        print(f"requested_max_notional_usdt={_decimal_to_str(requested_cap)}")
        print(f"hard_max_notional_usdt={_decimal_to_str(MAX_STAGE3A_NOTIONAL_USDT)}")
        print(f"computed_notional={_decimal_to_str(notional)}")
        print(f"decision_reason={decision_reason}")
        print(f"order_calls_attempted={adapter.order_calls_attempted}")
        print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
        if cleanup_result is not None:
            print("cleanup_result=" + json.dumps(
                {
                    "cancelled_order_ids": cleanup_result["cancelled_order_ids"],
                    "cancelled_algo_order_ids": cleanup_result["cancelled_algo_order_ids"],
                    "close_order_id": cleanup_result["close_order"]["orderId"] if cleanup_result["close_order"] else None,
                    "final_open_orders_count": cleanup_result["final_open_orders_count"],
                    "final_position_amt": _decimal_to_str(cleanup_result["final_position_amt"]),
                },
                sort_keys=True,
            ))
        if cleanup_error is not None:
            print(f"cleanup_error={cleanup_error}")
            return_code = 1

        if trade is not None and entry_order is not None:
            _append_bridge_log(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "vortex_trade_id": trade["id"],
                    "symbol": symbol,
                    "direction": trade["direction"],
                    "strategy": trade["strategy"],
                    "current_price_at_execution": float(current_price),
                    "vortex_entry": float(trade["entry"]),
                    "vortex_sl": float(trade["sl"]),
                    "vortex_tp": float(trade["tp"]),
                    "risk_usd": float(risk_usd),
                    "quantity": float(capped_qty),
                    "notional": float(notional),
                    "entry_order_id": _order_identifier(entry_order),
                    "tp_order_id": _order_identifier(tp_order) if tp_order else None,
                    "sl_order_id": _order_identifier(sl_order) if sl_order else None,
                    "status": logged_status,
                }
            )

    return return_code


def _cleanup_mode(args: argparse.Namespace) -> int:
    adapter = BinanceDemoAdapter()
    print("Manual Vortex Demo Bridge Cleanup")
    print("---------------------------------")
    print("strategy_runtime_connected=false")

    try:
        _validate_runtime_guard(adapter, args.confirm_demo_cleanup, "cleanup")
        bridge_log_entry = _load_bridge_log_entry(args.trade_id)
        trade = _fetch_trade(args.trade_id)

        if bridge_log_entry is not None:
            symbol = str(bridge_log_entry["symbol"]).upper()
        elif trade is not None:
            symbol = str(trade["pair"]).upper()
        else:
            raise RuntimeError(f"Refusing cleanup because trade {args.trade_id} does not exist and no bridge log entry exists.")

        cleanup_result = _cleanup_symbol(adapter, symbol)
        print(f"trade_id={args.trade_id}")
        print(f"symbol={symbol}")
        print("cancelled_order_ids=" + json.dumps(cleanup_result["cancelled_order_ids"]))
        print("cancelled_algo_order_ids=" + json.dumps(cleanup_result["cancelled_algo_order_ids"]))
        print(
            f"close_order_id="
            f"{cleanup_result['close_order'].get('orderId') if cleanup_result['close_order'] else '<not placed>'}"
        )
        print(
            f"close_order_status="
            f"{cleanup_result['close_order'].get('status') if cleanup_result['close_order'] else '<not placed>'}"
        )
        print(f"final_position_amount={_decimal_to_str(cleanup_result['final_position_amt'])}")
        print(f"open_orders_count={cleanup_result['final_open_orders_count']}")
        print(f"order_calls_attempted={adapter.order_calls_attempted}")
        print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
        return 0
    except Exception as exc:
        print(f"error={exc}")
        print(f"order_calls_attempted={adapter.order_calls_attempted}")
        print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
        return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual bridge from an OPEN Vortex dry-run trade to Binance demo.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Read-only status for a Vortex trade and Binance demo symbol.")
    status_parser.add_argument("--trade-id", type=int, required=True)

    execute_parser = subparsers.add_parser("execute", help="Manually bridge a Vortex OPEN trade to Binance demo.")
    execute_parser.add_argument("--trade-id", type=int, required=True)
    execute_parser.add_argument(
        "--max-notional-usdt",
        type=Decimal,
        default=DEFAULT_MAX_NOTIONAL_USDT,
    )
    execute_parser.add_argument("--confirm-demo-execution", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup", help="Cancel bridge orders and flatten demo position for a trade.")
    cleanup_parser.add_argument("--trade-id", type=int, required=True)
    cleanup_parser.add_argument("--confirm-demo-cleanup", action="store_true")

    return parser.parse_args()


def main() -> int:
    load_dotenv_file()
    args = parse_args()

    if args.command == "status":
        return _status_mode(args)
    if args.command == "execute":
        return _execute_mode(args)
    if args.command == "cleanup":
        return _cleanup_mode(args)
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
