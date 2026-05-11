"""
strategy10_vol_break_long.py — S10: Vol_Break LONG
"""

from __future__ import annotations

from strategy_utils import get_candles

TF_DETECT = "4h"
BREAKOUT_PERIOD = 20
VOL_LOOKBACK = 20
RR = 3.0
MIN_RR = 2.99


def _avg_volume_ex_current(candles: list[dict], lookback: int) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def scan_vol_break_long(pair: str) -> list[dict]:
    candles = get_candles(pair, TF_DETECT, limit=BREAKOUT_PERIOD + VOL_LOOKBACK + 10)
    if len(candles) < BREAKOUT_PERIOD + 2:
        return []

    cur = candles[-1]
    prior_high_20 = max(c["high"] for c in candles[-(BREAKOUT_PERIOD + 1):-1])
    if cur["close"] <= prior_high_20:
        return []

    vol_avg = _avg_volume_ex_current(candles, VOL_LOOKBACK)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0
    if vol_ratio <= 2.0:
        return []

    entry = cur["close"]
    sl = cur["low"]
    risk = entry - sl
    if risk <= 0:
        return []
    tp = entry + RR * risk
    breakout_pct = (entry - prior_high_20) / prior_high_20 if prior_high_20 > 0 else 0.0
    score = min(10.0, 7.1 + min(vol_ratio - 2.0, 1.0) * 1.2 + min(breakout_pct / 0.01, 1.0) * 1.0)

    return [{
        "pair": pair,
        "tf": TF_DETECT,
        "tf_label": "4H",
        "direction": "LONG",
        "type": "VolBreakLong",
        "current_price": entry,
        "in_zone": True,
        "prior_high_20": round(prior_high_20, 6),
        "vol_ratio": round(vol_ratio, 2),
        "planned_rr": RR,
        "min_rr": MIN_RR,
        "zone_key": f"S10_{pair}_LONG_{round(prior_high_20, 2)}",
        "confidence_score": round(score, 2),
        "trade": {
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp, 6),
            "tp2": round(tp, 6),
        },
    }]
