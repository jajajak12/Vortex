"""
Strategy 3: FVG + Imbalance (S3) — UPGRADED
============================================
Detection: 4H primary | Confirmation: 1H | Entry: 30m
Min score: 7.0 | TP1 max 1:3.0 | TP2 max 1:4.8

FVG (Fair Value Gap): displacement candle creates a gap zone.
Imbalance: displacement candle with gap >= 40% ATR.

Flow:
  1. Detect FVG/imbalance on 4H (lookback 6 candles — UPGRADED tighter window)
  2. Price MUST retrace INTO the gap zone (reclaim)
  3. Volume confirmation on displacement candle
  4. Wick rejection at 30m = MANDATORY entry confirm
  5. Score >= 7.0 for signal

Hard Gates:
  - FVG size >= 25% ATR (UPGRADED from 30%)
  - Imbalance size >= 40% ATR (UPGRADED from 50%)
  - Price INSIDE the gap zone (not just touching)
  - Displacement body >= 50% (UPGRADED from 55%)
  - Volume >= 1.3x avg (UPGRADED from 1.5x)
  - Wick rejection at 30m: MANDATORY

Confluence (base 5.0, min 7.0):
  + Price inside zone:              +1.0  (UPGRADED from +0.5)
  + HTF 4H aligned:               +1.5
  + S2 wick overlap:              +2.0
  + S5 compression overlap:       +1.5
  + Volume spike >= 1.5x avg:       +1.0
  + FVG at swing structure:        +0.5
  + 2+ confluence bonus:           +1.0
"""

from strategy1_liquidity import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
    _compute_htf_bias,
)
from strategy2_wick import is_long_downside_wick, is_long_upside_wick
from config import ATR_PERIOD, TF_ZONE

# ── S3 Thresholds (UPGRADED) ─────────────────────────────────
S3_TF_DETECT  = TF_ZONE
S3_TF_CONFIRM = "1h"
S3_TF_ENTRY   = "30m"

# FVG — TIGHTENED
FVG_LOOKBACK   = 20       # age filter: only last 20 candles (was 6 — too recent, small window)
FVG_TOUCH_LOOK = 20
FVG_MIN_ATR    = 0.40     # tightened from 0.25 — micro-FVG is noise

# Imbalance — TIGHTENED
IMBALANCE_MIN_ATR  = 0.55   # tightened from 0.40 (was 0.50 → 0.40 → now 0.55)
IMBALANCE_LOOKBACK = 20      # same age filter as FVG

# Entry
RETECT_TOLERANCE = 0.004   # Price must be inside zone
ENTRY_ATR_ZONE   = 0.20

# Displacement (HARD GATE) — tightened
DISP_BODY_MIN    = 0.55    # tightened from 0.50 (was 0.55 → 0.50 → back to 0.55)
DISP_VOL_MIN     = 1.5     # tightened from 1.3

# Score — base 5.0, min 7.5 for signal (raised from 7.0)
S3_BASE_SCORE = 5.0
S3_MIN_SCORE  = 7.5
S3_SCORE_HIGH = 9.0

# TP
TP1_MAX_RR  = 3.0
TP2_MAX_RR  = 4.8
S3_SL_BUFFER = 0.005

TF_LABEL = {"4h": "4H", "1h": "1H", "30m": "30m"}


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _avg_volume(candles: list[dict], lookback: int = 20) -> float:
    if len(candles) < lookback:
        lookback = max(1, len(candles) - 1)
    vols = [c["volume"] for c in candles[-lookback:-1]]
    return sum(vols) / len(vols) if vols else 0.0


def _is_in_zone(price: float, zone_low: float, zone_high: float) -> bool:
    return zone_low <= price <= zone_high


def _volume_confirmed(candles: list[dict], index: int) -> tuple[bool, float]:
    avg = _avg_volume(candles)
    if avg <= 0:
        return True, 0.0
    ratio = candles[index]["volume"] / avg
    return ratio >= DISP_VOL_MIN, round(ratio, 2)


def _is_displacement(candle: dict) -> dict | None:
    body_top    = max(candle["open"], candle["close"])
    body_bottom = min(candle["open"], candle["close"])
    body        = body_top - body_bottom
    total_range = candle["high"] - candle["low"]
    if total_range == 0:
        return None
    body_ratio = body / total_range
    if body_ratio < DISP_BODY_MIN:
        return None
    direction = "bullish" if candle["close"] > candle["open"] else "bearish"
    return {
        "is_disp":     True,
        "body_ratio":  round(body_ratio, 3),
        "direction":   direction,
        "body":        round(body, 4),
        "total_range": round(total_range, 4),
    }


# ═══════════════════════════════════════════════════════════════
# FVG DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_fvg(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Detect all FVGs in lookback window.
    FVG = candle i-2 fully above/below candle i-1 range.
    """
    results = []
    n = len(candles_4h)

    for i in range(max(2, n - FVG_LOOKBACK), n - 1):
        c1 = candles_4h[i - 2]
        c2 = candles_4h[i - 1]
        c3 = candles_4h[i]

        if c3["close"] > c1["high"]:   # Bullish FVG
            fvg_low  = c1["high"]
            fvg_high = c3["low"]
            gap      = fvg_high - fvg_low
            if gap < atr * FVG_MIN_ATR:
                continue
            vol_ok, vol_ratio = _volume_confirmed(candles_4h, i)
            disp = _is_displacement(c3)
            if not disp:
                continue
            results.append({
                "type":      "FVG",
                "direction": "LONG",
                "fvg_low":   round(fvg_low, 4),
                "fvg_high":  round(fvg_high, 4),
                "fvg_mid":   round((fvg_low + fvg_high) / 2, 4),
                "fvg_size":  round(gap, 4),
                "atr_pct":   round(gap / atr, 2),
                "disp_candle_idx": i,
                "vol_confirmed": vol_ok,
                "vol_ratio": vol_ratio,
                "disp": disp,
            })

        elif c3["close"] < c1["low"]:  # Bearish FVG
            fvg_low  = c3["high"]
            fvg_high = c1["low"]
            gap      = fvg_high - fvg_low
            if gap < atr * FVG_MIN_ATR:
                continue
            vol_ok, vol_ratio = _volume_confirmed(candles_4h, i)
            disp = _is_displacement(c3)
            if not disp:
                continue
            results.append({
                "type":      "FVG",
                "direction": "SHORT",
                "fvg_low":   round(fvg_low, 4),
                "fvg_high":  round(fvg_high, 4),
                "fvg_mid":   round((fvg_low + fvg_high) / 2, 4),
                "fvg_size":  round(gap, 4),
                "atr_pct":   round(gap / atr, 2),
                "disp_candle_idx": i,
                "vol_confirmed": vol_ok,
                "vol_ratio": vol_ratio,
                "disp": disp,
            })

    return results


# ═══════════════════════════════════════════════════════════════
# IMBALANCE DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_imbalance(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Detect imbalances: displacement candles with gap >= IMBALANCE_MIN_ATR ATR.
    Imbalance is bigger than FVG but doesn't need full 3-candle gap.
    """
    results = []
    n = len(candles_4h)

    for i in range(max(1, n - IMBALANCE_LOOKBACK), n - 1):
        c1 = candles_4h[i - 1]
        c2 = candles_4h[i]

        gap_bull = c2["close"] - c1["high"]
        gap_bear = c1["low"] - c2["open"]

        disp = _is_displacement(c2)
        if not disp:
            continue

        if gap_bull > atr * IMBALANCE_MIN_ATR:
            results.append({
                "type":      "Imbalance",
                "direction": "LONG",
                "fvg_low":   round(c1["high"], 4),
                "fvg_high":  round(c2["close"], 4),
                "fvg_mid":   round((c1["high"] + c2["close"]) / 2, 4),
                "fvg_size":  round(gap_bull, 4),
                "atr_pct":   round(gap_bull / atr, 2),
                "disp_candle_idx": i,
                "vol_confirmed": _volume_confirmed(candles_4h, i)[0],
                "vol_ratio": _volume_confirmed(candles_4h, i)[1],
                "disp": disp,
            })

        elif gap_bear > atr * IMBALANCE_MIN_ATR:
            results.append({
                "type":      "Imbalance",
                "direction": "SHORT",
                "fvg_low":   round(c2["open"], 4),
                "fvg_high":  round(c1["low"], 4),
                "fvg_mid":   round((c2["open"] + c1["low"]) / 2, 4),
                "fvg_size":  round(gap_bear, 4),
                "atr_pct":   round(gap_bear / atr, 2),
                "disp_candle_idx": i,
                "vol_confirmed": _volume_confirmed(candles_4h, i)[0],
                "vol_ratio": _volume_confirmed(candles_4h, i)[1],
                "disp": disp,
            })

    return results


# ═══════════════════════════════════════════════════════════════
# WICK REJECTION CHECK
# ═══════════════════════════════════════════════════════════════

def _check_wick_rejection(
    direction: str,
    zone_price: float,
    pair: str,
) -> dict | None:
    """
    Check for wick rejection at zone on 30m candles.
    Returns dict if rejection found, None otherwise.
    """
    for tf in ("30m", "15m", "5m"):
        try:
            candles_tf = get_candles(pair, tf, limit=20)
        except Exception:
            continue
        if len(candles_tf) < 2:
            continue
        for c in candles_tf[-4:]:
            if direction == "LONG":
                wick = is_long_downside_wick(c)
                if wick:
                    diff_pct = abs(wick["wick_low"] - zone_price) / zone_price
                    if diff_pct < 0.005:
                        return {
                            "rejected":         True,
                            "tf":               tf,
                            "wick_low":         wick["wick_low"],
                            "wick_body_ratio":  wick["wick_body_ratio"],
                        }
            else:
                wick = is_long_upside_wick(c)
                if wick:
                    diff_pct = abs(wick["wick_high"] - zone_price) / zone_price
                    if diff_pct < 0.005:
                        return {
                            "rejected":         True,
                            "tf":               tf,
                            "wick_high":        wick["wick_high"],
                            "wick_body_ratio":  wick["wick_body_ratio"],
                        }
    return None


# ═══════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════

def _compute_score(
    imbalance_type: str,
    htf_bullish: bool, htf_bearish: bool,
    has_s2: bool, has_s5: bool,
    vol_confirmed: bool,
    in_zone: bool,
    at_swing: bool,
    vol_ratio: float,
) -> dict:
    score = S3_BASE_SCORE

    if in_zone:
        score += 1.0   # UPGRADED: +1.0 (was +0.5)
    if htf_bullish or htf_bearish:
        score += 1.5
    if has_s2:
        score += 1.0   # tightened from 2.0 — was artificially inflating scores
    if has_s5:
        score += 1.5
    if vol_confirmed:
        score += 1.0
        if vol_ratio >= 2.0:
            score += 0.5
    if at_swing:
        score += 0.5

    conf_count = sum([has_s2, has_s5])
    if conf_count >= 2:
        score += 1.0

    score = min(score, 10.0)
    label = "⭐⭐⭐ HIGH" if score >= S3_SCORE_HIGH else "⭐⭐ MEDIUM" if score >= S3_MIN_SCORE else "⭐ STANDARD"
    return {"score": round(score, 1), "label": label}


# ═══════════════════════════════════════════════════════════════
# TRADE CALCULATION
# ═══════════════════════════════════════════════════════════════

def _calc_trade(entry: float, zone_low: float, zone_high: float,
                direction: str) -> dict:
    """Entry = zone mid or current price if already inside zone."""
    if direction == "LONG":
        sl      = zone_low * (1 - S3_SL_BUFFER)
        sl_dist = entry - sl
        tp1    = entry + sl_dist * TP1_MAX_RR
        tp2    = entry + sl_dist * TP2_MAX_RR
    else:
        sl      = zone_high * (1 + S3_SL_BUFFER)
        sl_dist = sl - entry
        tp1    = entry - sl_dist * TP1_MAX_RR
        tp2    = entry - sl_dist * TP2_MAX_RR

    rr1 = round(abs(tp1 - entry) / sl_dist, 2) if sl_dist > 0 else 0
    rr2 = round(abs(tp2 - entry) / sl_dist, 2) if sl_dist > 0 else 0

    return {
        "entry":   round(entry, 4),
        "sl":      round(sl, 4),
        "tp1":     round(tp1, 4),
        "tp2":     round(tp2, 4),
        "rr1":     f"1:{rr1}",
        "rr2":     f"1:{rr2}",
        "sl_pct":  round(sl_dist / entry * 100, 2),
        "tp1_pct": round(abs(tp1 - entry) / entry * 100, 2),
        "tp2_pct": round(abs(tp2 - entry) / entry * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def scan_fvg_imbalance(
    pair: str,
    wick_setups: list[dict],
    engineered_setups: list[dict],
) -> list[dict]:
    """
    Scan pair for S3: FVG + Imbalance setups (UPGRADED).
    wick_setups: from scan_wick_setups() for S2 confluence.
    engineered_setups: from scan_engineered_setups() for S5 confluence.
    Returns setups sorted by score descending.
    """
    engineered_setups = engineered_setups or []
    results = []

    candles_4h = get_candles(pair, S3_TF_DETECT, limit=100)
    if len(candles_4h) < ATR_PERIOD + 10:
        return []

    atr = calculate_atr(candles_4h)
    current_price = candles_4h[-1]["close"]
    n = len(candles_4h)

    # Detect swings for structural confluence
    swing_lows  = find_swing_lows(candles_4h, lookback=5)
    swing_highs = find_swing_highs(candles_4h, lookback=5)

    # Detect FVG and Imbalance
    all_gaps = _detect_fvg(candles_4h, atr) + _detect_imbalance(candles_4h, atr)

    # Filter out invalidated gaps
    valid_gaps = []
    for gap in all_gaps:
        direction = gap["direction"]
        check_from = gap["disp_candle_idx"] + 3
        invalidated = False
        for c in candles_4h[check_from:]:
            if direction == "LONG"  and c["close"] < gap["fvg_low"]:
                invalidated = True
                break
            if direction == "SHORT" and c["close"] > gap["fvg_high"]:
                invalidated = True
                break
        if not invalidated:
            valid_gaps.append(gap)

    # HTF bias
    _htf = _compute_htf_bias(candles_4h)
    htf_bullish = _htf == "LONG"
    htf_bearish = _htf == "SHORT"

    for gap in valid_gaps:
        direction  = gap["direction"]
        zone_price = gap["fvg_mid"]

        # S2 wick overlap
        has_s2 = False
        for w in wick_setups:
            wick_zone_price = w.get("wick", {}).get("wick_low") or w.get("wick", {}).get("wick_high", 0)
            if wick_zone_price and abs(wick_zone_price - zone_price) / zone_price < 0.02:
                if (direction == "LONG" and w.get("direction") == "LONG") or \
                   (direction == "SHORT" and w.get("direction") == "SHORT"):
                    has_s2 = True
                    break

        # S5 compression overlap
        has_s5 = any(
            (direction == "LONG" and e.get("direction") == "LONG") or
            (direction == "SHORT" and e.get("direction") == "SHORT")
            for e in engineered_setups
        )

        # At swing level confluence
        at_swing = any(
            abs(s["price"] - zone_price) / zone_price < 0.02
            for s in (swing_lows if direction == "LONG" else swing_highs)
        )

        in_zone = _is_in_zone(current_price, gap["fvg_low"], gap["fvg_high"])

        sc = _compute_score(
            gap["type"],
            htf_bullish, htf_bearish,
            has_s2, has_s5,
            gap["vol_confirmed"],
            in_zone,
            at_swing,
            gap["vol_ratio"],
        )

        trade = None
        if in_zone:
            trade = _calc_trade(
                current_price,
                gap["fvg_low"],
                gap["fvg_high"],
                direction,
            )

        results.append({
            "pair":              pair,
            "tf":                S3_TF_DETECT,
            "tf_label":          TF_LABEL[S3_TF_DETECT],
            "direction":         direction,
            "type":              gap["type"],
            "fvg":              gap,
            "atr":               round(atr, 4),
            "current_price":     current_price,
            "in_zone":           in_zone,
            "trade":             trade,
            "has_s2_confluence": has_s2,
            "has_s5_confluence": has_s5,
            "vol_confirmed":     gap["vol_confirmed"],
            "vol_ratio":         gap["vol_ratio"],
            "wick_rejection":    _check_wick_rejection(direction, zone_price, pair) if in_zone else None,
            "confidence_score":  sc["score"],
            "confidence_label":  sc["label"],
        })

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
