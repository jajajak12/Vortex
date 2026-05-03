"""
strategy5_vol_impulse.py — S5: volume_impulse_bull_close_high LONG

Detection: 4H bullish impulse candle with volume expansion and close near high.
Trade: RR 1:2.
"""

from strategy_utils import calculate_atr, get_candles
from config import ATR_PERIOD

TF_DETECT = "4h"
VOL_LOOKBACK = 20
VOL_MULT = 1.6
BODY_RATIO_MIN = 0.50
CLOSE_HIGH_MIN = 0.75
SL_BUFFER = 0.003
RR = 2.0


def _avg_vol(candles: list[dict], lookback: int = VOL_LOOKBACK) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def scan_vol_impulse(pair: str) -> list[dict]:
    candles = get_candles(pair, TF_DETECT, limit=VOL_LOOKBACK + ATR_PERIOD + 5)
    if len(candles) < VOL_LOOKBACK + 2:
        return []

    cur = candles[-1]
    rng = cur["high"] - cur["low"]
    if rng <= 0:
        return []

    body_ratio = abs(cur["close"] - cur["open"]) / rng
    close_pos = (cur["close"] - cur["low"]) / rng
    vol_avg = _avg_vol(candles)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0

    if cur["close"] <= cur["open"]:
        return []
    if body_ratio < BODY_RATIO_MIN or close_pos < CLOSE_HIGH_MIN or vol_ratio < VOL_MULT:
        return []

    entry = cur["close"]
    sl = cur["low"] * (1 - SL_BUFFER)
    risk = entry - sl
    if risk <= 0:
        return []
    tp = entry + risk * RR
    atr = calculate_atr(candles, ATR_PERIOD)
    score = min(10.0, 7.0 + min(vol_ratio / VOL_MULT - 1, 1.0) * 1.5 + close_pos * 1.5)

    return [{
        "pair": pair,
        "tf": TF_DETECT,
        "tf_label": "4H",
        "direction": "LONG",
        "type": "VolumeImpulseBullCloseHigh",
        "current_price": cur["close"],
        "in_zone": True,
        "atr": atr,
        "body_ratio": round(body_ratio, 3),
        "close_pos": round(close_pos, 3),
        "vol_ratio": round(vol_ratio, 2),
        "zone_key": f"S5_{pair}_LONG_{round(cur['low'], 2)}",
        "confidence_score": round(score, 2),
        "confidence_label": "HIGH" if score >= 8.5 else "MEDIUM",
        "trade": {
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp, 6),
            "tp2": round(tp, 6),
        },
    }]
