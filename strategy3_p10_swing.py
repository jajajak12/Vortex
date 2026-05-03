"""
strategy3_p10_swing.py — S3: S7 P10 Swing Reversal

Pattern (P10-SWING from backtest discovery, 2026-05-03):
  LONG:  candle.low  touches 20-bar swing low  + high vol + bullish body + session
  SHORT: candle.high touches 20-bar swing high + high vol + bearish body + session

Backtest OOS (2024-2026, 4 pairs, RR=1.0 deployed):
  Unfiltered:       WR=41.0%  N=144  Exp=+0.229R
  Session-filtered: WR=43.3%  N=127  Exp=+0.299R  ← deployed config
"""

import math
from datetime import datetime, timezone

from config import (
    S7_SWING_LOOKBACK,
    S7_VOL_MULT,
    S7_BODY_RATIO_MIN,
    S7_SESSION_START_UTC,
    S7_SESSION_END_UTC,
)
from strategy_utils import get_candles


def _atr14(ca: list[dict]) -> float:
    """Simple ATR-14 from last 14+1 candles."""
    n = min(15, len(ca))
    tr_sum = 0.0
    for i in range(1, n):
        c = ca[-n + i]
        p = ca[-n + i - 1]
        tr_sum += max(c["high"] - c["low"],
                      abs(c["high"] - p["close"]),
                      abs(c["low"]  - p["close"]))
    return tr_sum / (n - 1) if n > 1 else 0.0


def scan_p10_swing(pair: str) -> list[dict]:
    """
    Returns list of setup dicts (0 or 1 per direction).
    Compatible with strategy_runner.scan_s3 Signal emission format.
    """
    needed = S7_SWING_LOOKBACK + 5
    ca = get_candles(pair, "1h", limit=needed)
    if len(ca) < S7_SWING_LOOKBACK + 2:
        return []

    # Session gate — uses wall-clock UTC time (live scan runs in real-time)
    utc_hour = datetime.now(timezone.utc).hour
    if not (S7_SESSION_START_UTC <= utc_hour <= S7_SESSION_END_UTC):
        return []

    cur      = ca[-1]
    lookback = ca[-(S7_SWING_LOOKBACK + 1):-1]   # prior 20 closed candles

    rng = cur["high"] - cur["low"]
    if rng <= 0:
        return []

    body      = abs(cur["close"] - cur["open"])
    body_ratio = body / rng
    if body_ratio < S7_BODY_RATIO_MIN:
        return []

    vol_avg = sum(c["volume"] for c in lookback) / len(lookback)
    vol_ratio = cur["volume"] / vol_avg if vol_avg > 0 else 0.0
    if vol_ratio < S7_VOL_MULT:
        return []

    swing_low  = min(c["low"]  for c in lookback)
    swing_high = max(c["high"] for c in lookback)

    is_bull = cur["close"] > cur["open"]
    is_bear = cur["close"] < cur["open"]

    atr = _atr14(ca)
    results = []

    # ── LONG: price touches 20-bar low, bullish close ─────────────
    if cur["low"] <= swing_low and is_bull:
        entry = cur["close"]
        sl    = cur["low"]
        risk  = abs(entry - sl)
        if risk > 0:
            score = _score(body_ratio, vol_ratio)
            results.append({
                "direction":        "LONG",
                "confidence_score": score,
                "zone_key":         f"S3_{pair}_LONG_{round(swing_low, 2)}",
                "in_zone":          True,
                "tf_label":         "1H",
                "atr":              atr,
                "body_ratio":       round(body_ratio, 3),
                "vol_ratio":        round(vol_ratio, 2),
                "swing_extreme":    swing_low,
                "trade": {
                    "entry": entry,
                    "sl":    sl,
                    "tp1":   entry + risk * 1.0,
                    "tp2":   entry + risk * 1.0,
                },
            })

    # ── SHORT: price touches 20-bar high, bearish close ──────────
    if cur["high"] >= swing_high and is_bear:
        entry = cur["close"]
        sl    = cur["high"]
        risk  = abs(sl - entry)
        if risk > 0:
            score = _score(body_ratio, vol_ratio)
            results.append({
                "direction":        "SHORT",
                "confidence_score": score,
                "zone_key":         f"S3_{pair}_SHORT_{round(swing_high, 2)}",
                "in_zone":          True,
                "tf_label":         "1H",
                "atr":              atr,
                "body_ratio":       round(body_ratio, 3),
                "vol_ratio":        round(vol_ratio, 2),
                "swing_extreme":    swing_high,
                "trade": {
                    "entry": entry,
                    "sl":    sl,
                    "tp1":   entry - risk * 1.0,
                    "tp2":   entry - risk * 1.0,
                },
            })

    return results


def _score(body_ratio: float, vol_ratio: float) -> float:
    """Confidence score 7.0–10.0 based on body strength and volume spike."""
    base  = 7.0
    base += min((body_ratio - S7_BODY_RATIO_MIN) / (1.0 - S7_BODY_RATIO_MIN), 1.0) * 1.5
    base += min((vol_ratio - S7_VOL_MULT) / S7_VOL_MULT, 1.0) * 1.5
    return round(min(base, 10.0), 2)


scan_swing_reversal = scan_p10_swing
