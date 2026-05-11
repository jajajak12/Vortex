from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import json
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db
from config import DRY_RUN_STARTING_EQUITY_USD, MAX_RISK_USD, RISK_EQUITY_PCT
from exchange.binance_demo import BinanceDemoAdapter, load_dotenv_file


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _to_decimal(value: float | str) -> Decimal:
    return Decimal(str(value))


def _decimal_to_str(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def round_price_to_tick(price: float, tick_size: str) -> float:
    tick = _to_decimal(tick_size)
    rounded = (_to_decimal(price) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    return float(rounded)


def round_qty_to_step(qty: float, step_size: str) -> float:
    step = _to_decimal(step_size)
    rounded = (_to_decimal(qty) / step).to_integral_value(rounding=ROUND_DOWN) * step
    return float(rounded)


def validate_min_qty(qty: float, min_qty: str) -> bool:
    return _to_decimal(qty) >= _to_decimal(min_qty)


def validate_min_notional(qty: float, price: float, min_notional: str) -> bool:
    return _to_decimal(qty) * _to_decimal(price) >= _to_decimal(min_notional)


@dataclass(frozen=True)
class SampleSetup:
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float


SAMPLE_SETUPS = [
    SampleSetup(symbol="BTCUSDT", direction="LONG", entry=100000, sl=98000, tp=106000),
    SampleSetup(symbol="ETHUSDT", direction="SHORT", entry=3500, sl=3600, tp=3200),
    SampleSetup(symbol="BNBUSDT", direction="LONG", entry=700, sl=680, tp=760),
    SampleSetup(symbol="SOLUSDT", direction="LONG", entry=160, sl=150, tp=190),
]


def build_payloads(symbol: str, direction: str, qty: Decimal, rounded_tp: Decimal, rounded_sl: Decimal) -> dict[str, dict[str, str | bool]]:
    entry_side = "BUY" if direction == "LONG" else "SELL"
    close_side = "SELL" if direction == "LONG" else "BUY"
    qty_str = _decimal_to_str(qty)
    return {
        "entry": {
            "symbol": symbol,
            "side": entry_side,
            "type": "MARKET",
            "quantity": qty_str,
            "reduceOnly": False,
        },
        "take_profit": {
            "symbol": symbol,
            "side": close_side,
            "type": "TAKE_PROFIT_MARKET",
            "stopPrice": _decimal_to_str(rounded_tp),
            "quantity": qty_str,
            "reduceOnly": True,
            "closePosition": False,
        },
        "stop_loss": {
            "symbol": symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": _decimal_to_str(rounded_sl),
            "quantity": qty_str,
            "reduceOnly": True,
            "closePosition": False,
        },
    }


def simulate_setup(adapter: BinanceDemoAdapter, setup: SampleSetup, current_equity: float, risk_usd: float) -> dict[str, object]:
    filters = adapter.get_symbol_filters(setup.symbol)
    tick_size = str(filters["tickSize"])
    step_size = str(filters["stepSize"])
    min_qty = str(filters["minQty"])
    min_notional = str(filters["minNotional"])

    entry_dec = _to_decimal(setup.entry)
    sl_dec = _to_decimal(setup.sl)
    tp_dec = _to_decimal(setup.tp)

    if setup.direction == "LONG":
        risk_per_unit = entry_dec - sl_dec
    else:
        risk_per_unit = sl_dec - entry_dec

    result: dict[str, object] = {
        "symbol": setup.symbol,
        "direction": setup.direction,
        "risk_usd": risk_usd,
        "filters": {
            "tickSize": tick_size,
            "stepSize": step_size,
            "minQty": min_qty,
            "minNotional": min_notional,
        },
        "prices_raw": {
            "entry": setup.entry,
            "sl": setup.sl,
            "tp": setup.tp,
        },
        "prices_rounded": {
            "entry": round_price_to_tick(setup.entry, tick_size),
            "sl": round_price_to_tick(setup.sl, tick_size),
            "tp": round_price_to_tick(setup.tp, tick_size),
        },
    }

    if risk_per_unit <= 0:
        result.update(
            approved=False,
            reject_reason="invalid_stop_distance",
            raw_qty=0.0,
            rounded_qty=0.0,
            notional=0.0,
            validations={
                "min_qty_ok": False,
                "min_notional_ok": False,
            },
            payloads=None,
        )
        return result

    raw_qty = _to_decimal(risk_usd) / risk_per_unit
    rounded_qty = _to_decimal(round_qty_to_step(float(raw_qty), step_size))
    notional = rounded_qty * entry_dec
    rounded_tp = _to_decimal(round_price_to_tick(setup.tp, tick_size))
    rounded_sl = _to_decimal(round_price_to_tick(setup.sl, tick_size))

    min_qty_ok = validate_min_qty(float(rounded_qty), min_qty)
    min_notional_ok = validate_min_notional(float(rounded_qty), setup.entry, min_notional)

    result.update(
        raw_qty=float(raw_qty),
        rounded_qty=float(rounded_qty),
        notional=float(notional),
        validations={
            "min_qty_ok": min_qty_ok,
            "min_notional_ok": min_notional_ok,
        },
    )

    if not min_qty_ok:
        result.update(approved=False, reject_reason="quantity_below_min_qty", payloads=None)
        return result

    if not min_notional_ok:
        result.update(approved=False, reject_reason="notional_below_min_notional", payloads=None)
        return result

    result.update(
        approved=True,
        reject_reason=None,
        payloads=build_payloads(setup.symbol, setup.direction, rounded_qty, rounded_tp, rounded_sl),
    )
    return result


def main() -> int:
    load_dotenv_file()
    adapter = BinanceDemoAdapter()
    current_equity = round(DRY_RUN_STARTING_EQUITY_USD + db.compute_realized_pnl(), 4)
    risk_usd = round(min(current_equity * RISK_EQUITY_PCT, MAX_RISK_USD), 4)

    print("Binance Demo Order Payload Simulator")
    print("------------------------------------")
    print(f"demo_mode={_fmt_bool(adapter.demo_mode)}")
    print(f"execution_enabled={_fmt_bool(adapter.execution_enabled)}")
    print(f"current_equity={current_equity}")
    print(f"risk_usd={risk_usd}")

    for setup in SAMPLE_SETUPS:
        simulation = simulate_setup(adapter, setup, current_equity, risk_usd)
        print(f"symbol={simulation['symbol']}")
        print(f"direction={simulation['direction']}")
        print(
            f"entry_raw={simulation['prices_raw']['entry']} "
            f"sl_raw={simulation['prices_raw']['sl']} tp_raw={simulation['prices_raw']['tp']}"
        )
        print(
            f"entry_rounded={simulation['prices_rounded']['entry']} "
            f"sl_rounded={simulation['prices_rounded']['sl']} tp_rounded={simulation['prices_rounded']['tp']}"
        )
        print(f"raw_qty={simulation['raw_qty']}")
        print(f"rounded_qty={simulation['rounded_qty']}")
        print(f"notional={simulation['notional']}")
        print(
            f"minQty={simulation['filters']['minQty']} minQty_ok={_fmt_bool(simulation['validations']['min_qty_ok'])}"
        )
        print(
            f"minNotional={simulation['filters']['minNotional']} "
            f"minNotional_ok={_fmt_bool(simulation['validations']['min_notional_ok'])}"
        )
        print(f"approved={_fmt_bool(bool(simulation['approved']))}")
        if simulation["reject_reason"]:
            print(f"reject_reason={simulation['reject_reason']}")
        else:
            print("payloads=" + json.dumps(simulation["payloads"], sort_keys=True))
        print("---")

    print(f"order_calls_attempted={adapter.order_calls_attempted}")
    print(f"order_endpoints_called={_fmt_bool(adapter.order_endpoints_called)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
