"""
strategy6_donchian_breakout.py — S6: donchian_breakout LONG 50-period

Detection: 4H close breaks above prior 50-candle Donchian high.
Trade: RR 1:2.
"""

from strategy_utils import calculate_atr, get_candles
from config import ATR_PERIOD

TF_DETECT = "4h"
PERIOD = 50
VOL_LOOKBACK = 20
VOL_MULT = 1.1
SL_ATR_MULT = 1.0
RR = 2.0


def _avg_vol(candles: list[dict], lookback: int = VOL_LOOKBACK) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def scan_donchian_breakout(pair: str) -> list[dict]:
    candles = get_candles(pair, TF_DETECT, limit=PERIOD + ATR_PERIOD + 5)
    if len(candles) < PERIOD + 2:
        return []

    cur = candles[-1]
    channel = candles[-(PERIOD + 1):-1]
    donchian_high = max(c["high"] for c in channel)
    if cur["close"] <= donchian_high:
        return []

    vol_avg = _avg_vol(candles)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0
    if vol_ratio < VOL_MULT:
        return []

    atr = calculate_atr(candles, ATR_PERIOD)
    entry = cur["close"]
    sl = max(donchian_high - atr * SL_ATR_MULT, 0.0)
    risk = entry - sl
    if risk <= 0:
        return []
    tp = entry + risk * RR
    breakout_pct = (entry - donchian_high) / donchian_high if donchian_high > 0 else 0.0
    score = min(10.0, 7.0 + min(vol_ratio / VOL_MULT - 1, 1.0) * 1.0 + min(breakout_pct / 0.01, 1.0) * 2.0)

    return [{
        "pair": pair,
        "tf": TF_DETECT,
        "tf_label": "4H",
        "direction": "LONG",
        "type": "DonchianBreakout50",
        "current_price": cur["close"],
        "in_zone": True,
        "atr": atr,
        "donchian_high": round(donchian_high, 6),
        "breakout_pct": round(breakout_pct * 100, 3),
        "vol_ratio": round(vol_ratio, 2),
        "zone_key": f"S6_{pair}_LONG_{round(donchian_high, 2)}",
        "confidence_score": round(score, 2),
        "confidence_label": "HIGH" if score >= 8.5 else "MEDIUM",
        "trade": {
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp, 6),
            "tp2": round(tp, 6),
        },
    }]
