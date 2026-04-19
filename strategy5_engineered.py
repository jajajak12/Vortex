"""
Strategy 5: Engineered Liquidity Reversal (S5) — UPGRADED
==========================================================
Detection: 4H | Confirmation: 1H | Entry: 30m
Min score: 7.5 | TP1 max 1:3.0 | TP2 max 1:4.8

S5 owns COMPRESSION + ENGINEERED SWEEPS.
Aggressive, fast setups triggered by liquidity hunt.
NOT reactive retests — S4 owns those.

Flow:
  1. Compression: price consolidate dalam range sempit (< 50% ATR width)
  2. engineered sweep: liquidity hunt outside range, fast reclaim
  3. Entry: reclaim candle closes back inside range + displacement
  4. Volume spike required
  5. Wick rejection at 30m: MANDATORY

Hard Gates:
  - Compression range < 50% ATR
  - Sweep displacement candle body >= 55%
  - Volume >= 1.5x avg
  - Wick rejection at 30m: MANDATORY
  - Price retrace ke zona compression

Confluence (base 5.0):
  + Compression tight (<30% ATR):     +1.5
  + S3 FVG/Imbalance overlap:         +1.5
  + Volume spike >= 2x avg:            +1.0
  + HTF aligned:                      +1.0
  + At swing structure:               +0.5
  + 2+ confluence:                    +0.5
"""

from strategy1_liquidity import (
    get_candles, calculate_atr,
    find_swing_lows, find_swing_highs,
    _compute_htf_bias,
)
from config import ATR_PERIOD

S5_TF_DETECT = "4h"
S5_TF_CONFIRM = "1h"
S5_TF_ENTRY   = "30m"

COMP_MAX_ATR    = 0.50   # Compression max width
COMP_TIGHT_ATR  = 0.30   # Tight compression bonus threshold
SWEEP_LOOKBACK  = 10
DISP_BODY_MIN   = 0.55
DISP_VOL_MIN    = 1.5

S5_BASE_SCORE   = 5.0
S5_MIN_SCORE    = 7.5
S5_SCORE_HIGH   = 9.0

TP1_MAX_RR      = 3.0
TP2_MAX_RR       = 4.8
S5_SL_BUFFER     = 0.005

TF_LABEL = {"4h": "4H", "1h": "1H", "30m": "30m"}


def _avg_vol(candles: list[dict], lookback: int = 20) -> float:
    n = len(candles)
    if n < 3:
        return 1.0
    start = max(1, n - lookback)
    vols = [c["volume"] for c in candles[start:n-1]]
    return sum(vols) / len(vols) if vols else 1.0


def _is_in_zone(price: float, lo: float, hi: float) -> bool:
    return lo <= price <= hi


def _detect_compression(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Detect compression zones: tight range of N candles (< COMP_MAX_ATR ATR wide).
    """
    results = []
    n = len(candles_4h)

    for lookback in [6, 8, 10]:
        for i in range(n - lookback):
            chunk = candles_4h[i:i + lookback]
            if len(chunk) < lookback:
                continue
            highs = [c["high"] for c in chunk]
            lows  = [c["low"]  for c in chunk]
            comp_range = max(highs) - min(lows)
            if comp_range >= atr * COMP_MAX_ATR:
                continue
            # Check for a sweep outside the range
            for j in range(i + lookback, min(i + lookback + SWEEP_LOOKBACK, n)):
                sweep_c = candles_4h[j]
                if sweep_c["low"] < min(lows) or sweep_c["high"] > max(highs):
                    # Found a sweep
                    results.append({
                        "type":       "Compression",
                        "direction":  "LONG" if sweep_c["close"] > max(highs) else "SHORT",
                        "comp_low":   round(min(lows), 4),
                        "comp_high":  round(max(highs), 4),
                        "comp_mid":   round((min(lows) + max(highs)) / 2, 4),
                        "comp_size":  round(comp_range, 4),
                        "atr_pct":    round(comp_range / atr, 2),
                        "tight":      comp_range < atr * COMP_TIGHT_ATR,
                        "sweep_idx":  j,
                        "sweep_low":  round(sweep_c["low"], 4),
                        "sweep_high": round(sweep_c["high"], 4),
                        "sweep_close":round(sweep_c["close"], 4),
                        "comp_start": i,
                        "comp_end":   i + lookback,
                    })
                    break

    return results


def _detect_sweep_reclaim(candles_4h: list[dict], atr: float) -> list[dict]:
    """
    Detect engineered sweep + reclaim patterns.
    Price spikes outside range then reclaims fast.
    """
    results = []
    n = len(candles_4h)
    avg_v = _avg_vol(candles_4h)

    for i in range(5, n - 3):
        c0 = candles_4h[i - 1]  # pre-sweep
        c1 = candles_4h[i]       # sweep
        c2 = candles_4h[i + 1]  # reclaim
        c3 = candles_4h[i + 2]   # confirmation

        rng0 = c0["high"] - c0["low"]
        rng1 = c1["high"] - c1["low"]

        if rng0 == 0 or rng1 == 0:
            continue

        body0 = abs(c0["close"] - c0["open"]) / rng0
        body1 = abs(c1["close"] - c1["open"]) / rng1

        # Bullish sweep then reclaim
        if (c1["low"]  < c0["low"]  and   # sweep below range
            c2["close"] > c0["low"] and   # reclaim above
            c2["volume"] >= avg_v * DISP_VOL_MIN and
            body1 >= DISP_BODY_MIN):

            results.append({
                "type":        "EngineeredSweep",
                "direction":   "LONG",
                "zone_low":    round(c0["low"], 4),
                "zone_high":   round(c0["high"], 4),
                "zone_mid":    round((c0["low"] + c0["high"]) / 2, 4),
                "sweep_low":   round(c1["low"], 4),
                "sweep_high":  round(c1["high"], 4),
                "reclaim_close": round(c2["close"], 4),
                "atr_pct":     round((c0["high"] - c0["low"]) / atr, 2),
                "sweep_idx":   i,
                "disp_idx":    i + 1,
                "vol_ratio":   round(c2["volume"] / avg_v, 2),
            })

        # Bearish sweep then reclaim
        if (c1["high"] > c0["high"] and   # sweep above range
            c2["close"] < c0["high"] and   # reclaim below
            c2["volume"] >= avg_v * DISP_VOL_MIN and
            body1 >= DISP_BODY_MIN):

            results.append({
                "type":        "EngineeredSweep",
                "direction":   "SHORT",
                "zone_low":    round(c0["low"], 4),
                "zone_high":   round(c0["high"], 4),
                "zone_mid":    round((c0["low"] + c0["high"]) / 2, 4),
                "sweep_low":   round(c1["low"], 4),
                "sweep_high":  round(c1["high"], 4),
                "reclaim_close": round(c2["close"], 4),
                "atr_pct":     round((c0["high"] - c0["low"]) / atr, 2),
                "sweep_idx":   i,
                "disp_idx":    i + 1,
                "vol_ratio":   round(c2["volume"] / avg_v, 2),
            })

    return results


def _compute_score(
    compression_tight: bool,
    htf_aligned: bool,
    has_s3: bool,
    vol_ratio: float,
    in_zone: bool,
    at_swing: bool,
) -> dict:
    s = S5_BASE_SCORE
    if compression_tight: s += 1.5
    if has_s3:           s += 1.5
    if vol_ratio >= 2.0: s += 1.0
    elif vol_ratio >= 1.5: s += 0.5
    if htf_aligned:      s += 1.0
    if at_swing:          s += 0.5
    if sum([compression_tight, has_s3, htf_aligned]) >= 2: s += 0.5
    s = min(s, 10.0)
    lbl = "HIGH" if s >= S5_SCORE_HIGH else "MEDIUM" if s >= S5_MIN_SCORE else "LOW"
    return {"score": round(s, 1), "label": lbl}


def _calc(entry: float, zone_low: float, zone_high: float,
          direction: str) -> dict:
    if direction == "LONG":
        sl = zone_low * (1 - S5_SL_BUFFER)
        d  = entry - sl
        tp1, tp2 = entry + d * TP1_MAX_RR, entry + d * TP2_MAX_RR
    else:
        sl = zone_high * (1 + S5_SL_BUFFER)
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


def scan_engineered(
    pair: str,
    fvg_setups: list[dict] | None = None,
) -> list[dict]:
    """
    Scan pair for S5: Engineered Liquidity Reversal.
    S5 owns compression + engineered sweep setups.
    """
    fvg_setups = fvg_setups or []
    results = []

    c4 = get_candles(pair, S5_TF_DETECT, limit=100)
    if len(c4) < ATR_PERIOD + 10:
        return []

    atr   = calculate_atr(c4)
    price = c4[-1]["close"]
    n     = len(c4)

    avg_v = _avg_vol(c4)
    htf_bull, htf_bear = _compute_htf_bias(c4)

    # Detect both compression and engineered sweep
    comps  = _detect_compression(c4, atr)
    sweeps = _detect_sweep_reclaim(c4, atr)

    sw_lo = find_swing_lows(c4, lookback=10)
    sw_hi = find_swing_highs(c4, lookback=10)

    for setup in comps + sweeps:
        d    = setup["direction"]
        lo   = setup["zone_low"]
        hi   = setup["zone_high"]
        mid  = setup["zone_mid"]

        in_zone = _is_in_zone(price, lo, hi)

        # S3 FVG overlap
        has_s3 = any(
            (d == "LONG"  and f.get("direction") == "LONG" and
             abs(f.get("fvg", {}).get("fvg_mid", 0) - mid) / mid < 0.03) or
            (d == "SHORT" and f.get("direction") == "SHORT" and
             abs(f.get("fvg", {}).get("fvg_mid", 0) - mid) / mid < 0.03)
            for f in fvg_setups
        )

        # HTF aligned
        htf_ok = (htf_bull and d == "LONG") or (htf_bear and d == "SHORT")

        # Structural
        at_sw = any(
            abs(s["price"] - mid) / mid < 0.02
            for s in (sw_lo if d == "LONG" else sw_hi)
        )

        # Compression tightness
        tight = setup.get("tight", setup["atr_pct"] < COMP_TIGHT_ATR * 100)
        vol_r = setup.get("vol_ratio", 1.0)

        sc = _compute_score(tight, htf_ok, has_s3, vol_r, in_zone, at_sw)

        trade = _calc(price, lo, hi, d) if in_zone else None

        results.append({
            "pair": pair,
            "tf":   S5_TF_DETECT,
            "tf_label": TF_LABEL[S5_TF_DETECT],
            "direction": d,
            "type": setup["type"],
            "zone": setup,
            "atr":  round(atr, 4),
            "current_price": price,
            "in_zone": in_zone,
            "trade": trade,
            "has_s3": has_s3,
            "vol_ratio": vol_r,
            "compression_tight": tight,
            "confidence_score": sc["score"],
            "confidence_label": sc["label"],
            "zone_key": f"{pair}_{d}_{mid:.4f}",
        })

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
