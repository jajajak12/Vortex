"""
S4+S6 Merged: Order Block / Breaker Block + BOS / MSS / CHOCH
=============================================================
Detection: 4H | Confirmation: 1H | Entry: 30m
Min score: 8.0 | TP1 max 1:3.0 | TP2 max 1:4.8

Replaces strategy4_orderblock.py + strategy6_bos_mss.py.

Entry modes:
  RETEST   — OB/BB reactive retest (ex-S4): price returns to OB/BB zone
  MOMENTUM — BOS/CHOCH momentum break (ex-S6): price breaks structure and holds

Priority: same zone_key → RETEST preferred over MOMENTUM.
S5 reads seen_ob to skip zones owned by this strategy.
"""

from strategy1_liquidity import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
    _compute_htf_bias,
)
from config import ATR_PERIOD, TF_ZONE

# ── Timeframes ────────────────────────────────────────────────
TF_DETECT  = TF_ZONE
TF_CONFIRM = "1h"
TF_ENTRY   = "30m"
TF_LABEL   = {"4h": "4H", "1h": "1H", "30m": "30m"}

# ── OB / BB thresholds ────────────────────────────────────────
OB_ATR_MIN    = 0.50   # min OB body size relative to ATR
BB_ATR_MIN    = 0.60   # min BB size relative to ATR
OB_TOUCH_LOOK = 30     # candles to check for prior touch
OB_AGE_LIMIT  = 40     # max age of OB candle (~1 week of 4H data)

# ── BOS / CHOCH thresholds ────────────────────────────────────
BOS_LOOKBACK     = 8
BOS_BODY_MIN     = 0.60   # displacement body / range (tightened from 0.55)
BOS_VOL_MIN      = 1.5
BOS_HOLD_CANDLES = 3      # candles that must hold beyond break level

# ── Scoring ───────────────────────────────────────────────────
BASE_SCORE = 5.0
MIN_SCORE  = 8.0
SCORE_HIGH = 9.5

# ── Trade ─────────────────────────────────────────────────────
TP1_MAX_RR = 3.0
TP2_MAX_RR = 4.8
SL_BUFFER  = 0.005


# ═══════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

def _avg_vol(candles: list[dict], lookback: int = 20) -> float:
    n = len(candles)
    if n < 3:
        return 1.0
    start = max(1, n - lookback)
    vols = [c["volume"] for c in candles[start:n - 1]]
    return sum(vols) / len(vols) if vols else 1.0


def _is_impulse(candle: dict, avg_vol: float, body_min: float = 0.55,
                vol_min: float = 1.5) -> bool:
    total = candle["high"] - candle["low"]
    if total == 0:
        return False
    body = abs(candle["close"] - candle["open"])
    return (candle["volume"] >= avg_vol * vol_min and
            (body / total) >= body_min)


def _is_in_zone(price: float, lo: float, hi: float,
                tol: float = 0.005) -> bool:
    return lo * (1 - tol) <= price <= hi * (1 + tol)


def _calc(entry: float, zone_low: float, zone_high: float,
          direction: str) -> dict:
    if direction == "LONG":
        sl = zone_low * (1 - SL_BUFFER)
        d  = entry - sl
        tp1, tp2 = entry + d * TP1_MAX_RR, entry + d * TP2_MAX_RR
    else:
        sl = zone_high * (1 + SL_BUFFER)
        d  = sl - entry
        tp1, tp2 = entry - d * TP1_MAX_RR, entry - d * TP2_MAX_RR
    rr1 = round(abs(tp1 - entry) / d, 2) if d > 0 else 0
    rr2 = round(abs(tp2 - entry) / d, 2) if d > 0 else 0
    return {
        "entry": round(entry, 4), "sl": round(sl, 4),
        "tp1":   round(tp1, 4),   "tp2": round(tp2, 4),
        "rr1":   f"1:{rr1}",      "rr2": f"1:{rr2}",
        "sl_pct":  round(d / entry * 100, 2),
        "tp1_pct": round(abs(tp1 - entry) / entry * 100, 2),
        "tp2_pct": round(abs(tp2 - entry) / entry * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# ORDER BLOCK DETECTION  (ex-S4)
# ═══════════════════════════════════════════════════════════════

def _detect_order_blocks(candles: list[dict], atr: float) -> list[dict]:
    results = []
    avg_v = _avg_vol(candles)
    n = len(candles)

    for i in range(max(3, n - OB_AGE_LIMIT - 2), n - 2):
        c_ob  = candles[i]
        c_imp = candles[i + 1]
        c_pre = candles[i - 1]

        pre_bear = c_pre["close"] < c_pre["open"]
        pre_bull = c_pre["close"] > c_pre["open"]
        imp_bull = c_imp["close"] > c_imp["open"]
        imp_bear = c_imp["close"] < c_imp["open"]

        # Bullish OB: bearish candle before bullish impulse
        if pre_bear and imp_bull and _is_impulse(c_imp, avg_v):
            lo = min(c_ob["open"], c_ob["close"])
            hi = max(c_ob["open"], c_ob["close"])
            sz = hi - lo
            if sz >= atr * OB_ATR_MIN:
                results.append({
                    "entry_mode": "RETEST",
                    "ob_type":    "OrderBlock",
                    "direction":  "LONG",
                    "ob_low":     round(lo, 4),
                    "ob_high":    round(hi, 4),
                    "ob_mid":     round((lo + hi) / 2, 4),
                    "ob_size":    round(sz, 4),
                    "atr_pct":    round(sz / atr, 2),
                    "ob_idx":     i,
                    "imp_idx":    i + 1,
                    "vol_ok":     _is_impulse(c_imp, avg_v),
                })

        # Bearish OB: bullish candle before bearish impulse
        if pre_bull and imp_bear and _is_impulse(c_imp, avg_v):
            lo = min(c_ob["open"], c_ob["close"])
            hi = max(c_ob["open"], c_ob["close"])
            sz = hi - lo
            if sz >= atr * OB_ATR_MIN:
                results.append({
                    "entry_mode": "RETEST",
                    "ob_type":    "OrderBlock",
                    "direction":  "SHORT",
                    "ob_low":     round(lo, 4),
                    "ob_high":    round(hi, 4),
                    "ob_mid":     round((lo + hi) / 2, 4),
                    "ob_size":    round(sz, 4),
                    "atr_pct":    round(sz / atr, 2),
                    "ob_idx":     i,
                    "imp_idx":    i + 1,
                    "vol_ok":     _is_impulse(c_imp, avg_v),
                })

    return results


# ═══════════════════════════════════════════════════════════════
# BREAKER BLOCK DETECTION  (ex-S4)
# ═══════════════════════════════════════════════════════════════

def _detect_breaker_blocks(candles: list[dict], atr: float) -> list[dict]:
    results = []
    n     = len(candles)
    sw_lo = find_swing_lows(candles, lookback=10)
    sw_hi = find_swing_highs(candles, lookback=10)

    for sl in sw_lo[-5:]:
        sp, si = sl["price"], sl["index"]
        broken = any(c["close"] < sp for c in candles[si + 1:min(si + OB_TOUCH_LOOK, n)])
        if not broken:
            continue
        for c in candles[si + 1:min(si + OB_TOUCH_LOOK, n)]:
            if c["low"] <= sp and c["close"] > sp:
                lo, hi = round(sp * 0.998, 4), round(c["close"], 4)
                sz = hi - lo
                if sz >= atr * BB_ATR_MIN:
                    results.append({
                        "entry_mode": "RETEST",
                        "ob_type":    "BreakerBlock",
                        "direction":  "LONG",
                        "ob_low":     lo, "ob_high": hi,
                        "ob_mid":     round((lo + hi) / 2, 4),
                        "ob_size":    round(sz, 4),
                        "atr_pct":    round(sz / atr, 2),
                        "ob_idx":     si, "imp_idx": si,
                        "vol_ok":     True,
                    })
                break

    for sh in sw_hi[-5:]:
        sp, si = sh["price"], sh["index"]
        broken = any(c["close"] > sp for c in candles[si + 1:min(si + OB_TOUCH_LOOK, n)])
        if not broken:
            continue
        for c in candles[si + 1:min(si + OB_TOUCH_LOOK, n)]:
            if c["high"] >= sp and c["close"] < sp:
                lo, hi = round(c["close"], 4), round(sp * 1.002, 4)
                sz = hi - lo
                if sz >= atr * BB_ATR_MIN:
                    results.append({
                        "entry_mode": "RETEST",
                        "ob_type":    "BreakerBlock",
                        "direction":  "SHORT",
                        "ob_low":     lo, "ob_high": hi,
                        "ob_mid":     round((lo + hi) / 2, 4),
                        "ob_size":    round(sz, 4),
                        "atr_pct":    round(sz / atr, 2),
                        "ob_idx":     si, "imp_idx": si,
                        "vol_ok":     True,
                    })
                break

    return results


def _was_touched(candles: list[dict], lo: float, hi: float,
                 start: int) -> bool:
    for c in candles[start + 1:]:
        if lo <= c["close"] <= hi:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# BOS / MSS DETECTION  (ex-S6)
# ═══════════════════════════════════════════════════════════════

def _detect_bos(candles: list[dict], atr: float) -> list[dict]:
    results = []
    n     = len(candles)
    avg_v = _avg_vol(candles)

    sw_lo = find_swing_lows(candles, lookback=BOS_LOOKBACK)
    sw_hi = find_swing_highs(candles, lookback=BOS_LOOKBACK)

    for sl in sw_lo[-4:]:
        sl_p, sl_i = sl["price"], sl["index"]
        if sl_i >= n - 2:
            continue
        for i in range(sl_i + 1, min(sl_i + BOS_LOOKBACK, n)):
            c = candles[i]
            if c["close"] <= sl_p:
                continue
            if not _is_impulse(c, avg_v, BOS_BODY_MIN, BOS_VOL_MIN):
                continue
            holds = all(
                candles[j]["close"] > sl_p
                for j in range(i + 1, min(i + BOS_HOLD_CANDLES + 1, n))
            )
            if not holds:
                continue
            results.append({
                "entry_mode":    "MOMENTUM",
                "ob_type":       "BOS",
                "direction":     "LONG",
                "ob_low":        round(sl_p * 0.998, 4),
                "ob_high":       round(c["close"], 4),
                "ob_mid":        round((sl_p + c["close"]) / 2, 4),
                "ob_size":       round(c["close"] - sl_p, 4),
                "atr_pct":       round((c["close"] - sl_p) / atr, 2),
                "ob_idx":        i,
                "imp_idx":       i,
                "vol_ok":        True,
                "holds_beyond":  holds,
                "vol_ratio":     round(c["volume"] / avg_v, 2),
                "break_level":   round(sl_p, 4),
            })
            break

    for sh in sw_hi[-4:]:
        sh_p, sh_i = sh["price"], sh["index"]
        if sh_i >= n - 2:
            continue
        for i in range(sh_i + 1, min(sh_i + BOS_LOOKBACK, n)):
            c = candles[i]
            if c["close"] >= sh_p:
                continue
            if not _is_impulse(c, avg_v, BOS_BODY_MIN, BOS_VOL_MIN):
                continue
            holds = all(
                candles[j]["close"] < sh_p
                for j in range(i + 1, min(i + BOS_HOLD_CANDLES + 1, n))
            )
            if not holds:
                continue
            results.append({
                "entry_mode":    "MOMENTUM",
                "ob_type":       "BOS",
                "direction":     "SHORT",
                "ob_low":        round(c["close"], 4),
                "ob_high":       round(sh_p * 1.002, 4),
                "ob_mid":        round((c["close"] + sh_p) / 2, 4),
                "ob_size":       round(sh_p - c["close"], 4),
                "atr_pct":       round((sh_p - c["close"]) / atr, 2),
                "ob_idx":        i,
                "imp_idx":       i,
                "vol_ok":        True,
                "holds_beyond":  holds,
                "vol_ratio":     round(c["volume"] / avg_v, 2),
                "break_level":   round(sh_p, 4),
            })
            break

    return results


def _detect_choch(candles: list[dict], atr: float) -> list[dict]:
    """Change of Character: HTF trendline break + hold."""
    results = []
    n     = len(candles)
    avg_v = _avg_vol(candles)

    if n < 20:
        return results

    sw_lo = find_swing_lows(candles, lookback=15)
    sw_hi = find_swing_highs(candles, lookback=15)

    def _slope(pts: list[dict]) -> tuple[float, float]:
        if len(pts) < 2:
            return 0.0, 0.0
        p1, p2 = pts[-3] if len(pts) >= 3 else pts[0], pts[-1]
        s = (p2["price"] - p1["price"]) / max(p2["index"] - p1["index"], 1)
        return s, p1["price"] - s * p1["index"]

    # Uptrend break → SHORT CHOCH
    if len(sw_lo) >= 3:
        sl, ic = _slope(sw_lo)
        for i in range(sw_lo[-1]["index"], min(sw_lo[-1]["index"] + 5, n)):
            c = candles[i]
            expected = sl * i + ic
            if c["close"] < expected - atr * 0.3:
                if _is_impulse(c, avg_v, BOS_BODY_MIN, BOS_VOL_MIN):
                    lo = round(c["close"], 4)
                    hi = round(expected + atr * 0.5, 4)
                    results.append({
                        "entry_mode":   "MOMENTUM",
                        "ob_type":      "CHOCH",
                        "direction":    "SHORT",
                        "ob_low":       lo,
                        "ob_high":      hi,
                        "ob_mid":       round((lo + hi) / 2, 4),
                        "ob_size":      round(hi - lo, 4),
                        "atr_pct":      round((hi - lo) / atr, 2),
                        "ob_idx":       i,
                        "imp_idx":      i,
                        "vol_ok":       True,
                        "holds_beyond": False,
                        "vol_ratio":    round(c["volume"] / avg_v, 2),
                        "is_choch":     True,
                    })

    # Downtrend break → LONG CHOCH
    if len(sw_hi) >= 3:
        sl, ic = _slope(sw_hi)
        for i in range(sw_hi[-1]["index"], min(sw_hi[-1]["index"] + 5, n)):
            c = candles[i]
            expected = sl * i + ic
            if c["close"] > expected + atr * 0.3:
                if _is_impulse(c, avg_v, BOS_BODY_MIN, BOS_VOL_MIN):
                    lo = round(expected - atr * 0.5, 4)
                    hi = round(c["close"], 4)
                    results.append({
                        "entry_mode":   "MOMENTUM",
                        "ob_type":      "CHOCH",
                        "direction":    "LONG",
                        "ob_low":       lo,
                        "ob_high":      hi,
                        "ob_mid":       round((lo + hi) / 2, 4),
                        "ob_size":      round(hi - lo, 4),
                        "atr_pct":      round((hi - lo) / atr, 2),
                        "ob_idx":       i,
                        "imp_idx":      i,
                        "vol_ok":       True,
                        "holds_beyond": False,
                        "vol_ratio":    round(c["volume"] / avg_v, 2),
                        "is_choch":     True,
                    })

    return results


# ═══════════════════════════════════════════════════════════════
# UNIFIED SCORING
# ═══════════════════════════════════════════════════════════════

def _score(
    entry_mode: str,
    in_zone: bool,
    holds_beyond: bool,
    is_choch: bool,
    vol_ratio: float,
    has_s1: bool,
    has_s3_or_s5: bool,
    at_struct: bool,
) -> dict:
    s = BASE_SCORE

    if entry_mode == "RETEST":
        if in_zone:       s += 1.5
        if has_s1:        s += 1.0
        if has_s3_or_s5:  s += 1.0
        if vol_ratio >= 1.5: s += 1.0
        if at_struct:     s += 0.5
        if sum([has_s1, has_s3_or_s5]) >= 2: s += 0.5
    else:  # MOMENTUM
        if holds_beyond:  s += 2.0
        if is_choch:      s += 1.5
        if vol_ratio >= 2.0: s += 1.0
        elif vol_ratio >= 1.5: s += 0.5
        if has_s1:        s += 1.0
        if has_s3_or_s5:  s += 1.0
        if at_struct:     s += 0.5
        if sum([is_choch, has_s1, has_s3_or_s5]) >= 2: s += 0.5

    s = min(s, 10.0)
    lbl = "HIGH" if s >= SCORE_HIGH else "MEDIUM" if s >= MIN_SCORE else "LOW"
    return {"score": round(s, 1), "label": lbl}


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def scan_ob_bos(
    pair: str,
    s1_zones: dict | None = None,
    fvg_setups: list[dict] | None = None,
    eng_setups: list[dict] | None = None,
    seen_ob: set | None = None,
) -> list[dict]:
    """
    Unified S4+S6 scan: OB/BB reactive retests + BOS/CHOCH momentum breaks.
    Same zone_key → RETEST wins over MOMENTUM.
    seen_ob: zones already owned (skip them).
    """
    s1_zones   = s1_zones   or {}
    fvg_setups = fvg_setups or []
    eng_setups = eng_setups or []
    seen_ob    = seen_ob    or set()

    c4 = get_candles(pair, TF_DETECT, limit=120)
    if len(c4) < ATR_PERIOD + 10:
        return []

    atr   = calculate_atr(c4)
    price = c4[-1]["close"]
    n     = len(c4)
    avg_v = _avg_vol(c4)

    sw_lo = find_swing_lows(c4, lookback=10)
    sw_hi = find_swing_highs(c4, lookback=10)

    # Detect all raw setups
    retest_raw   = _detect_order_blocks(c4, atr) + _detect_breaker_blocks(c4, atr)
    momentum_raw = _detect_bos(c4, atr) + _detect_choch(c4, atr)

    # Filter RETEST: must have been touched AND price near/in zone
    retest_valid = []
    for ob in retest_raw:
        d, lo, hi = ob["direction"], ob["ob_low"], ob["ob_high"]
        mid = ob["ob_mid"]
        touched   = _was_touched(c4, lo, hi, ob["ob_idx"])
        in_zone   = _is_in_zone(price, lo, hi)
        near_zone = abs(price - mid) / mid < 0.01
        if not (touched and (in_zone or near_zone)):
            continue
        ob["in_zone"] = in_zone
        retest_valid.append(ob)

    # Filter MOMENTUM: must be in zone
    momentum_valid = []
    for ob in momentum_raw:
        lo, hi = ob["ob_low"], ob["ob_high"]
        in_zone = _is_in_zone(price, lo, hi)
        if not in_zone:
            continue
        ob["in_zone"] = in_zone
        momentum_valid.append(ob)

    # Merge, dedup by zone_key — RETEST wins
    by_key: dict[str, dict] = {}
    for ob in retest_valid + momentum_valid:
        d, mid = ob["direction"], ob["ob_mid"]
        key = f"{pair}_{d}_{mid:.4f}"
        if key in by_key and by_key[key]["entry_mode"] == "RETEST":
            continue  # RETEST already claimed this zone
        by_key[key] = ob

    results = []
    for zone_key, ob in by_key.items():
        # Skip if S4/S6 already fired for this zone in this scan cycle
        if zone_key in seen_ob:
            continue

        d     = ob["direction"]
        lo    = ob["ob_low"]
        hi    = ob["ob_high"]
        mid   = ob["ob_mid"]
        mode  = ob["entry_mode"]
        in_zone = ob.get("in_zone", False)

        # Confluence
        has_s1 = any(
            abs((z["low"] + z["high"]) / 2 - mid) / mid < 0.02 and z["type"] == d
            for z in (s1_zones.get("LONG", []) + s1_zones.get("SHORT", []))
        )
        has_s3 = any(
            d == f.get("direction") and
            abs(f.get("fvg", {}).get("fvg_mid", 0) - mid) / mid < 0.02
            for f in fvg_setups
        )
        has_s5 = any(
            d == e.get("direction")
            for e in eng_setups
        )
        at_struct = any(
            abs(sw["price"] - mid) / mid < 0.02
            for sw in (sw_lo if d == "LONG" else sw_hi)
        )

        sc = _score(
            mode,
            in_zone,
            ob.get("holds_beyond", False),
            ob.get("is_choch", False),
            ob.get("vol_ratio", 1.0),
            has_s1,
            has_s3 or has_s5,
            at_struct,
        )

        if sc["score"] < MIN_SCORE:
            continue

        trade = _calc(price, lo, hi, d) if in_zone else None

        results.append({
            "pair":             pair,
            "tf":               TF_DETECT,
            "tf_label":         TF_LABEL[TF_DETECT],
            "direction":        d,
            "type":             ob["ob_type"],
            "entry_mode":       mode,
            "ob":               ob,
            "atr":              round(atr, 4),
            "current_price":    price,
            "in_zone":          in_zone,
            "trade":            trade,
            "has_s1":           has_s1,
            "has_s3":           has_s3,
            "has_s5":           has_s5,
            "vol_confirmed":    ob.get("vol_ok", False),
            "vol_ratio":        ob.get("vol_ratio", 1.0),
            "mss":              ob.get("holds_beyond", False),
            "choch":            ob.get("is_choch", False),
            "confidence_score": sc["score"],
            "confidence_label": sc["label"],
            "zone_key":         zone_key,
        })

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
