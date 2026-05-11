"""
strategy8_donchian_daily_ema200.py — S8: Donchian Daily EMA200 LONG
"""

from __future__ import annotations

from config import ATR_PERIOD
from strategy_utils import calculate_atr, get_candles

TF_ENTRY = "4h"
TF_FILTER = "1d"
DONCHIAN_PERIOD = 50
VOL_LOOKBACK = 20
EMA_PERIOD = 200
RR = 3.0
MIN_RR = 2.99


def _ema_series(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    ema = sum(values[:period]) / period
    series = [ema]
    for value in values[period:]:
        ema = value * alpha + ema * (1 - alpha)
        series.append(ema)
    return series


def _avg_volume_ex_current(candles: list[dict], lookback: int) -> float:
    sample = candles[-(lookback + 1):-1]
    return sum(c["volume"] for c in sample) / len(sample) if sample else 0.0


def scan_donchian_daily_ema200_long(pair: str) -> list[dict]:
    candles_4h = get_candles(pair, TF_ENTRY, limit=DONCHIAN_PERIOD + ATR_PERIOD + VOL_LOOKBACK + 10)
    candles_1d = get_candles(pair, TF_FILTER, limit=EMA_PERIOD + 10)
    if len(candles_4h) < DONCHIAN_PERIOD + 2 or len(candles_1d) < EMA_PERIOD + 2:
        return []

    cur = candles_4h[-1]
    prior_high_50 = max(c["high"] for c in candles_4h[-(DONCHIAN_PERIOD + 1):-1])
    if cur["close"] <= prior_high_50:
        return []

    vol_avg = _avg_volume_ex_current(candles_4h, VOL_LOOKBACK)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0
    if vol_ratio <= 1.0:
        return []

    daily_closes = [c["close"] for c in candles_1d]
    ema_series = _ema_series(daily_closes, EMA_PERIOD)
    if len(ema_series) < 2:
        return []
    prev_daily_close = daily_closes[-2]
    prev_daily_ema200 = ema_series[-2]
    if prev_daily_close <= prev_daily_ema200:
        return []

    atr = calculate_atr(candles_4h, ATR_PERIOD)
    if atr <= 0:
        return []

    entry = cur["close"]
    sl = entry - 2 * atr
    risk = entry - sl
    if risk <= 0:
        return []
    tp = entry + RR * risk
    breakout_pct = (entry - prior_high_50) / prior_high_50 if prior_high_50 > 0 else 0.0
    score = min(10.0, 7.1 + min(vol_ratio - 1.0, 1.0) * 1.2 + min(breakout_pct / 0.01, 1.0) * 1.2)

    return [{
        "pair": pair,
        "tf": TF_ENTRY,
        "tf_label": "4H",
        "direction": "LONG",
        "type": "DonchianDailyEMA200Long",
        "current_price": entry,
        "in_zone": True,
        "atr": atr,
        "donchian_high": round(prior_high_50, 6),
        "daily_ema200": round(prev_daily_ema200, 6),
        "vol_ratio": round(vol_ratio, 2),
        "planned_rr": RR,
        "min_rr": MIN_RR,
        "zone_key": f"S8_{pair}_LONG_{round(prior_high_50, 2)}",
        "confidence_score": round(score, 2),
        "trade": {
            "entry": round(entry, 6),
            "sl": round(sl, 6),
            "tp1": round(tp, 6),
            "tp2": round(tp, 6),
        },
    }]
