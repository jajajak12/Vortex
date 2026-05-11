"""
strategy9_vwap_bounce.py — S9: VWAP Bounce
"""

from __future__ import annotations

from config import ATR_PERIOD
from strategy_utils import calculate_atr, get_candles

TF_DETECT = "4h"
ROLLING_PERIOD = 20
RR = 1.5
MIN_RR = 1.49
ATR_BUFFER = 0.3


def _volume_avg_ex_current(candles: list[dict], lookback: int) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def _vwap20(candles: list[dict]) -> float:
    window = candles[-ROLLING_PERIOD:]
    numerator = 0.0
    denominator = 0.0
    for candle in window:
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3.0
        numerator += typical * candle["volume"]
        denominator += candle["volume"]
    return numerator / denominator if denominator > 0 else 0.0


def scan_vwap_bounce(pair: str) -> list[dict]:
    candles = get_candles(pair, TF_DETECT, limit=ROLLING_PERIOD + ATR_PERIOD + 10)
    if len(candles) < ROLLING_PERIOD + 2:
        return []

    cur = candles[-1]
    atr = calculate_atr(candles, ATR_PERIOD)
    if atr <= 0:
        return []
    vwap20 = _vwap20(candles)
    if vwap20 <= 0:
        return []
    vol_avg = _volume_avg_ex_current(candles, ROLLING_PERIOD)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0
    if vol_ratio <= 1.2:
        return []

    setups: list[dict] = []

    if (
        cur["low"] <= vwap20 * 1.002
        and cur["close"] > vwap20
        and cur["close"] > cur["open"]
    ):
        entry = cur["close"]
        sl = cur["low"] - ATR_BUFFER * atr
        risk = entry - sl
        if risk > 0:
            tp = entry + RR * risk
            score = min(10.0, 7.0 + min(vol_ratio - 1.2, 1.0) * 1.3)
            setups.append({
                "pair": pair,
                "tf": TF_DETECT,
                "tf_label": "4H",
                "direction": "LONG",
                "type": "VWAPBounceLong",
                "current_price": entry,
                "in_zone": True,
                "atr": atr,
                "vwap20": round(vwap20, 6),
                "vol_ratio": round(vol_ratio, 2),
                "planned_rr": RR,
                "min_rr": MIN_RR,
                "zone_key": f"S9_{pair}_LONG_{round(vwap20, 2)}",
                "confidence_score": round(score, 2),
                "trade": {
                    "entry": round(entry, 6),
                    "sl": round(sl, 6),
                    "tp1": round(tp, 6),
                    "tp2": round(tp, 6),
                },
            })

    if (
        cur["high"] >= vwap20 * 0.998
        and cur["close"] < vwap20
        and cur["close"] < cur["open"]
    ):
        entry = cur["close"]
        sl = cur["high"] + ATR_BUFFER * atr
        risk = sl - entry
        if risk > 0:
            tp = entry - RR * risk
            score = min(10.0, 7.0 + min(vol_ratio - 1.2, 1.0) * 1.3)
            setups.append({
                "pair": pair,
                "tf": TF_DETECT,
                "tf_label": "4H",
                "direction": "SHORT",
                "type": "VWAPBounceShort",
                "current_price": entry,
                "in_zone": True,
                "atr": atr,
                "vwap20": round(vwap20, 6),
                "vol_ratio": round(vol_ratio, 2),
                "planned_rr": RR,
                "min_rr": MIN_RR,
                "zone_key": f"S9_{pair}_SHORT_{round(vwap20, 2)}",
                "confidence_score": round(score, 2),
                "trade": {
                    "entry": round(entry, 6),
                    "sl": round(sl, 6),
                    "tp1": round(tp, 6),
                    "tp2": round(tp, 6),
                },
            })

    return setups
