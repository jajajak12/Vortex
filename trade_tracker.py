import json
import os
import time
from datetime import datetime

from vortex_logger import get_logger

log = get_logger(__name__)

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")

# ── In-memory cache — hindari 13× disk read per scan cycle ───
_cache: list | None = None


def _load() -> list:
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.exists(TRADES_FILE):
        _cache = []
        return _cache
    with open(TRADES_FILE) as f:
        _cache = json.load(f)
    return _cache


def _save(trades: list):
    global _cache
    _cache = trades
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def log_signal(pair: str, direction: str, entry: float, sl: float, tp: float,
               confluence_score: int = 0, regime_state: str = "UNKNOWN",
               strategy: str = "S1", position_usdt: float = 0.0,
               rr: float = 0.0) -> dict:
    """Catat signal entry baru dengan status OPEN."""
    trades = _load()
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
        # Metadata untuk postmortem
        "strategy":           strategy,
        "confluence_score":   confluence_score,
        "regime_state":       regime_state,
        "position_usdt":      position_usdt,
        "rr":                 rr,
        "candles_to_resolve": None,  # diisi saat close
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
            open_time  = datetime.strptime(t["time"], "%Y-%m-%d %H:%M:%S")
            close_time = datetime.now()
            minutes_open = int((close_time - open_time).total_seconds() / 60)

            t["status"]           = "CLOSED"
            t["result"]           = result
            t["close_price"]      = current_price
            t["close_time"]       = close_time.strftime("%Y-%m-%d %H:%M:%S")
            t["candles_to_resolve"] = minutes_open // 5  # dalam 5m candles
            closed.append(t)

    if closed:
        _save(trades)

    return closed


def get_stats() -> dict:
    trades = _load()
    closed = [t for t in trades if t["status"] == "CLOSED"]
    open_  = [t for t in trades if t["status"] == "OPEN"]

    if not closed:
        return {
            "total": 0, "wins": 0, "losses": 0, "winrate": 0.0,
            "open": len(open_), "by_strategy": {},
        }

    wins   = sum(1 for t in closed if t["result"] == "WIN")
    losses = sum(1 for t in closed if t["result"] == "LOSS")

    # Breakdown per strategi
    by_strategy: dict[str, dict] = {}
    for t in closed:
        s = t.get("strategy", "?")
        if s not in by_strategy:
            by_strategy[s] = {"wins": 0, "losses": 0, "total": 0, "winrate": 0.0}
        by_strategy[s]["total"] += 1
        if t["result"] == "WIN":
            by_strategy[s]["wins"] += 1
        else:
            by_strategy[s]["losses"] += 1
    for s, d in by_strategy.items():
        d["winrate"] = round(d["wins"] / d["total"] * 100, 1) if d["total"] > 0 else 0.0

    return {
        "total":       len(closed),
        "wins":        wins,
        "losses":      losses,
        "winrate":     round(wins / len(closed) * 100, 1),
        "open":        len(open_),
        "by_strategy": by_strategy,
    }


def trim_old_trades(keep_closed: int = 500):
    """
    Fix 6: Bersihkan trades.json agar tidak tumbuh tak terbatas.
    Simpan semua OPEN + max keep_closed trades CLOSED terbaru.
    """
    trades = _load()
    open_  = [t for t in trades if t["status"] == "OPEN"]
    closed = [t for t in trades if t["status"] == "CLOSED"]

    if len(closed) <= keep_closed:
        return  # belum perlu trim

    closed_trimmed = closed[-keep_closed:]
    _save(open_ + closed_trimmed)
    log.info(f"[TRIM] trades.json: {len(closed)} → {len(closed_trimmed)} closed trades")
