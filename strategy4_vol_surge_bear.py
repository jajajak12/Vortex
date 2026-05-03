"""
strategy4_vol_surge_bear.py — S4: S8 Volume Surge Bear SHORT

Detection: 4H bearish volume surge near a 50-bar high.
Trade: RR 1:2.
"""

from strategy_utils import calculate_atr, get_candles
from config import ATR_PERIOD

TF_DETECT = "4h"
LOOKBACK = 50
VOL_LOOKBACK = 20
VOL_MULT = 1.8
BODY_RATIO_MIN = 0.45
CLOSE_LOW_MAX = 0.35
NEAR_HIGH_PCT = 0.02
SL_BUFFER = 0.003
RR = 2.0


def _avg_vol(candles: list[dict], lookback: int = VOL_LOOKBACK) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def scan_vol_surge_bear(pair: str) -> list[dict]:
    candles = get_candles(pair, TF_DETECT, limit=LOOKBACK + VOL_LOOKBACK + 5)
    if len(candles) < LOOKBACK + 2:
        return []

    cur = candles[-1]
    prev = candles[-(LOOKBACK + 1):-1]
    rng = cur["high"] - cur["low"]
    if rng <= 0:
        return []

    body_ratio = abs(cur["close"] - cur["open"]) / rng
    close_pos = (cur["close"] - cur["low"]) / rng
    vol_avg = _avg_vol(candles)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0
    swing_high = max(c["high"] for c in prev)

    is_bear = cur["close"] < cur["open"]
    near_high = cur["high"] >= swing_high * (1 - NEAR_HIGH_PCT)
    if not (is_bear and near_high):
        return []
    if body_ratio < BODY_RATIO_MIN or close_pos > CLOSE_LOW_MAX or vol_ratio < VOL_MULT:
        return []

    entry = cur["close"]
    sl = cur["high"] * (1 + SL_BUFFER)
    risk = sl - entry
    if risk <= 0:
        return []
    tp = entry - risk * RR
    atr = calculate_atr(candles, ATR_PERIOD)
    score = min(10.0, 7.0 + min(vol_ratio / VOL_MULT - 1, 1.0) * 1.5 + min(body_ratio, 1.0) * 1.5)

    return [{
        "pair": pair,
        "tf": TF_DETECT,
        "tf_label": "4H",
        "direction": "SHORT",
        "type": "VolumeSurgeBear",
        "current_price": cur["close"],
        "in_zone": True,
        "atr": atr,
        "body_ratio": round(body_ratio, 3),
        "vol_ratio": round(vol_ratio, 2),
        "swing_extreme": swing_high,
        "zone_key": f"S4_{pair}_SHORT_{round(swing_high, 2)}",
        "confidence_score": round(score, 2),
        "confidence_label": "HIGH" if score >= 8.5 else "MEDIUM",
        "trade": {
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp, 6),
            "tp2": round(tp, 6),
        },
    }]
