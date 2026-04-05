"""
Strategy 3: FVG Reclaim after Liquidity Sweep
==============================================
Flow:
  1. Liquidity sweep: recent swing low/high ditembus lalu close kembali di atas/bawah
  2. Displacement: setelah sweep, candle kuat meninggalkan Bullish/Bearish FVG
     Bullish FVG: high[i+2] < low[i]   → zone = [high[i+2], low[i]]
     Bearish FVG: low[i+2]  > high[i]  → zone = [high[i],   low[i+2]]
  3. Reclaim: harga retrace ke FVG zone (entry zone)
  4. Konfirmasi: rejection candle di TF 5m (shared dengan Strat 1/2)

Confluence scoring (max 10):
  Base (sweep + FVG terbentuk) : 5
  HTF 4H searah setup          : +2
  Overlap dengan Strat 2 wick  : +2
  FVG size > 1× ATR            : +1

Signal hanya dikirim jika score >= STRAT3_MIN_SCORE (config, default 7).
"""

from strategy1_liquidity import (
    get_candles, find_swing_lows, find_swing_highs,
    _compute_htf_bias, calculate_atr,
)
from config import (
    ATR_PERIOD, FVG_TF, FVG_MIN_ATR_RATIO, FVG_LOOKBACK,
    SWEEP_LOOKBACK, STRAT3_SL_BUFFER,
)

# ── Score thresholds (scale 0–10) ─────────────────────────────
SCORE_HIGH   = 8   # ⭐⭐⭐ HIGH priority
SCORE_MEDIUM = 6   # ⭐⭐ MEDIUM priority

TF_LABEL = {
    "4h": "4H",
    "1d": "1D",
    "1w": "1W",
}


# ── FVG Detection ──────────────────────────────────────────────

def _detect_sweep_of_low(candles: list[dict], swing_idx: int) -> dict | None:
    """
    Deteksi liquidity sweep di bawah swing low.
    Syarat: candle low < swing_price AND close > swing_price (recovery).
    """
    swing_price = candles[swing_idx]["low"]
    for i in range(swing_idx + 1, len(candles)):
        c = candles[i]
        if c["low"] < swing_price and c["close"] > swing_price:
            return {
                "sweep_index": i,
                "sweep_low":   round(c["low"], 4),
                "swing_price": round(swing_price, 4),
            }
    return None


def _detect_sweep_of_high(candles: list[dict], swing_idx: int) -> dict | None:
    """
    Deteksi liquidity sweep di atas swing high.
    Syarat: candle high > swing_price AND close < swing_price (recovery).
    """
    swing_price = candles[swing_idx]["high"]
    for i in range(swing_idx + 1, len(candles)):
        c = candles[i]
        if c["high"] > swing_price and c["close"] < swing_price:
            return {
                "sweep_index": i,
                "sweep_high":  round(c["high"], 4),
                "swing_price": round(swing_price, 4),
            }
    return None


def _detect_bullish_fvg(candles: list[dict], start_idx: int, atr: float) -> dict | None:
    """
    Cari Bullish FVG setelah sweep: high[i+2] < low[i].
    FVG zone = [high[i+2], low[i]].
    Candle tengah (i+1) harus bullish (displacement).
    FVG harus lebih besar dari FVG_MIN_ATR_RATIO × ATR.
    """
    min_size = atr * FVG_MIN_ATR_RATIO
    end = min(start_idx + FVG_LOOKBACK, len(candles) - 2)

    for i in range(start_idx, end):
        c0, c1, c2 = candles[i], candles[i + 1], candles[i + 2]

        fvg_low  = c2["high"]
        fvg_high = c0["low"]

        if fvg_low >= fvg_high:
            continue
        if (fvg_high - fvg_low) < min_size:
            continue
        if c1["close"] <= c1["open"]:   # displacement harus bullish
            continue

        return {
            "fvg_low":   round(fvg_low, 4),
            "fvg_high":  round(fvg_high, 4),
            "fvg_mid":   round((fvg_low + fvg_high) / 2, 4),
            "fvg_size":  round(fvg_high - fvg_low, 4),
            "fvg_index": i,
        }
    return None


def _detect_bearish_fvg(candles: list[dict], start_idx: int, atr: float) -> dict | None:
    """
    Cari Bearish FVG setelah sweep: low[i+2] > high[i].
    FVG zone = [high[i], low[i+2]].
    Candle tengah harus bearish.
    """
    min_size = atr * FVG_MIN_ATR_RATIO
    end = min(start_idx + FVG_LOOKBACK, len(candles) - 2)

    for i in range(start_idx, end):
        c0, c1, c2 = candles[i], candles[i + 1], candles[i + 2]

        fvg_low  = c0["high"]
        fvg_high = c2["low"]

        if fvg_low >= fvg_high:
            continue
        if (fvg_high - fvg_low) < min_size:
            continue
        if c1["close"] >= c1["open"]:   # displacement harus bearish
            continue

        return {
            "fvg_low":   round(fvg_low, 4),
            "fvg_high":  round(fvg_high, 4),
            "fvg_mid":   round((fvg_low + fvg_high) / 2, 4),
            "fvg_size":  round(fvg_high - fvg_low, 4),
            "fvg_index": i,
        }
    return None


def _is_fvg_invalidated(fvg: dict, candles: list[dict], direction: str) -> bool:
    """
    FVG di-invalidate jika close price menembus zona sepenuhnya.
    LONG: close < fvg_low  → imbalance sudah terisi habis (bearish)
    SHORT: close > fvg_high → imbalance sudah terisi habis (bullish)
    """
    check_from = fvg["fvg_index"] + 3
    for c in candles[check_from:]:
        if direction == "LONG"  and c["close"] < fvg["fvg_low"]:
            return True
        if direction == "SHORT" and c["close"] > fvg["fvg_high"]:
            return True
    return False


def _is_in_fvg_zone(price: float, fvg: dict) -> bool:
    return fvg["fvg_low"] <= price <= fvg["fvg_high"]


# ── Confluence helpers ─────────────────────────────────────────

def _htf_aligned(candles: list[dict], direction: str) -> bool:
    """EMA50 4H searah setup."""
    return _compute_htf_bias(candles) == direction


def _wick_overlaps_fvg(wick_setups: list[dict], fvg: dict) -> bool:
    """
    Ada Strat 2 wick zone yang overlap dengan FVG zone?
    Overlap = wick_low <= fvg_high AND wick_50pct >= fvg_low.
    """
    for s in wick_setups:
        w = s["wick"]
        if w["wick_low"] <= fvg["fvg_high"] and w["wick_50pct"] >= fvg["fvg_low"]:
            return True
    return False


# ── Trade calculation ──────────────────────────────────────────

def calculate_fvg_trade(entry: float, sl_ref: float, fvg: dict, direction: str) -> dict:
    """
    LONG : SL di bawah sweep_low; TP1 = fvg_high; TP2 = 1:2 RR
    SHORT: SL di atas sweep_high; TP1 = fvg_low;  TP2 = 1:2 RR
    Minimum RR TP2 = 1:2 (by construction).
    """
    if direction == "LONG":
        sl      = sl_ref * (1 - STRAT3_SL_BUFFER)
        sl_dist = entry - sl
        tp1     = fvg["fvg_high"]
        tp2     = entry + sl_dist * 2
    else:
        sl      = sl_ref * (1 + STRAT3_SL_BUFFER)
        sl_dist = sl - entry
        tp1     = fvg["fvg_low"]
        tp2     = entry - sl_dist * 2

    rr1 = round(abs(tp1 - entry) / sl_dist, 2) if sl_dist > 0 else 0

    return {
        "entry":   round(entry, 4),
        "sl":      round(sl, 4),
        "tp1":     round(tp1, 4),
        "tp2":     round(tp2, 4),
        "rr1":     f"1:{rr1}",
        "rr2":     "1:2.0",
        "sl_pct":  round(sl_dist / entry * 100, 2),
        "tp1_pct": round(abs(tp1 - entry) / entry * 100, 2),
        "tp2_pct": round(abs(tp2 - entry) / entry * 100, 2),
    }


# ── Main scan function ─────────────────────────────────────────

def scan_fvg_setups(pair: str, wick_setups: list[dict] | None = None) -> list[dict]:
    """
    Scan pair untuk Strat 3: FVG Reclaim setelah Liquidity Sweep.

    wick_setups: hasil scan_wick_setups() untuk confluence cross-strategy.
    Return list setup diurutkan dari score tertinggi.
    """
    wick_setups = wick_setups or []
    results     = []

    candles = get_candles(pair, FVG_TF, limit=SWEEP_LOOKBACK + 30)
    if len(candles) < ATR_PERIOD + 10:
        return []

    atr           = calculate_atr(candles)
    current_price = candles[-1]["close"]
    n             = len(candles)

    # ── LONG setups ──────────────────────────────────────────
    for swing in find_swing_lows(candles, lookback=5):
        if swing["index"] < n - SWEEP_LOOKBACK:
            continue

        sweep = _detect_sweep_of_low(candles, swing["index"])
        if sweep is None:
            continue

        fvg = _detect_bullish_fvg(candles, sweep["sweep_index"], atr)
        if fvg is None:
            continue

        if _is_fvg_invalidated(fvg, candles, "LONG"):
            continue

        in_zone = _is_in_fvg_zone(current_price, fvg)

        score, notes = 5, []

        if _htf_aligned(candles, "LONG"):
            score += 2
            notes.append("✅ 4H HTF bullish (EMA50)")
        else:
            notes.append("⬜ 4H HTF tidak bullish")

        if _wick_overlaps_fvg(wick_setups, fvg):
            score += 2
            notes.append("✅ Overlap Strat 2 wick zone")
        else:
            notes.append("⬜ Tidak ada wick confluence")

        if atr > 0 and fvg["fvg_size"] > atr:
            score += 1
            notes.append(f"✅ FVG besar: {fvg['fvg_size']:.4f} > ATR {atr:.4f}")
        else:
            notes.append(f"⬜ FVG kecil: {fvg['fvg_size']:.4f} (ATR {atr:.4f})")

        label = ("⭐⭐⭐ HIGH"   if score >= SCORE_HIGH
                 else "⭐⭐ MEDIUM" if score >= SCORE_MEDIUM
                 else "⭐ LOW")

        trade = calculate_fvg_trade(current_price, sweep["sweep_low"], fvg, "LONG") if in_zone else None

        results.append({
            "pair":             pair,
            "tf":               FVG_TF,
            "tf_label":         TF_LABEL.get(FVG_TF, FVG_TF.upper()),
            "direction":        "LONG",
            "sweep":            sweep,
            "fvg":              fvg,
            "atr":              round(atr, 4),
            "current_price":    current_price,
            "in_fvg_zone":      in_zone,
            "trade":            trade,
            "confluence_score": score,
            "confluence_label": label,
            "confluence_notes": notes,
        })

    # ── SHORT setups ─────────────────────────────────────────
    for swing in find_swing_highs(candles, lookback=5):
        if swing["index"] < n - SWEEP_LOOKBACK:
            continue

        sweep = _detect_sweep_of_high(candles, swing["index"])
        if sweep is None:
            continue

        fvg = _detect_bearish_fvg(candles, sweep["sweep_index"], atr)
        if fvg is None:
            continue

        if _is_fvg_invalidated(fvg, candles, "SHORT"):
            continue

        in_zone = _is_in_fvg_zone(current_price, fvg)

        score, notes = 5, []

        if _htf_aligned(candles, "SHORT"):
            score += 2
            notes.append("✅ 4H HTF bearish (EMA50)")
        else:
            notes.append("⬜ 4H HTF tidak bearish")

        if fvg["fvg_size"] > atr:
            score += 1
            notes.append(f"✅ FVG besar: {fvg['fvg_size']:.4f} > ATR {atr:.4f}")
        else:
            notes.append(f"⬜ FVG kecil: {fvg['fvg_size']:.4f} (ATR {atr:.4f})")

        label = ("⭐⭐⭐ HIGH"   if score >= SCORE_HIGH
                 else "⭐⭐ MEDIUM" if score >= SCORE_MEDIUM
                 else "⭐ LOW")

        trade = calculate_fvg_trade(current_price, sweep["sweep_high"], fvg, "SHORT") if in_zone else None

        results.append({
            "pair":             pair,
            "tf":               FVG_TF,
            "tf_label":         TF_LABEL.get(FVG_TF, FVG_TF.upper()),
            "direction":        "SHORT",
            "sweep":            sweep,
            "fvg":              fvg,
            "atr":              round(atr, 4),
            "current_price":    current_price,
            "in_fvg_zone":      in_zone,
            "trade":            trade,
            "confluence_score": score,
            "confluence_label": label,
            "confluence_notes": notes,
        })

    results.sort(key=lambda x: x["confluence_score"], reverse=True)
    return results
