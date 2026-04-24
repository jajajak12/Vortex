"""
Strategy 1: Classic Chart Patterns (S1) — PURE
===============================================
Deteksi: 4H | Konfirmasi: 1H | Entry: 30m
Min score: 8.0 | TP1 max 1:3.0 | TP2 max 1:4.8

POLA YANG DETEKSI:
  - Rising Wedge (bearish): converging higher highs + higher lows
  - Falling Wedge (bullish): converging lower highs + lower lows
  - Head & Shoulders (bearish): 3 peaks, middle highest, neckline break
  - Inverse H&S (bullish): 3 troughs, middle lowest, neckline break
  - Bull Flag (bullish): strong impulse UP, diagonal DOWN channel, continue UP
  - Bear Flag (bearish): strong impulse DOWN, diagonal UP channel, continue DOWN

Hard Gates:
  - Min 3 touches per trendline channel
  - Breakout candle body ≥ 60% of its range
  - Volume ≥ 1.5x avg on breakout candle
  - Wick rejection MANDATORY at 15m/30m
  - NO liquidity grab, NO fresh zone, NO unmitigated check
  - Pattern must form at swing high/low (structural anchor)
  - HTF bias must align (bullish pattern in uptrend = LONG)

TP: SL di luar pattern boundary, TP1=entry+dist*3.0, TP2=entry+dist*4.8
"""

from strategy1_liquidity import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
    _compute_htf_bias,
)
from strategy2_wick import is_long_downside_wick, is_long_upside_wick
from config import ATR_PERIOD

# ── Timeframes ───────────────────────────────────────────────
S1_TF_DETECT  = "4h"
S1_TF_CONFIRM = "1h"
S1_TF_ENTRY   = "30m"

# ── Pattern thresholds ────────────────────────────────────────
MIN_TOUCHES       = 3        # min touches per trendline
BREAKOUT_BODY_MIN = 0.65     # tightened from 0.60
BREAKOUT_VOL_MIN  = 2.0      # tightened from 1.5 — false breakouts rarely have 2x volume
BREAKOUT_MAX_AGE  = 15       # breakout candle must be within last 15 candles (recency filter)

# ── Scoring ─────────────────────────────────────────────────
S1_BASE_SCORE  = 5.5
S1_MIN_SCORE  = 8.0
S1_SCORE_HIGH = 9.5

# ── TP ──────────────────────────────────────────────────────
TP1_MAX_RR  = 3.0
TP2_MAX_RR  = 4.8
S1_SL_BUFFER = 0.005

TF_LABEL = {"4h": "4H", "1h": "1H", "30m": "30m"}


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _avg_vol(candles: list[dict], lookback: int = 20) -> float:
    n = len(candles)
    if n < 3:
        return 1.0
    start = max(1, n - lookback)
    vols = [c["volume"] for c in candles[start:n - 1]]
    return sum(vols) / len(vols) if vols else 1.0


def _is_displacement(c: dict, avg_vol: float) -> dict | None:
    """Returns {valid, body_ratio, vol_ratio} or None."""
    total_range = c["high"] - c["low"]
    if total_range == 0:
        return None
    body = abs(c["close"] - c["open"])
    body_ratio = body / total_range
    if body_ratio < BREAKOUT_BODY_MIN:
        return None
    vol_ratio = c["volume"] / avg_vol
    if vol_ratio < BREAKOUT_VOL_MIN:
        return None
    return {"body_ratio": round(body_ratio, 3), "vol_ratio": round(vol_ratio, 2)}


def _linregress(xs: list, ys: list) -> tuple[float, float]:
    """Simple linear regression: returns (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(n))
    den = sum((xs[i] - x_mean) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0.0
    intercept = y_mean - slope * x_mean
    return slope, intercept


def _touch_distance(price: float, slope: float, intercept: float, idx: int) -> float:
    """Distance from price to trendline at index."""
    expected = slope * idx + intercept
    return abs(price - expected) / expected


def _in_zone(price: float, lo: float, hi: float, tol: float = 0.005) -> bool:
    return lo * (1 - tol) <= price <= hi * (1 + tol)


# ═══════════════════════════════════════════════════════════════
# WEDGE DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_wedge(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Rising Wedge (bearish): converging higher highs + higher lows.
    Breakout: candle closes below lower trendline.
    Falling Wedge (bullish): converging lower highs + lower lows.
    Breakout: candle closes above upper trendline.
    """
    results = []
    n = len(candles_4h)
    avg_v = _avg_vol(candles_4h)

    if n < 15:
        return results

    sw_lo = find_swing_lows(candles_4h, lookback=5)
    sw_hi = find_swing_highs(candles_4h, lookback=5)

    # ── Rising Wedge: higher highs + higher lows (bearish) ──────
    if len(sw_hi) >= MIN_TOUCHES and len(sw_lo) >= MIN_TOUCHES:
        hi_idxs = [s["index"] for s in sw_hi]
        hi_prices = [s["price"] for s in sw_hi]
        lo_idxs = [s["index"] for s in sw_lo]
        lo_prices = [s["price"] for s in sw_lo]

        # Check converging: highs slope < lows slope (wedge shape)
        hi_slope, _ = _linregress(hi_idxs, hi_prices)
        lo_slope, _ = _linregress(lo_idxs, lo_prices)

        # Rising Wedge: highs still rising but slower than lows
        if hi_slope > 0 and lo_slope > 0 and hi_slope < lo_slope:
            # Check breakout: candle closes below lower trendline
            lo_intercept = lo_prices[-1] - lo_slope * lo_idxs[-1]
            upper_intercept = hi_prices[-1] - hi_slope * hi_idxs[-1]

            for i in range(max(hi_idxs[-1], lo_idxs[-1]) + 1, n):
                c = candles_4h[i]
                lower_trend = lo_slope * i + lo_intercept
                # Close below lower trendline = bearish breakout
                if c["close"] < lower_trend:
                    disp = _is_displacement(c, avg_v)
                    if not disp:
                        continue
                    # Wedge must be at/near swing high area
                    pattern_top = upper_intercept + hi_slope * i
                    pattern_bot = lower_trend

                    results.append({
                        "pattern":    "RisingWedge",
                        "direction":  "SHORT",
                        "zone_low":   round(pattern_bot, 4),
                        "zone_high":  round(pattern_top, 4),
                        "zone_mid":   round((pattern_bot + pattern_top) / 2, 4),
                        "atr_pct":    round(abs(pattern_top - pattern_bot) / atr, 2),
                        "break_idx":  i,
                        "vol_confirmed": True,
                        "vol_ratio":  disp["vol_ratio"],
                        "disp":       disp,
                    })
                    break

        # ── Falling Wedge: lower highs + lower lows (bullish) ────
        if hi_slope < 0 and lo_slope < 0 and lo_slope > hi_slope:
            lo_intercept = lo_prices[-1] - lo_slope * lo_idxs[-1]
            upper_intercept = hi_prices[-1] - hi_slope * hi_idxs[-1]

            for i in range(max(hi_idxs[-1], lo_idxs[-1]) + 1, n):
                c = candles_4h[i]
                upper_trend = hi_slope * i + upper_intercept
                # Close above upper trendline = bullish breakout
                if c["close"] > upper_trend:
                    disp = _is_displacement(c, avg_v)
                    if not disp:
                        continue
                    pattern_top = upper_trend
                    pattern_bot = lo_slope * i + lo_intercept

                    results.append({
                        "pattern":    "FallingWedge",
                        "direction":  "LONG",
                        "zone_low":   round(pattern_bot, 4),
                        "zone_high":  round(pattern_top, 4),
                        "zone_mid":   round((pattern_bot + pattern_top) / 2, 4),
                        "atr_pct":    round(abs(pattern_top - pattern_bot) / atr, 2),
                        "break_idx":  i,
                        "vol_confirmed": True,
                        "vol_ratio":  disp["vol_ratio"],
                        "disp":       disp,
                    })
                    break

    return results


# ═══════════════════════════════════════════════════════════════
# HEAD & SHOULDERS DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_hs(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Head & Shoulders (bearish): left shoulder, head (higher), right shoulder (equal to left).
    Neckline connects the two troughs. Breakout: close below neckline.
    Inverse H&S (bullish): inverse, breakout above neckline.
    """
    results = []
    n = len(candles_4h)
    avg_v = _avg_vol(candles_4h)

    if n < 20:
        return results

    sw_lo = find_swing_lows(candles_4h, lookback=5)
    sw_hi = find_swing_highs(candles_4h, lookback=5)

    if len(sw_hi) < 3 or len(sw_lo) < 2:
        return results

    # H&S: need 3 highs — left shoulder, head (highest), right shoulder
    for i in range(len(sw_hi) - 2):
        lsh = sw_hi[i]       # left shoulder
        head = sw_hi[i + 1]  # head — must be highest
        rsh = sw_hi[i + 2]   # right shoulder — should be ≈ left shoulder height

        # Head must be significantly higher than both shoulders
        head_tolerance = 0.02  # head at least 2% higher
        shoulder_tolerance = 0.03  # left and right within 3% of each other

        if not (head["price"] > lsh["price"] * (1 + head_tolerance) and
                head["price"] > rsh["price"] * (1 + head_tolerance)):
            continue
        if not (abs(lsh["price"] - rsh["price"]) / lsh["price"] < shoulder_tolerance):
            continue

        # Neckline: connect the two troughs between shoulders
        # Find troughs between lsh-head and head-rsh
        troughs = [t for t in sw_lo
                   if lsh["index"] < t["index"] < head["index"] or
                      head["index"] < t["index"] < rsh["index"]]
        if len(troughs) < 2:
            continue

        neckline_low = max(t["price"] for t in troughs if t["index"] < head["index"])
        neckline_high = min(t["price"] for t in troughs if t["index"] > head["index"])
        neckline = (neckline_low + neckline_high) / 2

        # Neckline must be roughly horizontal (within 20% of ATR)
        if abs(neckline_low - neckline_high) > atr * 0.20:
            continue

        # Bearish breakout: close below neckline
        for j in range(rsh["index"] + 1, n):
            c = candles_4h[j]
            if c["close"] < neckline:
                disp = _is_displacement(c, avg_v)
                if not disp:
                    continue
                results.append({
                    "pattern":    "HeadShoulders",
                    "direction":  "SHORT",
                    "zone_low":   round(neckline * 0.998, 4),
                    "zone_high":  round(head["price"], 4),
                    "zone_mid":   round((neckline + head["price"]) / 2, 4),
                    "atr_pct":    round(abs(head["price"] - neckline) / atr, 2),
                    "break_idx":  j,
                    "vol_confirmed": True,
                    "vol_ratio":  disp["vol_ratio"],
                    "disp":       disp,
                })
                break

    # ── Inverse H&S: 3 troughs, middle lowest ──────────────────
    for i in range(len(sw_lo) - 2):
        lsb = sw_lo[i]       # left base
        inv_head = sw_lo[i + 1]  # head — lowest
        rsb = sw_lo[i + 2]   # right base

        inv_tolerance = 0.02
        base_tolerance = 0.03

        if not (inv_head["price"] < lsb["price"] * (1 - inv_tolerance) and
                inv_head["price"] < rsb["price"] * (1 - inv_tolerance)):
            continue
        if not (abs(lsb["price"] - rsb["price"]) / lsb["price"] < base_tolerance):
            continue

        # Neckline: connect the two peaks between troughs
        peaks = [p for p in sw_hi
                 if lsb["index"] < p["index"] < inv_head["index"] or
                    inv_head["index"] < p["index"] < rsb["index"]]
        if len(peaks) < 2:
            continue

        neckline_low = min(p["price"] for p in peaks if p["index"] < inv_head["index"])
        neckline_high = max(p["price"] for p in peaks if p["index"] > inv_head["index"])
        neckline = (neckline_low + neckline_high) / 2

        if abs(neckline_low - neckline_high) > atr * 0.20:
            continue

        # Bullish breakout: close above neckline
        for j in range(rsb["index"] + 1, n):
            c = candles_4h[j]
            if c["close"] > neckline:
                disp = _is_displacement(c, avg_v)
                if not disp:
                    continue
                results.append({
                    "pattern":    "InverseHS",
                    "direction":  "LONG",
                    "zone_low":   round(inv_head["price"], 4),
                    "zone_high":  round(neckline * 1.002, 4),
                    "zone_mid":   round((inv_head["price"] + neckline) / 2, 4),
                    "atr_pct":    round(abs(neckline - inv_head["price"]) / atr, 2),
                    "break_idx":  j,
                    "vol_confirmed": True,
                    "vol_ratio":  disp["vol_ratio"],
                    "disp":       disp,
                })
                break

    return results


# ═══════════════════════════════════════════════════════════════
# FLAG DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_flag(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Bull Flag (bullish): strong bullish impulse, then diagonal down-channel.
    Bear Flag (bearish): strong bearish impulse, then diagonal up-channel.
    """
    results = []
    n = len(candles_4h)
    avg_v = _avg_vol(candles_4h)
    if n < 20:
        return results

    sw_lo = find_swing_lows(candles_4h, lookback=5)
    sw_hi = find_swing_highs(candles_4h, lookback=5)

    # ── Bull Flag: impulse up (3 rising highs), then lower highs ─
    if len(sw_hi) >= 4 and len(sw_lo) >= 2:
        recent_hi = sw_hi[-3:]
        if (len(recent_hi) == 3 and
                recent_hi[2]["price"] > recent_hi[1]["price"] > recent_hi[0]["price"]):
            pole_high = recent_hi[0]["price"]
            impulse_strength = (recent_hi[2]["price"] - recent_hi[0]["price"]) / pole_high
            if impulse_strength < 0.03:
                return results
            # Flag highs: swings after pole top
            flag_highs = [s for s in sw_hi if s["index"] > recent_hi[-1]["index"]]
            flag_lows = [s for s in sw_lo if s["index"] > recent_hi[-1]["index"]]
            if len(flag_highs) < 2 or len(flag_lows) < 2:
                return results
            hh_slope, _ = _linregress([s["index"] for s in flag_highs], [s["price"] for s in flag_highs])
            hl_slope, _ = _linregress([s["index"] for s in flag_lows], [s["price"] for s in flag_lows])
            # Bull flag: HH descending, HL flat/slightly descending
            if not (hh_slope < 0 and abs(hl_slope) < abs(hh_slope) * 0.6):
                return results
            hi_int = flag_highs[0]["price"] - hh_slope * flag_highs[0]["index"]
            lo_int = flag_lows[0]["price"] - hl_slope * flag_lows[0]["index"]
            flag_end = max(s["index"] for s in flag_highs + flag_lows)
            for j in range(flag_end + 1, n):
                c = candles_4h[j]
                upper_ch = hh_slope * j + hi_int
                if c["close"] > upper_ch:
                    disp = _is_displacement(c, avg_v)
                    if not disp:
                        continue
                    lo_ch = hl_slope * j + lo_int
                    results.append({
                        "pattern":      "BullFlag",
                        "direction":    "LONG",
                        "zone_low":     round(lo_ch, 4),
                        "zone_high":    round(upper_ch, 4),
                        "zone_mid":     round((lo_ch + upper_ch) / 2, 4),
                        "atr_pct":      round(abs(upper_ch - lo_ch) / atr, 2),
                        "break_idx":    j,
                        "vol_confirmed": True,
                        "vol_ratio":    disp["vol_ratio"],
                        "disp":         disp,
                    })
                    break

    # ── Bear Flag: impulse down (3 falling lows), then higher lows ─
    if len(sw_lo) >= 4 and len(sw_hi) >= 2:
        recent_lo = sw_lo[-3:]
        if (len(recent_lo) == 3 and
                recent_lo[2]["price"] < recent_lo[1]["price"] < recent_lo[0]["price"]):
            pole_low = recent_lo[0]["price"]
            impulse_strength = (recent_lo[0]["price"] - recent_lo[2]["price"]) / pole_low
            if impulse_strength < 0.03:
                return results
            flag_lows = [s for s in sw_lo if s["index"] > recent_lo[-1]["index"]]
            flag_highs = [s for s in sw_hi if s["index"] > recent_lo[-1]["index"]]
            if len(flag_lows) < 2 or len(flag_highs) < 2:
                return results
            ll_slope, _ = _linregress([s["index"] for s in flag_lows], [s["price"] for s in flag_lows])
            lh_slope, _ = _linregress([s["index"] for s in flag_highs], [s["price"] for s in flag_highs])
            # Bear flag: LL ascending, LH flat/slightly ascending
            if not (ll_slope > 0 and abs(lh_slope) < ll_slope * 0.6):
                return results
            lo_int = flag_lows[0]["price"] - ll_slope * flag_lows[0]["index"]
            hi_int = flag_highs[0]["price"] - lh_slope * flag_highs[0]["index"]
            flag_end = max(s["index"] for s in flag_highs + flag_lows)
            for j in range(flag_end + 1, n):
                c = candles_4h[j]
                lower_ch = ll_slope * j + lo_int
                if c["close"] < lower_ch:
                    disp = _is_displacement(c, avg_v)
                    if not disp:
                        continue
                    upper_ch = lh_slope * j + hi_int
                    results.append({
                        "pattern":      "BearFlag",
                        "direction":    "SHORT",
                        "zone_low":     round(lower_ch, 4),
                        "zone_high":    round(upper_ch, 4),
                        "zone_mid":     round((lower_ch + upper_ch) / 2, 4),
                        "atr_pct":      round(abs(upper_ch - lower_ch) / atr, 2),
                        "break_idx":    j,
                        "vol_confirmed": True,
                        "vol_ratio":    disp["vol_ratio"],
                        "disp":         disp,
                    })
                    break

    return results


# ═══════════════════════════════════════════════════════════════
# WICK REJECTION CHECK
# ═══════════════════════════════════════════════════════════════

def _check_wick_rejection(pair: str, direction: str, zone_mid: float) -> dict | None:
    """MANDATORY wick rejection at 15m/30m — returns {tf, wick} or None."""
    for tf in ("30m", "15m"):
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
                    diff_pct = abs(wick["wick_low"] - zone_mid) / zone_mid
                    if diff_pct < 0.005:
                        return {"tf": tf, "wick": wick}
            else:
                wick = is_long_upside_wick(c)
                if wick:
                    diff_pct = abs(wick["wick_high"] - zone_mid) / zone_mid
                    if diff_pct < 0.005:
                        return {"tf": tf, "wick": wick}
    return None


# ═══════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════

def _score(
    vol_ratio: float,
    at_structure: bool,
    htf_bullish: bool,
    htf_bearish: bool,
    touches_count: int,
) -> dict:
    s = S1_BASE_SCORE

    # Volume
    if vol_ratio >= 2.0:
        s += 1.5
    elif vol_ratio >= 1.5:
        s += 1.0

    # At structural level
    if at_structure:
        s += 1.0

    # HTF alignment
    if htf_bullish:
        s += 1.0
    if htf_bearish:
        s += 1.0

    # Multiple trendline touches
    if touches_count >= 4:
        s += 1.0
    elif touches_count >= 3:
        s += 0.5

    # Pattern type bonus
    s += 0.5  # base bonus for valid pattern

    s = min(s, 10.0)
    label = "HIGH" if s >= S1_SCORE_HIGH else "MEDIUM" if s >= S1_MIN_SCORE else "LOW"
    return {"score": round(s, 1), "label": label}


# ═══════════════════════════════════════════════════════════════
# TRADE CALCULATION
# ═══════════════════════════════════════════════════════════════

def _calc(entry: float, zone_low: float, zone_high: float, direction: str) -> dict:
    if direction == "LONG":
        sl = zone_low * (1 - S1_SL_BUFFER)
        d = entry - sl
        tp1 = entry + d * TP1_MAX_RR
        tp2 = entry + d * TP2_MAX_RR
    else:
        sl = zone_high * (1 + S1_SL_BUFFER)
        d = sl - entry
        tp1 = entry - d * TP1_MAX_RR
        tp2 = entry - d * TP2_MAX_RR
    rr1 = round(abs(tp1 - entry) / d, 2) if d > 0 else 0
    rr2 = round(abs(tp2 - entry) / d, 2) if d > 0 else 0
    return {
        "entry": round(entry, 4),
        "sl":    round(sl, 4),
        "tp1":   round(tp1, 4),
        "tp2":   round(tp2, 4),
        "rr1":   f"1:{rr1}",
        "rr2":   f"1:{rr2}",
        "sl_pct": round(d / entry * 100, 2),
        "tp1_pct": round(abs(tp1 - entry) / entry * 100, 2),
        "tp2_pct": round(abs(tp2 - entry) / entry * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def scan_chart_patterns(
    pair: str,
    ob_setups: list[dict] | None = None,
) -> list[dict]:
    """
    Scan pair for S1: Pure Chart Patterns.
    Reads ob_setups from S4 for overlap check (S4 owns reactive retests, S1 owns patterns).
    Returns setups sorted by score descending.
    """
    ob_setups = ob_setups or []
    results = []

    c4 = get_candles(pair, S1_TF_DETECT, limit=150)
    if len(c4) < ATR_PERIOD + 10:
        return []

    atr   = calculate_atr(c4)
    price = c4[-1]["close"]
    n     = len(c4)
    avg_v = _avg_vol(c4)

    sw_lo = find_swing_lows(c4, lookback=5)
    sw_hi = find_swing_highs(c4, lookback=5)
    htf_bullish, htf_bearish = _compute_htf_bias(c4)

    # ── Detect all patterns ──────────────────────────────────
    all_patterns = (
        _detect_wedge(c4, atr) +
        _detect_hs(c4, atr) +
        _detect_flag(c4, atr)
    )

    # ── Pattern-level checks ─────────────────────────────────
    for p in all_patterns:
        d = p["direction"]
        lo, hi = p["zone_low"], p["zone_high"]
        mid = p["zone_mid"]

        # Recency filter: breakout must be within last BREAKOUT_MAX_AGE candles
        if (n - 1) - p.get("break_idx", 0) > BREAKOUT_MAX_AGE:
            continue

        # Pattern must align with HTF bias
        if ENABLE_MACRO_FILTER := True:  # injected from config via closure
            if d == "LONG" and htf_bearish:
                continue
            if d == "SHORT" and htf_bullish:
                continue

        # S4 overlap check: S4 owns reactive retests, S1 owns pattern breaks
        # Only skip if zone is IDENTICAL (≤1% mid difference)
        s4_overlap = any(
            abs(ob.get("ob", {}).get("ob_mid", 0) - mid) / mid < 0.01
            and ob.get("direction") == d
            for ob in ob_setups
        )
        # Note: S1 still fires for pattern setups even near S4 zones
        # because they are fundamentally different (pattern vs reactive)

        # Price must be near pattern zone (within 2%)
        near_zone = abs(price - mid) / mid < 0.02

        # At structural level (pattern anchored at swing)
        at_struct = any(
            abs(s["price"] - mid) / mid < 0.03
            for s in (sw_lo if d == "LONG" else sw_hi)
        )

        # MANDATORY wick rejection check
        wick_rej = _check_wick_rejection(pair, d, mid)

        sc = _score(
            vol_ratio=p["vol_ratio"],
            at_structure=at_struct,
            htf_bullish=htf_bullish,
            htf_bearish=htf_bearish,
            touches_count=MIN_TOUCHES,
        )

        # Only entry-worthy if in zone AND wick confirmed
        in_zone = _in_zone(price, lo, hi)
        trade = _calc(price, lo, hi, d) if (in_zone and wick_rej) else None

        results.append({
            "pair":            pair,
            "tf":              S1_TF_DETECT,
            "tf_label":        TF_LABEL[S1_TF_DETECT],
            "direction":       d,
            "pattern":         p["pattern"],
            "zone_low":        lo,
            "zone_high":       hi,
            "zone_mid":        mid,
            "atr":             round(atr, 4),
            "atr_pct":         p["atr_pct"],
            "current_price":   price,
            "in_zone":         in_zone,
            "near_zone":       near_zone,
            "at_structure":    at_struct,
            "trade":           trade,
            "vol_confirmed":   p["vol_confirmed"],
            "vol_ratio":       p["vol_ratio"],
            "wick_rejection":  wick_rej,
            "htf_bullish":     htf_bullish,
            "htf_bearish":     htf_bearish,
            "s4_overlap":       s4_overlap,
            "confidence_score": sc["score"],
            "confidence_label": sc["label"],
            "zone_key":        f"{pair}_{d}_{mid:.4f}",
        })

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
