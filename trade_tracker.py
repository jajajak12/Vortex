"""
trade_tracker.py — P2.1: Rewritten to use SQLite via db.py.
Interface identical to old JSON version — scanner.py needs no changes.
"""

import time
from datetime import datetime

# Entry TF per strategy — used for candles_to_resolve granularity
_STRATEGY_TF_MINUTES: dict[str, int] = {
    "S1": 5, "S1-CHART": 30, "S2": 5, "S3": 30,
    "S4-RETEST": 30, "S4-MOMENTUM": 30, "S5": 30, "S6": 30,
}

import db
from vortex_logger import get_logger

log = get_logger(__name__)

# Init DB + migrate from trades.json on first import
db.init_db()


def log_signal(pair: str, direction: str, entry: float, sl: float, tp: float,
               confluence_score: int = 0, regime_state: str = "UNKNOWN",
               strategy: str = "S1", position_usdt: float = 0.0,
               rr: float = 0.0) -> dict:
    """Catat signal entry baru dengan status OPEN."""
    trade = {
        "id":               int(time.time() * 1000),
        "time":             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pair":             pair,
        "direction":        direction,
        "entry":            entry,
        "sl":               sl,
        "tp":               tp,
        "status":           "OPEN",
        "result":           None,
        "close_price":      None,
        "close_time":       None,
        "strategy":         strategy,
        "confluence_score": confluence_score,
        "regime_state":     regime_state,
        "position_usdt":    position_usdt,
        "rr":               rr,
        "candles_to_resolve": None,
    }
    db.insert_trade(trade)
    return trade


def update_trades_for_pair(pair: str, current_price: float,
                            candle_high: float = 0.0, candle_low: float = 0.0) -> list[dict]:
    """
    Cek trade OPEN untuk pair ini.
    Tandai WIN jika hit TP, LOSS jika hit SL.
    Gunakan high/low candle untuk cek wick-driven closes.
    Return list trade yang baru ditutup.
    """
    open_trades = db.get_open_trades(pair)
    closed = []

    check_high = candle_high if candle_high > 0 else current_price
    check_low  = candle_low  if candle_low  > 0 else current_price

    for t in open_trades:
        result      = None
        close_price = current_price
        if t["direction"] == "LONG":
            if check_high >= t["tp"]:
                result      = "WIN"
                close_price = t["tp"]
            elif check_low <= t["sl"]:
                result      = "LOSS"
                close_price = t["sl"]
        else:  # SHORT
            if check_low <= t["tp"]:
                result      = "WIN"
                close_price = t["tp"]
            elif check_high >= t["sl"]:
                result      = "LOSS"
                close_price = t["sl"]

        if result:
            open_time    = datetime.strptime(t["time"], "%Y-%m-%d %H:%M:%S")
            close_dt     = datetime.now()
            minutes_open = int((close_dt - open_time).total_seconds() / 60)
            tf_min       = _STRATEGY_TF_MINUTES.get(t.get("strategy", "S1"), 5)
            candles_res  = max(1, minutes_open // tf_min)

            close_time_str = close_dt.strftime("%Y-%m-%d %H:%M:%S")
            db.close_trade(t["id"], result, close_price, close_time_str, candles_res)

            closed_trade = dict(t)
            closed_trade.update({
                "status":             "CLOSED",
                "result":             result,
                "close_price":        close_price,
                "close_time":         close_time_str,
                "candles_to_resolve": candles_res,
            })
            closed.append(closed_trade)

    return closed


def get_stats() -> dict:
    return db.get_stats()


def trim_old_trades(keep_closed: int = 500):
    """No-op — SQLite tidak perlu trim. Data disimpan semua dengan index."""
    pass


def reset_all_trades() -> int:
    """Delete all trades from trades.db. Use for paper-trading resets."""
    return db.reset_all_trades()
