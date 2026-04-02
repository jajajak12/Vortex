import json
import os
import time
from datetime import datetime

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")


def _load():
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE) as f:
        return json.load(f)


def _save(trades):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def log_signal(pair: str, direction: str, entry: float, sl: float, tp: float) -> dict:
    """Catat signal entry baru dengan status OPEN."""
    trades = _load()
    trade = {
        "id":          int(time.time() * 1000),
        "time":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pair":        pair,
        "direction":   direction,
        "entry":       entry,
        "sl":          sl,
        "tp":          tp,
        "status":      "OPEN",
        "result":      None,
        "close_price": None,
        "close_time":  None,
    }
    trades.append(trade)
    _save(trades)
    return trade


def update_trades_for_pair(pair: str, current_price: float) -> list:
    """
    Cek trade OPEN untuk pair ini.
    Tandai WIN jika hit TP, LOSS jika hit SL.
    Return list trade yang baru ditutup.
    """
    trades = _load()
    closed = []

    for t in trades:
        if t["status"] != "OPEN" or t["pair"] != pair:
            continue

        result = None
        if t["direction"] == "LONG":
            if current_price >= t["tp"]:
                result = "WIN"
            elif current_price <= t["sl"]:
                result = "LOSS"
        else:  # SHORT
            if current_price <= t["tp"]:
                result = "WIN"
            elif current_price >= t["sl"]:
                result = "LOSS"

        if result:
            t["status"]      = "CLOSED"
            t["result"]      = result
            t["close_price"] = current_price
            t["close_time"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            closed.append(t)

    if closed:
        _save(trades)

    return closed


def get_stats() -> dict:
    trades = _load()
    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_  = [t for t in trades if t["status"] == "OPEN"]

    if not closed:
        return {"total": 0, "wins": 0, "losses": 0, "winrate": 0.0, "open": len(open_)}

    wins   = sum(1 for t in closed if t["result"] == "WIN")
    losses = sum(1 for t in closed if t["result"] == "LOSS")
    return {
        "total":   len(closed),
        "wins":    wins,
        "losses":  losses,
        "winrate": round(wins / len(closed) * 100, 1),
        "open":    len(open_),
    }
