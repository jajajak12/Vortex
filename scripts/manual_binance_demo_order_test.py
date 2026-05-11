from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from exchange.binance_demo import BinanceDemoAdapter, load_dotenv_file


ALLOWED_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}
ALLOWED_SIDES = {"BUY", "SELL"}
MAX_NOTIONAL_USDT = Decimal("50")
ZERO_POSITION_EPSILON = Decimal("0.00000001")


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _decimal_to_str(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _round_qty_to_step(qty: Decimal, step_size: str) -> Decimal:
    step = _to_decimal(step_size)
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _redacted_key_prefix(api_key: str) -> str:
    return f"{api_key[:4]}***" if api_key else "<missing>"


def _find_position_row(position_payload: Any, symbol: str) -> dict[str, Any] | None:
    if isinstance(position_payload, dict):
        position_payload = [position_payload]
    for item in position_payload or []:
        if item.get("symbol") == symbol:
            return item
    return None


def _position_amt_from_payload(position_payload: Any, symbol: str) -> Decimal:
    row = _find_position_row(position_payload, symbol)
    if not row:
        return Decimal("0")
    return _to_decimal(row.get("positionAmt", "0"))


def _validate_cli_args(args: argparse.Namespace) -> None:
    if args.symbol not in ALLOWED_SYMBOLS:
        raise RuntimeError(f"Unsupported symbol for manual demo order test: {args.symbol}")
    if args.side not in ALLOWED_SIDES:
        raise RuntimeError(f"Unsupported side for manual demo order test: {args.side}")
    if not args.confirm_demo_order:
        raise RuntimeError("Refusing manual demo order test because --confirm-demo-order is required.")
    if args.notional_usdt <= 0:
        raise RuntimeError("Refusing manual demo order test because --notional-usdt must be positive.")
    if args.notional_usdt > MAX_NOTIONAL_USDT:
        raise RuntimeError(
            f"Refusing manual demo order test because --notional-usdt exceeds {MAX_NOTIONAL_USDT} USDT."
        )


def _validate_runtime_guard(adapter: BinanceDemoAdapter) -> None:
    if not adapter.demo_mode:
        raise RuntimeError("Refusing manual demo order test because BINANCE_DEMO_MODE is not true.")
    if not adapter.execution_enabled:
        raise RuntimeError("Refusing manual demo order test because BINANCE_EXECUTION_ENABLED is not true.")
    if adapter._is_mainnet_like_host():
        raise RuntimeError(
            f"Refusing manual demo order test because base URL '{adapter.base_url}' looks like mainnet."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manual one-off Binance Futures demo order test.")
    parser.add_argument("--symbol", required=True, choices=sorted(ALLOWED_SYMBOLS))
    parser.add_argument("--side", required=True, choices=sorted(ALLOWED_SIDES))
    parser.add_argument("--notional-usdt", dest="notional_usdt", required=True, type=Decimal)
    parser.add_argument("--confirm-demo-order", action="store_true")
    return parser.parse_args()


def main() -> int:
    load_dotenv_file()
    args = parse_args()
    adapter = BinanceDemoAdapter()

    entry_order: dict[str, Any] | None = None
    close_order: dict[str, Any] | None = None
    entry_order_status = "<not placed>"
    close_order_status = "<not placed>"
    final_position_amt = "0"
    open_orders_count = -1
    cleanup_ok = False
    cleanup_errors: list[str] = []
    post_entry_position_amt = Decimal("0")
    close_side = "SELL" if args.side == "BUY" else "BUY"
    cleanup_required = False

    print("Binance Manual Demo Order Test")
    print("------------------------------")
    print("strategy_runtime_connected=false")
    print(f"demo_mode={_fmt_bool(adapter.demo_mode)}")
    print(f"execution_enabled={_fmt_bool(adapter.execution_enabled)}")
    print(f"base_url={adapter.base_url}")
    print(f"symbol={args.symbol}")
    print(f"side={args.side}")
    print(f"requested_notional_usdt={args.notional_usdt}")
    print(f"key_present={bool(adapter.api_key)}")
    print(f"key_prefix={_redacted_key_prefix(adapter.api_key)}")

    try:
        _validate_cli_args(args)
        _validate_runtime_guard(adapter)

        existing_position_payload = adapter.get_position_risk(args.symbol)
        existing_position_amt = _position_amt_from_payload(existing_position_payload, args.symbol)
        if abs(existing_position_amt) > ZERO_POSITION_EPSILON:
            raise RuntimeError(
                f"Refusing manual demo order test because symbol {args.symbol} already has an open position "
                f"({existing_position_amt}). Cleanup manually before retrying."
            )

        ticker_payload = adapter.ticker_price(args.symbol)
        current_price = _to_decimal(ticker_payload["price"])
        filters = adapter.get_symbol_filters(args.symbol)
        step_size = str(filters["stepSize"])
        min_qty = _to_decimal(filters["minQty"])
        min_notional = _to_decimal(filters["minNotional"])
        raw_qty = args.notional_usdt / current_price
        rounded_qty = _round_qty_to_step(raw_qty, step_size)
        actual_notional = rounded_qty * current_price

        print(f"current_price={current_price}")
        print(f"step_size={step_size}")
        print(f"min_qty={min_qty}")
        print(f"min_notional={min_notional}")
        print(f"raw_qty={raw_qty}")
        print(f"rounded_qty={rounded_qty}")
        print(f"actual_notional={actual_notional}")

        if rounded_qty < min_qty:
            raise RuntimeError(
                f"Refusing manual demo order test because rounded quantity {rounded_qty} is below minQty {min_qty}."
            )
        if actual_notional < min_notional:
            next_step_qty = rounded_qty + _to_decimal(step_size)
            next_step_notional = next_step_qty * current_price
            raise RuntimeError(
                f"Refusing manual demo order test because notional {actual_notional} is below minNotional {min_notional}. "
                f"At current price {current_price}, rounded quantity {rounded_qty} is too small, while the next valid "
                f"step {next_step_qty} would be notional {next_step_notional}, which exceeds the configured "
                f"{MAX_NOTIONAL_USDT} USDT cap."
            )

        entry_order = adapter.place_market_order(
            symbol=args.symbol,
            side=args.side,
            quantity=_decimal_to_str(rounded_qty),
            reduce_only=False,
        )
        cleanup_required = True
        entry_order_status = str(entry_order.get("status", "<missing>"))
        print(f"entry_order_id={entry_order.get('orderId', '<missing>')}")
        print(f"entry_order_status={entry_order_status}")

        post_entry_position_payload = adapter.get_position_risk(args.symbol)
        post_entry_position_amt = _position_amt_from_payload(post_entry_position_payload, args.symbol)
        print(f"post_entry_position_amt={post_entry_position_amt}")

    except Exception as exc:
        print(f"error={exc}")
        if entry_order is None:
            print(f"order_calls_attempted={adapter.order_calls_attempted}")
            print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
            print(f"entry_order_status={entry_order_status}")
            print(f"close_order_status={close_order_status}")
            print(f"final_position_amt={final_position_amt}")
            print("open_orders_count=0")
            print("cleanup_ok=false")
            return 1
    finally:
        if cleanup_required:
            try:
                if abs(post_entry_position_amt) <= ZERO_POSITION_EPSILON:
                    latest_position_payload = adapter.get_position_risk(args.symbol)
                    post_entry_position_amt = _position_amt_from_payload(latest_position_payload, args.symbol)
                if abs(post_entry_position_amt) > ZERO_POSITION_EPSILON:
                    close_qty = abs(post_entry_position_amt)
                    close_order = adapter.place_market_order(
                        symbol=args.symbol,
                        side=close_side,
                        quantity=_decimal_to_str(close_qty),
                        reduce_only=True,
                    )
                    close_order_status = str(close_order.get("status", "<missing>"))
            except Exception as close_exc:
                cleanup_errors.append(f"close_position: {close_exc}")

            try:
                final_position_payload = adapter.get_position_risk(args.symbol)
                final_position_amt = _decimal_to_str(_position_amt_from_payload(final_position_payload, args.symbol))
            except Exception as position_exc:
                cleanup_errors.append(f"final_position: {position_exc}")

            try:
                open_orders = adapter.get_open_orders(args.symbol)
                open_orders_count = len(open_orders or [])
                for order in open_orders or []:
                    order_id = order.get("orderId")
                    if order_id is None:
                        continue
                    adapter.cancel_order(args.symbol, order_id)
                if open_orders_count > 0:
                    open_orders = adapter.get_open_orders(args.symbol)
                    open_orders_count = len(open_orders or [])
            except Exception as open_orders_exc:
                cleanup_errors.append(f"open_orders: {open_orders_exc}")

            cleanup_ok = (
                not cleanup_errors
                and abs(_to_decimal(final_position_amt)) <= ZERO_POSITION_EPSILON
                and open_orders_count == 0
            )

    if close_order is not None:
        print(f"close_order_id={close_order.get('orderId', '<missing>')}")
        print(f"close_order_status={close_order_status}")
    else:
        print("close_order_id=<not placed>")
        print(f"close_order_status={close_order_status}")

    print(f"final_position_amt={final_position_amt}")
    print(f"open_orders_count={open_orders_count}")
    print(f"cleanup_ok={_fmt_bool(cleanup_ok)}")
    print(f"order_calls_attempted={adapter.order_calls_attempted}")
    print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
    if cleanup_errors:
        print("cleanup_errors:")
        for item in cleanup_errors:
            print(f"- {item}")

    return 0 if entry_order is not None and close_order is not None and cleanup_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
