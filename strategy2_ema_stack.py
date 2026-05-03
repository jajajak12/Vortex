"""
S2: S6 EMA Stack
===================================================
Detection: 4H EMA20 touch | Confirmation: 1H bounce | Entry: 30m
Min score: 8.0 | RR 1:2

Edge: When 1W/1D/4H EMAs all align, pullback to 4H EMA20 = high-probability
trend continuation. Trend-following to balance the reversal-heavy portfolio.

Zero ICT dependency — pure trend + mean reversion to EMA.

EMA Stack (LONG):
  1W price > EMA200  (macro trend up)
  1D price > EMA50   (intermediate trend up)
  4H EMA20 approached from above (pullback into trend)

EMA Stack (SHORT): inverse of above.

Hard Gates:
  - All 3 TF EMAs aligned (1W + 1D + 4H direction)
  - Price within ±1.5% of 4H EMA20 (pullback touch)
  - Price DID approach from correct side (was beyond EMA20 before touch)
  - 1H bounce: last 1H candle body close on correct side + body ≥ 50%
  - 1H bounce volume ≥ 1.3x avg
  - Wick rejection at 30m: MANDATORY (checked in strategy_runner)
  - Min score: 8.0

Confluence scoring (base 5.0):
  + Full 3-TF stack:              +2.0
  + Price ≤ 0.5% from EMA20:     +0.5 bonus (very precise touch)
  + 1H bounce body ≥ 60%:        +1.5  (else +0.5 if ≥ 50%)
  + Volume on 1H bounce ≥ 2x:    +1.0  (else +0.5 if ≥ 1.3x)
  + At swing structure:           +0.5
  + S1 liquidity overlap:         +0.5
  + 2+ confluence:                +0.5
"""

import math
from strategy_utils import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
)
from config import ATR_PERIOD

# ── Timeframes ────────────────────────────────────────────────
TF_1W    = "1w"
TF_1D    = "1d"
TF_4H    = "4h"
TF_1H    = "1h"
TF_ENTRY = "30m"

# ── EMA periods ───────────────────────────────────────────────
EMA_1W_PERIOD = 200
EMA_1D_PERIOD = 50
EMA_4H_PERIOD = 20

# ── Touch thresholds ──────────────────────────────────────────
TOUCH_TOL     = 0.015   # ±1.5% from 4H EMA20 = in pullback zone
PRECISE_TOL   = 0.005   # ≤0.5% = bonus for very precise touch
APPROACH_LOOK = 5       # look back N 4H candles to confirm "came from correct side"

# ── 1H bounce confirmation ────────────────────────────────────
BOUNCE_BODY_MIN  = 0.50   # 1H candle body / range ≥ 50%
BOUNCE_BODY_HIGH = 0.60   # ≥ 60% = bonus
BOUNCE_VOL_MIN   = 1.3    # volume ≥ 1.3x avg
BOUNCE_VOL_HIGH  = 2.0    # ≥ 2x = bonus

# ── Trade ─────────────────────────────────────────────────────
SL_ATR_MULT = 1.5   # SL = EMA20 ± SL_ATR_MULT * ATR
SL_BUFFER   = 0.003
TP1_MAX_RR  = 2.0
TP2_MAX_RR  = 2.0

# ── Scoring ───────────────────────────────────────────────────
BASE_SCORE = 5.0
MIN_SCORE  = 8.0
SCORE_HIGH = 9.5

TF_LABEL = {TF_4H: "4H", TF_1H: "1H", TF_ENTRY: "30m"}


# ═══════════════════════════════════════════════════════════════
# EMA CALCULATION
# ═══════════════════════════════════════════════════════════════

def _ema(candles: list[dict], period: int) -> float:
    """Wilder EMA over close prices. Seeded with SMA of first `period` candles."""
    n = len(candles)
    if n < period:
        return candles[-1]["close"] if candles else 0.0
    k    = 2.0 / (period + 1)
    ema  = sum(c["close"] for c in candles[:period]) / period
    for c in candles[period:]:
        ema = c["close"] * k + ema * (1 - k)
    return ema


def _avg_vol(candles: list[dict], lookback: int = 20) -> float:
    n = len(candles)
    if n < 3:
        return 1.0
    start = max(1, n - lookback)
    vols  = [c["volume"] for c in candles[start:n - 1]]
    return sum(vols) / len(vols) if vols else 1.0


# ═══════════════════════════════════════════════════════════════
# EMA STACK CHECK
# ═══════════════════════════════════════════════════════════════

def _check_ema_stack(pair: str) -> dict | None:
    """
    Compute EMA on 1W/1D/4H. Return direction if fully aligned, else None.
    Returns: {direction, ema_1w, ema_1d, ema_4h, price_4h, atr_4h, c4h}
    """
    # 1W EMA200
    c1w = get_candles(pair, TF_1W, limit=220)
    if len(c1w) < 30:
        return None
    ema_1w  = _ema(c1w, min(EMA_1W_PERIOD, len(c1w)))
    bull_1w = c1w[-1]["close"] > ema_1w

    # 1D EMA50
    c1d = get_candles(pair, TF_1D, limit=70)
    if len(c1d) < 20:
        return None
    ema_1d  = _ema(c1d, min(EMA_1D_PERIOD, len(c1d)))
    bull_1d = c1d[-1]["close"] > ema_1d

    # 4H EMA20 + ATR
    c4h = get_candles(pair, TF_4H, limit=120)
    if len(c4h) < ATR_PERIOD + 5:
        return None
    ema_4h  = _ema(c4h, EMA_4H_PERIOD)
    bull_4h = c4h[-1]["close"] > ema_4h
    atr_4h  = calculate_atr(c4h)

    if bull_1w and bull_1d:
        direction = "LONG"
    elif not bull_1w and not bull_1d:
        direction = "SHORT"
    else:
        return None   # mixed macro — no clear stack

    return {
        "direction": direction,
        "ema_1w":    round(ema_1w, 6),
        "ema_1d":    round(ema_1d, 6),
        "ema_4h":    round(ema_4h, 6),
        "price_4h":  round(c4h[-1]["close"], 6),
        "atr_4h":    round(atr_4h, 6),
        "c4h":       c4h,
        "bull_4h":   bull_4h,
    }


# ═══════════════════════════════════════════════════════════════
# PULLBACK TOUCH CHECK
# ═══════════════════════════════════════════════════════════════

def _check_pullback(stack: dict) -> dict | None:
    """
    Verify price is touching 4H EMA20 from the correct side.
    LONG: price was above EMA20 (APPROACH_LOOK candles ago), now within TOUCH_TOL.
    SHORT: price was below EMA20, now within TOUCH_TOL.
    Returns: {in_touch, precise_touch, approach_confirmed}
    """
    c4h       = stack["c4h"]
    ema_4h    = stack["ema_4h"]
    price     = stack["price_4h"]
    direction = stack["direction"]
    n         = len(c4h)

    # Is price within touch zone?
    diff_pct = abs(price - ema_4h) / ema_4h
    if diff_pct > TOUCH_TOL:
        return None

    # Did price approach from the correct side?
    # Check last APPROACH_LOOK candles before current
    lookback_slice = c4h[max(0, n - 1 - APPROACH_LOOK): n - 1]
    if not lookback_slice:
        return None

    if direction == "LONG":
        # At least half of lookback candles should have been above EMA20
        above_count = sum(1 for c in lookback_slice if c["close"] > ema_4h)
        if above_count < len(lookback_slice) // 2:
            return None
    else:
        below_count = sum(1 for c in lookback_slice if c["close"] < ema_4h)
        if below_count < len(lookback_slice) // 2:
            return None

    return {
        "in_touch":       True,
        "precise_touch":  diff_pct <= PRECISE_TOL,
        "diff_pct":       round(diff_pct * 100, 3),
    }


# ═══════════════════════════════════════════════════════════════
# 1H BOUNCE CONFIRMATION
# ═══════════════════════════════════════════════════════════════

def _check_bounce(pair: str, direction: str, ema_4h: float) -> dict | None:
    """
    Verify 1H bounce at EMA20 level.
    LONG: last 1H candle body closes above ema_4h with ≥ 50% body ratio.
    SHORT: last 1H candle body closes below ema_4h with ≥ 50% body ratio.
    """
    c1h = get_candles(pair, TF_1H, limit=10)
    if len(c1h) < 3:
        return None

    avg_v = _avg_vol(c1h, lookback=8)
    last  = c1h[-1]
    rng   = last["high"] - last["low"]
    if rng == 0:
        return None
    body      = abs(last["close"] - last["open"])
    body_ratio = body / rng
    vol_ratio  = last["volume"] / avg_v if avg_v > 0 else 1.0

    if body_ratio < BOUNCE_BODY_MIN:
        return None
    if vol_ratio < BOUNCE_VOL_MIN:
        return None

    if direction == "LONG":
        # Close must be above EMA level
        if last["close"] <= ema_4h:
            return None
        # Bullish candle
        if last["close"] <= last["open"]:
            return None
        # At least one of last 3 candles touched or went below EMA (the actual pullback)
        touched = any(c["low"] <= ema_4h * 1.005 for c in c1h[-4:-1])
        if not touched:
            return None
    else:
        if last["close"] >= ema_4h:
            return None
        if last["close"] >= last["open"]:
            return None
        touched = any(c["high"] >= ema_4h * 0.995 for c in c1h[-4:-1])
        if not touched:
            return None

    return {
        "confirmed":   True,
        "body_ratio":  round(body_ratio, 3),
        "vol_ratio":   round(vol_ratio, 2),
        "close_1h":    round(last["close"], 6),
    }


# ═══════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════

def _score(
    precise_touch: bool,
    body_ratio: float,
    vol_ratio: float,
    at_struct: bool,
    has_s1: bool,
) -> dict:
    s = BASE_SCORE
    s += 2.0   # full 3-TF stack is always confirmed at this point
    if precise_touch:   s += 0.5
    if body_ratio >= BOUNCE_BODY_HIGH: s += 1.5
    elif body_ratio >= BOUNCE_BODY_MIN: s += 0.5
    if vol_ratio >= BOUNCE_VOL_HIGH:   s += 1.0
    elif vol_ratio >= BOUNCE_VOL_MIN:  s += 0.5
    if at_struct: s += 0.5
    if has_s1:    s += 0.5
    if sum([at_struct, has_s1]) >= 2: s += 0.5
    s = min(s, 10.0)
    lbl = "HIGH" if s >= SCORE_HIGH else "MEDIUM" if s >= MIN_SCORE else "LOW"
    return {"score": round(s, 1), "label": lbl}


# ═══════════════════════════════════════════════════════════════
# TRADE CALC
# ═══════════════════════════════════════════════════════════════

def _calc(entry: float, ema_4h: float, atr_4h: float,
          direction: str) -> dict:
    if direction == "LONG":
        sl   = (ema_4h - atr_4h * SL_ATR_MULT) * (1 - SL_BUFFER)
        d    = entry - sl
        tp1  = entry + d * TP1_MAX_RR
        tp2  = entry + d * TP2_MAX_RR
    else:
        sl   = (ema_4h + atr_4h * SL_ATR_MULT) * (1 + SL_BUFFER)
        d    = sl - entry
        tp1  = entry - d * TP1_MAX_RR
        tp2  = entry - d * TP2_MAX_RR
    rr1 = round(abs(tp1 - entry) / d, 2) if d > 0 else 0
    rr2 = round(abs(tp2 - entry) / d, 2) if d > 0 else 0
    return {
        "entry": round(entry, 6), "sl": round(sl, 6),
        "tp1":   round(tp1, 6),   "tp2": round(tp2, 6),
        "rr1":   f"1:{rr1}",      "rr2": f"1:{rr2}",
        "sl_pct":  round(d / entry * 100, 3),
        "tp1_pct": round(abs(tp1 - entry) / entry * 100, 3),
        "tp2_pct": round(abs(tp2 - entry) / entry * 100, 3),
    }


def _zone_key_bucket(pair: str, direction: str, ema_4h: float) -> str:
    """Stable zone key — buckets EMA within 1% price bands."""
    mag  = 10 ** max(0, int(math.log10(max(ema_4h, 0.001))) - 1)
    buck = round(ema_4h / mag) * mag
    return f"{pair}_{direction}_EMA20_{buck:.4g}"


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def scan_ema_stack(
    pair: str,
    s1_zones: dict | None = None,
) -> list[dict]:
    """
    Scan pair for S6: EMA Stack Momentum pullback.
    Returns list with 0 or 1 setup (one active pullback per pair).
    """
    s1_zones = s1_zones or {}

    # ── Step 1: EMA stack alignment ──────────────────────────
    stack = _check_ema_stack(pair)
    if stack is None:
        return []

    direction = stack["direction"]
    ema_4h    = stack["ema_4h"]
    atr_4h    = stack["atr_4h"]
    price     = stack["price_4h"]
    c4h       = stack["c4h"]

    # ── Step 2: Pullback touch ────────────────────────────────
    pullback = _check_pullback(stack)
    if pullback is None:
        return []

    # ── Step 3: 1H bounce ─────────────────────────────────────
    bounce = _check_bounce(pair, direction, ema_4h)
    if bounce is None:
        return []

    # ── Step 4: Structural confluence ─────────────────────────
    sw_lo    = find_swing_lows(c4h, lookback=10)
    sw_hi    = find_swing_highs(c4h, lookback=10)
    at_struct = any(
        abs(sw["price"] - ema_4h) / ema_4h < 0.02
        for sw in (sw_lo if direction == "LONG" else sw_hi)
    )

    # ── Step 5: S1 liquidity overlap ─────────────────────────
    has_s1 = any(
        abs((z["low"] + z["high"]) / 2 - ema_4h) / ema_4h < 0.02
        and z["type"] == direction
        for z in (s1_zones.get("LONG", []) + s1_zones.get("SHORT", []))
    )

    # ── Step 6: Score + gate ──────────────────────────────────
    sc = _score(
        pullback["precise_touch"],
        bounce["body_ratio"],
        bounce["vol_ratio"],
        at_struct,
        has_s1,
    )
    if sc["score"] < MIN_SCORE:
        return []

    # ── Step 7: Trade calc ────────────────────────────────────
    trade     = _calc(price, ema_4h, atr_4h, direction)
    zone_key  = _zone_key_bucket(pair, direction, ema_4h)

    # Zone bounds for in_zone check (used by strategy_runner)
    zone_low  = round(ema_4h * (1 - TOUCH_TOL), 6)
    zone_high = round(ema_4h * (1 + TOUCH_TOL), 6)

    return [{
        "pair":             pair,
        "tf":               TF_4H,
        "tf_label":         TF_LABEL[TF_4H],
        "direction":        direction,
        "type":             "EMAStack",
        "ema_1w":           stack["ema_1w"],
        "ema_1d":           stack["ema_1d"],
        "ema_4h":           ema_4h,
        "atr":              atr_4h,
        "current_price":    price,
        "zone_low":         zone_low,
        "zone_high":        zone_high,
        "zone_mid":         round(ema_4h, 6),
        "in_zone":          True,   # pullback check already confirmed
        "trade":            trade,
        "pullback":         pullback,
        "bounce":           bounce,
        "at_structure":     at_struct,
        "has_s1":           has_s1,
        "vol_ratio":        bounce["vol_ratio"],
        "confidence_score": sc["score"],
        "confidence_label": sc["label"],
        "zone_key":         zone_key,
    }]
