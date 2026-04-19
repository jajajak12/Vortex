"""
Strategy 4: Order Block + Breaker Block (S4) — UPGRADED
========================================================
Detection: 4H | Confirmation: 1H | Entry: 30m
Min score: 8.0 | TP1 max 1:3.0 | TP2 max 1:4.8

S4 owns REACTIVE RETESTS at broken structure levels.
S5 owns COMPRESSION + ENGINEERED sweeps (aggressive, fast).
S6 owns MOMENTUM BREAKS + HOLD (NOT reactive retests).

Overlapping zone → S4 fires first, S6/S5 skip.

Flow:
  1. Order Block: candle 2-3 yang formed sebelum impulse move
     - Bullish OB: bearish candle sebelum bullish impulse
     - Bearish OB: bullish candle sebelum bearish impulse
  2. Breaker Block: struktur yang broken → retest dari sisi lain
  3. OB/BB harus sudah "touched/mitigated" (=price pernah revisit zona)
  4. Price retrace ke OB/BB zone = zona retest
  5. MANDATORY: wick rejection at 30m
  6. Entry ONLY saat reactive retest (NOT momentum break)

Hard Gates:
  - OB/BB sudah touched/mitigated sebelumnya
  - Price retrace INTI zona
  - Wick rejection at 30m: MANDATORY
  - Min score 8.0
  - Overlap check: writes to _seen_ob for S6/S5 handshake

Confluence (base 5.0):
  + Price inside zone:         +1.5
  + Previous momentum impulse:  +1.5
  + S1 liquidity overlap:       +1.0
  + S5 compression overlap:     +1.0
  + Volume confirmation:         +1.0
  + At key structural level:    +0.5
  + 2+ confluence:              +0.5
"""

from strategy1_liquidity import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
    _compute_htf_bias,
)
from config import ATR_PERIOD

S4_TF_DETECT  = "4h"
S4_TF_CONFIRM  = "1h"
S4_TF_ENTRY    = "30m"

OB_ATR_MIN     = 0.50
OB_TOUCH_LOOK  = 30
BB_ATR_MIN     = 0.60

S4_BASE_SCORE  = 5.0
S4_MIN_SCORE   = 8.0
S4_SCORE_HIGH  = 9.5

TP1_MAX_RR     = 3.0
TP2_MAX_RR     = 4.8
S4_SL_BUFFER   = 0.005

TF_LABEL = {"4h": "4H", "1h": "1H", "30m": "30m"}


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def _avg_vol(candles: list[dict], lookback: int = 20) -> float:
    n = len(candles)
    if n < 3:
        return 1.0
    start = max(1, n - lookback)
    vols = [c["volume"] for c in candles[start:n-1]]
    return sum(vols) / len(vols) if vols else 1.0


def _is_impulse(candle: dict, avg_vol: float, mult: float = 1.5) -> bool:
    total = candle["high"] - candle["low"]
    if total == 0:
        return False
    body = abs(candle["close"] - candle["open"])
    return candle["volume"] >= avg_vol * mult and (body / total) >= 0.6


def _is_in_zone(price: float, zone_low: float, zone_high: float,
                tol: float = 0.005) -> bool:
    return zone_low * (1 - tol) <= price <= zone_high * (1 + tol)


# ═══════════════════════════════════════════════════════════════
# ORDER BLOCK DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_order_blocks(candles_4h: list[dict], atr: float) -> list[dict]:
    results = []
    avg_v   = _avg_vol(candles_4h)
    n       = len(candles_4h)

    for i in range(3, n - 2):
        c_prev = candles_4h[i - 1]
        c_ob   = candles_4h[i]
        c_imp  = candles_4h[i + 1]

        prev_bear = c_prev["close"] < c_prev["open"]
        imp_bull  = c_imp["close"] > c_imp["open"]
        imp_bear  = c_imp["close"] < c_imp["open"]
        prev_bull = c_prev["close"] > c_prev["open"]

        if prev_bear and imp_bull and _is_impulse(c_imp, avg_v):
            lo = min(c_ob["open"], c_ob["close"])
            hi = max(c_ob["open"], c_ob["close"])
            sz = hi - lo
            if sz >= atr * OB_ATR_MIN:
                results.append({
                    "type": "OrderBlock", "direction": "LONG",
                    "ob_low": round(lo, 4), "ob_high": round(hi, 4),
                    "ob_mid": round((lo + hi) / 2, 4),
                    "ob_size": round(sz, 4),
                    "atr_pct": round(sz / atr, 2),
                    "ob_idx": i, "imp_idx": i + 1,
                })

        if prev_bull and imp_bear and _is_impulse(c_imp, avg_v):
            lo = min(c_ob["open"], c_ob["close"])
            hi = max(c_ob["open"], c_ob["close"])
            sz = hi - lo
            if sz >= atr * OB_ATR_MIN:
                results.append({
                    "type": "OrderBlock", "direction": "SHORT",
                    "ob_low": round(lo, 4), "ob_high": round(hi, 4),
                    "ob_mid": round((lo + hi) / 2, 4),
                    "ob_size": round(sz, 4),
                    "atr_pct": round(sz / atr, 2),
                    "ob_idx": i, "imp_idx": i + 1,
                })

    return results


# ═══════════════════════════════════════════════════════════════
# BREAKER BLOCK DETECTION
# ═══════════════════════════════════════════════════════════════

def _detect_breaker_blocks(candles_4h: list[dict], atr: float) -> list[dict]:
    results = []
    avg_v   = _avg_vol(candles_4h)
    n       = len(candles_4h)
    sw_lo   = find_swing_lows(candles_4h, lookback=10)
    sw_hi   = find_swing_highs(candles_4h, lookback=10)

    for sl in sw_lo[-5:]:
        sp, si = sl["price"], sl["index"]
        broken = any(c["close"] < sp for c in candles_4h[si+1:min(si+OB_TOUCH_LOOK, n)])
        if not broken:
            continue
        for c in candles_4h[si+1:min(si+OB_TOUCH_LOOK, n)]:
            if c["low"] <= sp and c["close"] > sp:
                lo, hi = round(sp * 0.998, 4), round(c["close"], 4)
                sz = hi - lo
                if sz >= atr * BB_ATR_MIN:
                    results.append({
                        "type": "BreakerBlock", "direction": "LONG",
                        "ob_low": lo, "ob_high": hi,
                        "ob_mid": round((lo + hi) / 2, 4),
                        "ob_size": round(sz, 4),
                        "atr_pct": round(sz / atr, 2),
                        "ob_idx": si, "imp_idx": si,
                    })
                break

    for sh in sw_hi[-5:]:
        sp, si = sh["price"], sh["index"]
        broken = any(c["close"] > sp for c in candles_4h[si+1:min(si+OB_TOUCH_LOOK, n)])
        if not broken:
            continue
        for c in candles_4h[si+1:min(si+OB_TOUCH_LOOK, n)]:
            if c["high"] >= sp and c["close"] < sp:
                lo, hi = round(c["close"], 4), round(sp * 1.002, 4)
                sz = hi - lo
                if sz >= atr * BB_ATR_MIN:
                    results.append({
                        "type": "BreakerBlock", "direction": "SHORT",
                        "ob_low": lo, "ob_high": hi,
                        "ob_mid": round((lo + hi) / 2, 4),
                        "ob_size": round(sz, 4),
                        "atr_pct": round(sz / atr, 2),
                        "ob_idx": si, "imp_idx": si,
                    })
                break

    return results


# ═══════════════════════════════════════════════════════════════
# TOUCH CHECK
# ═══════════════════════════════════════════════════════════════

def _was_touched(candles_4h: list[dict], lo: float, hi: float, start: int) -> bool:
    for c in candles_4h[start+1:]:
        if lo <= c["close"] <= hi:
            return True
    return False


# ═══════════════════════════════════════════════════════════════
# SCORING
# ═══════════════════════════════════════════════════════════════

def _score(obs_in_zone: bool, has_s1: bool, has_s5: bool,
           vol_ok: bool, at_struct: bool) -> dict:
    s = S4_BASE_SCORE
    if obs_in_zone:  s += 1.5
    if has_s1:       s += 1.0
    if has_s5:       s += 1.0
    if vol_ok:       s += 1.0
    if at_struct:    s += 0.5
    if sum([has_s1, has_s5]) >= 2: s += 0.5
    s = min(s, 10.0)
    lbl = "HIGH" if s >= S4_SCORE_HIGH else "MEDIUM" if s >= S4_MIN_SCORE else "LOW"
    return {"score": round(s, 1), "label": lbl}


# ═══════════════════════════════════════════════════════════════
# TRADE CALC
# ═══════════════════════════════════════════════════════════════

def _calc(entry: float, lo: float, hi: float, direction: str) -> dict:
    if direction == "LONG":
        sl = lo * (1 - S4_SL_BUFFER)
        d  = entry - sl
        tp1, tp2 = entry + d * TP1_MAX_RR, entry + d * TP2_MAX_RR
    else:
        sl = hi * (1 + S4_SL_BUFFER)
        d  = sl - entry
        tp1, tp2 = entry - d * TP1_MAX_RR, entry - d * TP2_MAX_RR
    rr1 = round(abs(tp1-entry)/d, 2) if d > 0 else 0
    rr2 = round(abs(tp2-entry)/d, 2) if d > 0 else 0
    return {
        "entry": round(entry,4), "sl": round(sl,4),
        "tp1": round(tp1,4), "tp2": round(tp2,4),
        "rr1": f"1:{rr1}", "rr2": f"1:{rr2}",
        "sl_pct": round(d/entry*100, 2),
        "tp1_pct": round(abs(tp1-entry)/entry*100, 2),
        "tp2_pct": round(abs(tp2-entry)/entry*100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# MAIN SCAN
# ═══════════════════════════════════════════════════════════════

def scan_order_blocks(
    pair: str,
    s1_zones: dict | None = None,
    engineered_setups: list[dict] | None = None,
) -> list[dict]:
    s1_zones = s1_zones or {}
    engineered_setups = engineered_setups or []
    results = []

    c4 = get_candles(pair, S4_TF_DETECT, limit=120)
    if len(c4) < ATR_PERIOD + 10:
        return []

    atr  = calculate_atr(c4)
    price = c4[-1]["close"]
    n     = len(c4)
    avg_v = _avg_vol(c4)

    all_obs = (_detect_order_blocks(c4, atr) +
               _detect_breaker_blocks(c4, atr))

    sw_lo = find_swing_lows(c4, lookback=10)
    sw_hi = find_swing_highs(c4, lookback=10)

    for ob in all_obs:
        d    = ob["direction"]
        lo, hi = ob["ob_low"], ob["ob_high"]
        mid   = ob["ob_mid"]

        touched = _was_touched(c4, lo, hi, ob["ob_idx"])
        in_zone = _is_in_zone(price, lo, hi)

        # S1 overlap — ≤2% zone proximity (tightened from 3%)
        has_s1 = any(
            abs((z["low"]+z["high"])/2 - mid)/mid < 0.02 and z["type"] == d
            for z in (s1_zones.get("LONG",[]) + s1_zones.get("SHORT",[]))
        )

        # S5 overlap
        has_s5 = any(
            (d == "LONG" and e.get("direction") == "LONG") or
            (d == "SHORT" and e.get("direction") == "SHORT")
            for e in engineered_setups
        )

        # Vol
        imp_c = c4[ob["imp_idx"]] if ob["imp_idx"] < n else c4[-1]
        vol_ok = imp_c["volume"] >= avg_v * 1.5

        # Structural
        at_struct = any(
            abs(s["price"] - mid)/mid < 0.02
            for s in (sw_lo if d == "LONG" else sw_hi)
        )

        # Only reactive retests: touched AND (in zone OR within 1% of zone)
        near_zone = abs(price - mid) / mid < 0.01
        if not (touched and (in_zone or near_zone)):
            continue

        sc    = _score(in_zone, has_s1, has_s5, vol_ok, at_struct)
        trade = _calc(price, lo, hi, d) if in_zone else None

        results.append({
            "pair": pair,
            "tf":   S4_TF_DETECT,
            "tf_label": TF_LABEL[S4_TF_DETECT],
            "direction": d,
            "type": ob["type"],
            "ob":   ob,
            "atr":  round(atr, 4),
            "current_price": price,
            "in_zone": in_zone,
            "was_touched": touched,
            "trade": trade,
            "has_s1": has_s1,
            "has_s5": has_s5,
            "vol_confirmed": vol_ok,
            "confidence_score": sc["score"],
            "confidence_label": sc["label"],
            "zone_key": f"{pair}_{d}_{mid:.4f}",
        })

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
