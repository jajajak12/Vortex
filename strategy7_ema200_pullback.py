"""
strategy7_ema200_pullback.py — S7: EMA200 Pullback + Volume
"""

from __future__ import annotations

from config import ATR_PERIOD
from strategy_utils import calculate_atr, get_candles

TF_DETECT = "4h"
EMA_PERIOD = 200
ATR_MULT = 2.0
VOL_LOOKBACK = 20
VOL_RATIO_MIN = 1.2
RR = 3.0
MIN_RR = 2.99


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return 0.0
    alpha = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = value * alpha + ema * (1 - alpha)
    return float(ema)


def _avg_volume_ex_current(candles: list[dict], lookback: int = VOL_LOOKBACK) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def scan_ema200_pullback(pair: str) -> list[dict]:
    candles = get_candles(pair, TF_DETECT, limit=EMA_PERIOD + ATR_PERIOD + VOL_LOOKBACK + 10)
    if len(candles) < EMA_PERIOD + VOL_LOOKBACK + 1:
        return []

    cur = candles[-1]
    closes = [c["close"] for c in candles]
    ema200 = _ema(closes, EMA_PERIOD)
    if ema200 <= 0:
        return []

    atr = calculate_atr(candles, ATR_PERIOD)
    if atr <= 0:
        return []

    volume_sma20 = _avg_volume_ex_current(candles, VOL_LOOKBACK)
    volume_ratio = cur["volume"] / volume_sma20 if volume_sma20 > 0 else 0.0
    if volume_ratio <= VOL_RATIO_MIN:
        return []

    setups: list[dict] = []
    entry = cur["close"]
    zone_key_base = round(ema200, 2)

    if (
        cur["close"] > ema200
        and cur["low"] <= ema200 * 1.005
        and cur["close"] > cur["open"]
    ):
        sl = entry - ATR_MULT * atr
        risk = entry - sl
        if risk > 0:
            tp = entry + RR * risk
            distance = max((cur["close"] - ema200) / ema200, 0.0)
            score = min(10.0, 7.2 + min(distance / 0.01, 1.0) * 1.1)
            setups.append({
                "pair": pair,
                "tf": TF_DETECT,
                "tf_label": "4H",
                "direction": "LONG",
                "type": "EMA200PullbackLong",
                "current_price": entry,
                "in_zone": True,
                "atr": atr,
                "ema200": round(ema200, 6),
                "volume_sma20": round(volume_sma20, 2),
                "volume_ratio": round(volume_ratio, 2),
                "planned_rr": RR,
                "min_rr": MIN_RR,
                "zone_key": f"S7_{pair}_LONG_{zone_key_base}",
                "confidence_score": round(score, 2),
                "trade": {
                    "entry": round(entry, 6),
                    "sl": round(sl, 6),
                    "tp1": round(tp, 6),
                    "tp2": round(tp, 6),
                },
            })

    if (
        cur["close"] < ema200
        and cur["high"] >= ema200 * 0.995
        and cur["close"] < cur["open"]
    ):
        sl = entry + ATR_MULT * atr
        risk = sl - entry
        if risk > 0:
            tp = entry - RR * risk
            distance = max((ema200 - cur["close"]) / ema200, 0.0)
            score = min(10.0, 7.2 + min(distance / 0.01, 1.0) * 1.1)
            setups.append({
                "pair": pair,
                "tf": TF_DETECT,
                "tf_label": "4H",
                "direction": "SHORT",
                "type": "EMA200PullbackShort",
                "current_price": entry,
                "in_zone": True,
                "atr": atr,
                "ema200": round(ema200, 6),
                "volume_sma20": round(volume_sma20, 2),
                "volume_ratio": round(volume_ratio, 2),
                "planned_rr": RR,
                "min_rr": MIN_RR,
                "zone_key": f"S7_{pair}_SHORT_{zone_key_base}",
                "confidence_score": round(score, 2),
                "trade": {
                    "entry": round(entry, 6),
                    "sl": round(sl, 6),
                    "tp1": round(tp, 6),
                    "tp2": round(tp, 6),
                },
            })

    return setups
