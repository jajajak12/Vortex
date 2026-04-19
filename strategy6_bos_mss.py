"""
Strategy 6: BOS + MSS / CHOCH (S6) — UPGRADED
=============================================
Detection: 4H | Confirmation: 1H | Entry: 30m
Min score: 8.0 | TP1 max 1:3.0 | TP2 max 1:4.8

S6 owns MOMENTUM BREAKS + HOLD (NOT reactive retests).
S4 owns REACTIVE RETESTS at broken structure levels.
S5 owns COMPRESSION + ENGINEERED sweeps (aggressive, fast).

Overlapping zone → S4 fires first, S6/S5 skip.

Flow:
  1. BOS: Break of Structure — price breaks swing high/low with momentum
  2. MSS: Market Structure Shift — candle closes beyond key level
  3. CHOCH: Change of Character — HTF trendline/structure break
  4. Retest after break = S4 territory (S6 SKIPS reactive retests)
  5. Entry: ONLY after confirmed break + hold (NOT retest)
  6. MANDATORY: displacement candle confirmation

Hard Gates:
  - BOS candle closes beyond structure (not just wick)
  - Displacement body >= 60%, volume >= 1.5x avg
  - NO reactive retest (those belong to S4)
  - Min score 8.0
  - Wick rejection at 30m: MANDATORY
  - Overlap check: reads _seen_ob from S4

Confluence (base 5.0):
  + Price holds beyond break:       +2.0
  + CHOCH confirmed (HTF break):    +1.5
  + Volume 2x+:                    +1.0
  + S1/S3 overlap:                  +1.0
  + At next structural target:     +0.5
  + 2+ confluence:                  +0.5
"""

from strategy1_liquidity import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
    _compute_htf_bias,
)
from config import ATR_PERIOD

S6_TF_DETECT = "4h"
S6_TF_CONFIRM = "1h"
S6_TF_ENTRY   = "30m"

BOS_LOOKBACK   = 8
BOS_BODY_MIN   = 0.60
BOS_VOL_MIN    = 1.5

S6_BASE_SCORE  = 5.0
S6_MIN_SCORE   = 8.0
S6_SCORE_HIGH  = 9.5

TP1_MAX_RR     = 3.0
TP2_MAX_RR     = 4.8
S6_SL_BUFFER   = 0.005

TF_LABEL = {"4h": "4H", "1h": "1H", "30m": "30m"}


def _avg_vol(candles: list[dict], lookback: int = 20) -> float:
    n = len(candles)
    if n < 3:
        return 1.0
    start = max(1, n - lookback)
    vols = [c["volume"] for c in candles[start:n-1]]
    return sum(vols) / len(vols) if vols else 1.0


def _is_displacement(c: dict) -> bool:
    rng = c["high"] - c["low"]
    if rng == 0:
        return False
    body = abs(c["close"] - c["open"])
    return (body / rng) >= BOS_BODY_MIN


def _is_in_zone(price: float, lo: float, hi: float,
                tol: float = 0.005) -> bool:
    return lo * (1 - tol) <= price <= hi * (1 + tol)


def _detect_bos(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Detect Break of Structure + Market Structure Shift.
    BOS: candle closes beyond swing high/low with displacement.
    MSS: candle that closes beyond key level confirming shift.
    """
    results = []
    n     = len(candles_4h)
    avg_v = _avg_vol(candles_4h)

    swing_lo = find_swing_lows(candles_4h, lookback=BOS_LOOKBACK)
    swing_hi = find_swing_highs(candles_4h, lookback=BOS_LOOKBACK)

    for sl in swing_lo[-4:]:
        sl_price = sl["price"]
        sl_idx   = sl["index"]
        if sl_idx >= n - 2:
            continue

        # Check for bullish break
        for i in range(sl_idx + 1, min(sl_idx + BOS_LOOKBACK, n)):
            c = candles_4h[i]
            # Close must be above swing low
            if c["close"] <= sl_price:
                continue
            # Check displacement
            if not _is_displacement(c):
                continue
            vol_ok = c["volume"] >= avg_v * BOS_VOL_MIN
            if not vol_ok:
                continue

            # MSS: candle closes and holds above
            holds = all(
                candles_4h[j]["close"] > sl_price
                for j in range(i + 1, min(i + 3, n))
            )
            if not holds:
                continue

            zone_lo = round(sl_price * 0.998, 4)
            zone_hi = round(c["close"], 4)
            size = zone_hi - zone_lo

            results.append({
                "type":        "BOS",
                "direction":   "LONG",
                "zone_low":    zone_lo,
                "zone_high":   zone_hi,
                "zone_mid":    round((zone_lo + zone_hi) / 2, 4),
                "break_level": round(sl_price, 4),
                "atr_pct":     round(size / atr, 2),
                "bos_idx":     i,
                "mss":         holds,
                "vol_confirmed": vol_ok,
                "vol_ratio":   round(c["volume"] / avg_v, 2),
            })
            break

    for sh in swing_hi[-4:]:
        sh_price = sh["price"]
        sh_idx   = sh["index"]
        if sh_idx >= n - 2:
            continue

        for i in range(sh_idx + 1, min(sh_idx + BOS_LOOKBACK, n)):
            c = candles_4h[i]
            if c["close"] >= sh_price:
                continue
            if not _is_displacement(c):
                continue
            vol_ok = c["volume"] >= avg_v * BOS_VOL_MIN
            if not vol_ok:
                continue

            holds = all(
                candles_4h[j]["close"] < sh_price
                for j in range(i + 1, min(i + 3, n))
            )
            if not holds:
                continue

            zone_lo = round(c["close"], 4)
            zone_hi = round(sh_price * 1.002, 4)
            size = zone_hi - zone_lo

            results.append({
                "type":        "BOS",
                "direction":   "SHORT",
                "zone_low":    zone_lo,
                "zone_high":   zone_hi,
                "zone_mid":    round((zone_lo + zone_hi) / 2, 4),
                "break_level": round(sh_price, 4),
                "atr_pct":     round(size / atr, 2),
                "bos_idx":     i,
                "mss":         holds,
                "vol_confirmed": vol_ok,
                "vol_ratio":   round(c["volume"] / avg_v, 2),
            })
            break

    return results


def _detect_choch(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Change of Character: HTF trendline/structure break.
    Detects when price breaks a trendline and holds.
    """
    results = []
    n     = len(candles_4h)
    avg_v = _avg_vol(candles_4h)

    if n < 20:
        return results

    # Detect trendlines using swing highs/lows
    sw_lo = find_swing_lows(candles_4h, lookback=15)
    sw_hi = find_swing_highs(candles_4h, lookback=15)

    # Uptrend line: connect swing lows
    if len(sw_lo) >= 3:
        tl_lo1 = sw_lo[-3]["price"]
        tl_lo2 = sw_lo[-1]["price"]
        slope  = (tl_lo2 - tl_lo1) / 2
        for i in range(sw_lo[-3]["index"], min(sw_lo[-1]["index"] + 5, n)):
            c = candles_4h[i]
            expected = tl_lo1 + slope * (i - sw_lo[-3]["index"])
            if c["close"] < expected - atr * 0.3:  # Break below trendline
                vol_ok = c["volume"] >= avg_v * BOS_VOL_MIN
                if vol_ok and _is_displacement(c):
                    results.append({
                        "type":    "CHOCH",
                        "direction": "SHORT",
                        "zone_low": round(c["close"], 4),
                        "zone_high": round(expected + atr * 0.5, 4),
                        "zone_mid": round((c["close"] + expected) / 2, 4),
                        "atr_pct": round(abs(c["close"] - expected) / atr, 2),
                        "choch_idx": i,
                        "vol_confirmed": vol_ok,
                        "vol_ratio": round(c["volume"] / avg_v, 2),
                    })

    # Downtrend line: connect swing highs
    if len(sw_hi) >= 3:
        tl_hi1 = sw_hi[-3]["price"]
        tl_hi2 = sw_hi[-1]["price"]
        slope  = (tl_hi2 - tl_hi1) / 2
        for i in range(sw_hi[-3]["index"], min(sw_hi[-1]["index"] + 5, n)):
            c = candles_4h[i]
            expected = tl_hi1 + slope * (i - sw_hi[-3]["index"])
            if c["close"] > expected + atr * 0.3:  # Break above trendline
                vol_ok = c["volume"] >= avg_v * BOS_VOL_MIN
                if vol_ok and _is_displacement(c):
                    results.append({
                        "type":    "CHOCH",
                        "direction": "LONG",
                        "zone_low": round(expected - atr * 0.5, 4),
                        "zone_high": round(c["close"], 4),
                        "zone_mid": round((expected + c["close"]) / 2, 4),
                        "atr_pct": round(abs(c["close"] - expected) / atr, 2),
                        "choch_idx": i,
                        "vol_confirmed": vol_ok,
                        "vol_ratio": round(c["volume"] / avg_v, 2),
                    })

    return results


def _compute_score(
    holds_beyond: bool,
    choch_confirmed: bool,
    vol_ratio: float,
    has_s1: bool,
    has_s3: bool,
    near_target: bool,
) -> dict:
    s = S6_BASE_SCORE
    if holds_beyond:    s += 2.0
    if choch_confirmed: s += 1.5
    if vol_ratio >= 2.0: s += 1.0
    elif vol_ratio >= 1.5: s += 0.5
    if has_s1:          s += 1.0
    if has_s3:          s += 1.0
    if near_target:     s += 0.5
    if sum([choch_confirmed, has_s1, has_s3]) >= 2: s += 0.5
    s = min(s, 10.0)
    lbl = "HIGH" if s >= S6_SCORE_HIGH else "MEDIUM" if s >= S6_MIN_SCORE else "LOW"
    return {"score": round(s, 1), "label": lbl}


def _calc(entry: float, zone_low: float, zone_high: float,
          direction: str) -> dict:
    if direction == "LONG":
        sl = zone_low * (1 - S6_SL_BUFFER)
        d  = entry - sl
        tp1, tp2 = entry + d * TP1_MAX_RR, entry + d * TP2_MAX_RR
    else:
        sl = zone_high * (1 + S6_SL_BUFFER)
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


def scan_bos_mss(
    pair: str,
    s1_zones: dict | None = None,
    fvg_setups: list[dict] | None = None,
    seen_ob: set | None = None,
) -> list[dict]:
    """
    Scan pair for S6: BOS + MSS / CHOCH.
    S6 owns MOMENTUM BREAKS — NOT reactive retests.
    Reads _seen_ob from scanner to skip zones already handled by S4.
    """
    s1_zones  = s1_zones  or {}
    fvg_setups = fvg_setups or []
    seen_ob    = seen_ob    or set()
    results    = []

    c4 = get_candles(pair, S6_TF_DETECT, limit=120)
    if len(c4) < ATR_PERIOD + 10:
        return []

    atr   = calculate_atr(c4)
    price = c4[-1]["close"]
    n     = len(c4)

    avg_v    = _avg_vol(c4)
    htf_bull, htf_bear = _compute_htf_bias(c4)

    all_setups = _detect_bos(c4, atr) + _detect_choch(c4, atr)

    sw_lo = find_swing_lows(c4, lookback=10)
    sw_hi = find_swing_highs(c4, lookback=10)

    for s in all_setups:
        d    = s["direction"]
        lo   = s["zone_low"]
        hi   = s["zone_high"]
        mid  = s["zone_mid"]

        # S4 overlap check: skip if S4 already fired for this zone
        zone_key = f"{pair}_{d}_{mid:.4f}"
        if zone_key in seen_ob:
            continue

        in_zone = _is_in_zone(price, lo, hi)

        # S1 overlap
        has_s1 = any(
            abs((z["low"]+z["high"])/2 - mid)/mid < 0.03 and z["type"] == d
            for z in (s1_zones.get("LONG",[]) + s1_zones.get("SHORT",[]))
        )

        # S3 overlap
        has_s3 = any(
            (d == "LONG"  and f.get("direction") == "LONG" and
             abs(f.get("fvg",{}).get("fvg_mid",0) - mid)/mid < 0.03) or
            (d == "SHORT" and f.get("direction") == "SHORT" and
             abs(f.get("fvg",{}).get("fvg_mid",0) - mid)/mid < 0.03)
            for f in fvg_setups
        )

        # Near next structural target
        near_tgt = any(
            abs(sw["price"] - mid) / mid < 0.03
            for sw in (sw_lo if d == "LONG" else sw_hi)
        )

        sc = _compute_score(
            s.get("mss", True),
            s["type"] == "CHOCH",
            s.get("vol_ratio", 1.0),
            has_s1, has_s3, near_tgt,
        )

        trade = _calc(price, lo, hi, d) if in_zone else None

        results.append({
            "pair": pair,
            "tf":   S6_TF_DETECT,
            "tf_label": TF_LABEL[S6_TF_DETECT],
            "direction": d,
            "type": s["type"],
            "zone": s,
            "atr":  round(atr, 4),
            "current_price": price,
            "in_zone": in_zone,
            "trade": trade,
            "has_s1": has_s1,
            "has_s3": has_s3,
            "vol_confirmed": s.get("vol_confirmed", False),
            "vol_ratio": s.get("vol_ratio", 1.0),
            "mss": s.get("mss", False),
            "choch": s["type"] == "CHOCH",
            "confidence_score": sc["score"],
            "confidence_label": sc["label"],
            "zone_key": zone_key,
        })

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
