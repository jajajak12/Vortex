import numpy as np
from strategy1_liquidity import get_candles
from vortex_logger import get_logger

log = get_logger(__name__)

# ── Parameter Strategy 2 ─────────────────────────────────────
WICK_MIN_BODY_RATIO   = 1.5    # Wick minimal 1.5x body size
WICK_MIN_RANGE_RATIO  = 0.30   # Atau minimal 30% dari total candle range
SL_BUFFER_PCT         = 0.008  # SL 0.8% di bawah wick low
EMA_PERIOD            = 50     # 1W 50 EMA untuk confluence
EMA_CONFLUENCE_PCT    = 0.02   # Wick dianggap dekat EMA jika dalam 2%
WICK_MASSIVE_RATIO    = 3.0    # Threshold "massive wick" (3x body)
WICK_STRONG_RATIO     = 2.0    # Threshold "strong wick" (2x body)
CONFLUENCE_HIGH       = 4      # Score >= 4 = HIGH confluence
CONFLUENCE_MEDIUM     = 2      # Score >= 2 = MEDIUM confluence

# Prioritas TF scan (tinggi ke rendah)
WICK_TIMEFRAMES = ["1w", "1d", "4h"]

TF_LABEL = {
    "1w": "1W (Weekly)",
    "1d": "1D (Daily)",
    "4h": "4H",
}

TF_PRIORITY = {
    "1w": "🔴 HIGH",
    "1d": "🟡 MEDIUM",
    "4h": "🟢 STANDARD",
}

def calculate_ema(prices: list[float], period: int) -> list[float]:
    """Hitung EMA dari list harga close."""
    ema = []
    k = 2 / (period + 1)
    for i, price in enumerate(prices):
        if i < period:
            ema.append(None)
        elif i == period:
            ema.append(sum(prices[:period]) / period)
        else:
            ema.append(price * k + ema[-1] * (1 - k))
    return ema

def is_long_downside_wick(candle: dict) -> dict | None:
    """
    Deteksi long downside wick (bullish rejection dari bawah).
    Kriteria: lower wick >= 1.5x body, ATAU lower wick >= 30% total range.
    """
    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    body_top    = max(o, c)
    body_bottom = min(o, c)
    body_size   = body_top - body_bottom
    lower_wick  = body_bottom - l
    total_range = h - l

    if total_range == 0 or lower_wick <= 0:
        return None

    wick_body_ratio  = lower_wick / body_size if body_size > 0 else 999
    wick_range_ratio = lower_wick / total_range

    if not (wick_body_ratio >= WICK_MIN_BODY_RATIO or
            wick_range_ratio >= WICK_MIN_RANGE_RATIO):
        return None

    wick_50pct  = l + lower_wick * 0.5
    return {
        "wick_low":         round(l, 4),
        "wick_50pct":       round(wick_50pct, 4),
        "wick_100pct":      round(body_bottom, 4),
        "lower_wick_size":  round(lower_wick, 4),
        "body_size":        round(body_size, 4),
        "wick_body_ratio":  round(wick_body_ratio, 2),
        "wick_range_ratio": round(wick_range_ratio, 2),
        "candle_high":  h,
        "candle_low":   l,
        "candle_open":  o,
        "candle_close": c,
    }


def is_long_upside_wick(candle: dict) -> dict | None:
    """
    Deteksi long upside wick (bearish rejection dari atas).
    Kriteria: upper wick >= 1.5x body, ATAU upper wick >= 30% total range.
    """
    o = candle["open"]
    h = candle["high"]
    l = candle["low"]
    c = candle["close"]

    body_top    = max(o, c)
    body_bottom = min(o, c)
    body_size   = body_top - body_bottom
    upper_wick  = h - body_top
    total_range = h - l

    if total_range == 0 or upper_wick <= 0:
        return None

    wick_body_ratio  = upper_wick / body_size if body_size > 0 else 999
    wick_range_ratio = upper_wick / total_range

    if not (wick_body_ratio >= WICK_MIN_BODY_RATIO or
            wick_range_ratio >= WICK_MIN_RANGE_RATIO):
        return None

    wick_50pct  = h - upper_wick * 0.5
    return {
        "wick_high":        round(h, 4),
        "wick_50pct":       round(wick_50pct, 4),   # 50% fill dari atas
        "wick_100pct":      round(body_top, 4),     # 100% fill = body top
        "upper_wick_size":  round(upper_wick, 4),
        "body_size":        round(body_size, 4),
        "wick_body_ratio":  round(wick_body_ratio, 2),
        "wick_range_ratio": round(wick_range_ratio, 2),
        "candle_high":  h,
        "candle_low":   l,
        "candle_open":  o,
        "candle_close": c,
    }

def _get_current_ema(candles: list[dict]) -> float | None:
    closes = [c["close"] for c in candles]
    emas   = calculate_ema(closes, EMA_PERIOD)
    return next((e for e in reversed(emas) if e is not None), None)

def check_ema_confluence(wick_low: float, current_ema: float | None) -> dict:
    """Cek apakah wick low dekat dengan 50 EMA. current_ema pre-computed per TF."""
    if current_ema is None:
        return {"has_confluence": False, "ema_value": None, "distance_pct": None}

    distance_pct = abs(wick_low - current_ema) / current_ema
    has_confluence = distance_pct <= EMA_CONFLUENCE_PCT

    return {
        "has_confluence": has_confluence,
        "ema_value":      round(current_ema, 4),
        "distance_pct":   round(distance_pct * 100, 2),
    }

def is_price_in_entry_zone_long(current_price: float, wick: dict) -> bool:
    """Entry zone LONG = antara wick low dan 50% level (dari bawah)."""
    return wick["wick_low"] <= current_price <= wick["wick_50pct"]

def is_price_in_entry_zone_short(current_price: float, wick: dict) -> bool:
    """Entry zone SHORT = antara wick high dan 50% level (dari atas)."""
    return wick["wick_50pct"] <= current_price <= wick["wick_high"]

def is_price_in_entry_zone(current_price: float, wick: dict) -> bool:
    """Backward compat — hanya untuk LONG downside wick."""
    return is_price_in_entry_zone_long(current_price, wick)

def is_setup_invalidated_long(last_close: float, wick: dict) -> bool:
    """LONG invalid jika close di bawah wick low."""
    return last_close < wick["wick_low"]

def is_setup_invalidated_short(last_close: float, wick: dict) -> bool:
    """SHORT invalid jika close di atas wick high."""
    return last_close > wick["wick_high"]

def is_setup_invalidated(current_price: float, wick: dict, last_close: float) -> bool:
    """Backward compat."""
    return is_setup_invalidated_long(last_close, wick)

def is_wick_mitigated_long(wick: dict, candles: list[dict], wick_index: int) -> bool:
    """LONG wick mitigated jika ada candle berikutnya yang low <= 50% fill."""
    for c in candles[wick_index + 1:]:
        if c["low"] <= wick["wick_50pct"]:
            return True
    return False

def is_wick_mitigated_short(wick: dict, candles: list[dict], wick_index: int) -> bool:
    """SHORT wick mitigated jika ada candle berikutnya yang high >= 50% fill."""
    for c in candles[wick_index + 1:]:
        if c["high"] >= wick["wick_50pct"]:
            return True
    return False

def is_wick_mitigated(wick: dict, candles: list[dict], wick_index: int) -> bool:
    """Backward compat."""
    return is_wick_mitigated_long(wick, candles, wick_index)

def calculate_wick_trade_long(current_price: float, wick: dict) -> dict:
    """LONG: SL di bawah wick low, TP ke atas (50% dan 100% fill)."""
    entry   = current_price
    sl      = wick["wick_low"] * (1 - SL_BUFFER_PCT)
    tp1     = wick["wick_50pct"]
    tp2     = wick["wick_100pct"]
    sl_dist = entry - sl
    rr1 = round((tp1 - entry) / sl_dist, 2) if sl_dist > 0 else 0
    rr2 = round((tp2 - entry) / sl_dist, 2) if sl_dist > 0 else 0
    return {
        "entry": round(entry, 4), "sl": round(sl, 4),
        "tp1":   round(tp1, 4),   "tp2": round(tp2, 4),
        "rr1": f"1:{rr1}", "rr2": f"1:{rr2}",
        "sl_pct":  round((entry - sl) / entry * 100, 2),
        "tp1_pct": round((tp1 - entry) / entry * 100, 2),
        "tp2_pct": round((tp2 - entry) / entry * 100, 2),
    }

def calculate_wick_trade_short(current_price: float, wick: dict) -> dict:
    """SHORT: SL di atas wick high, TP ke bawah (50% dan 100% fill)."""
    entry   = current_price
    sl      = wick["wick_high"] * (1 + SL_BUFFER_PCT)
    tp1     = wick["wick_50pct"]
    tp2     = wick["wick_100pct"]
    sl_dist = sl - entry
    rr1 = round((entry - tp1) / sl_dist, 2) if sl_dist > 0 else 0
    rr2 = round((entry - tp2) / sl_dist, 2) if sl_dist > 0 else 0
    return {
        "entry": round(entry, 4), "sl": round(sl, 4),
        "tp1":   round(tp1, 4),   "tp2": round(tp2, 4),
        "rr1": f"1:{rr1}", "rr2": f"1:{rr2}",
        "sl_pct":  round((sl - entry) / entry * 100, 2),
        "tp1_pct": round((entry - tp1) / entry * 100, 2),
        "tp2_pct": round((entry - tp2) / entry * 100, 2),
    }

def calculate_wick_trade(current_price: float, wick: dict) -> dict:
    """Backward compat — hanya untuk LONG."""
    return calculate_wick_trade_long(current_price, wick)

def _score_wick(wick_body_ratio: float, tf: str, ema_info: dict) -> tuple[int, list[str]]:
    """Hitung confluence score dan notes — sama untuk LONG dan SHORT."""
    score, notes = 0, []

    if ema_info["has_confluence"]:
        score += 2
        notes.append(f"✅ Dekat EMA50 (${ema_info['ema_value']})")
    else:
        notes.append(f"⬜ EMA50 jauh (${ema_info['ema_value']}, {ema_info['distance_pct']}% away)")

    if wick_body_ratio >= WICK_MASSIVE_RATIO:
        score += 1
        notes.append(f"✅ Massive wick ({wick_body_ratio}x body)")
    elif wick_body_ratio >= WICK_STRONG_RATIO:
        notes.append(f"✅ Strong wick ({wick_body_ratio}x body)")
    else:
        notes.append(f"⬜ Moderate wick ({wick_body_ratio}x body)")

    if tf == "1w":
        score += 2
    elif tf == "1d":
        score += 1

    return score, notes


def scan_wick_setups(pair: str) -> list[dict]:
    """
    Scan semua TF (1W, 1D, 4H) untuk wick fill setups — LONG dan SHORT.
    Return list setup yang valid, diurutkan 1W > 1D > 4H.
    """
    results  = []
    tf_order = {"1w": 0, "1d": 1, "4h": 2}

    for tf in WICK_TIMEFRAMES:
        try:
            candles = get_candles(pair, tf, limit=100)
            if len(candles) < EMA_PERIOD + 5:
                continue

            current_price = candles[-1]["close"]
            last_close    = candles[-1]["close"]
            current_ema   = _get_current_ema(candles)

            for i in range(len(candles) - 4, len(candles) - 1):
                candle = candles[i]

                # ── LONG: downside wick ──────────────────────────
                wick_long = is_long_downside_wick(candle)
                if wick_long and not is_setup_invalidated_long(last_close, wick_long) \
                              and not is_wick_mitigated_long(wick_long, candles, i):
                    in_zone  = is_price_in_entry_zone_long(current_price, wick_long)
                    ema_info = check_ema_confluence(wick_long["wick_low"], current_ema)
                    score, notes = _score_wick(wick_long["wick_body_ratio"], tf, ema_info)
                    label  = ("⭐⭐⭐ HIGH" if score >= CONFLUENCE_HIGH
                              else "⭐⭐ MEDIUM" if score >= CONFLUENCE_MEDIUM
                              else "⭐ LOW")
                    trade  = calculate_wick_trade_long(current_price, wick_long) if in_zone else None
                    results.append({
                        "pair":             pair,
                        "direction":        "LONG",
                        "tf":               tf,
                        "tf_label":         TF_LABEL[tf],
                        "priority":         TF_PRIORITY[tf],
                        "wick":             wick_long,
                        "ema_info":         ema_info,
                        "in_entry_zone":    in_zone,
                        "current_price":    current_price,
                        "trade":            trade,
                        "confluence_score": score,
                        "confluence_label": label,
                        "confluence_notes": notes,
                    })

                # ── SHORT: upside wick ───────────────────────────
                wick_short = is_long_upside_wick(candle)
                if wick_short and not is_setup_invalidated_short(last_close, wick_short) \
                               and not is_wick_mitigated_short(wick_short, candles, i):
                    in_zone  = is_price_in_entry_zone_short(current_price, wick_short)
                    ema_info = check_ema_confluence(wick_short["wick_high"], current_ema)
                    score, notes = _score_wick(wick_short["wick_body_ratio"], tf, ema_info)
                    label  = ("⭐⭐⭐ HIGH" if score >= CONFLUENCE_HIGH
                              else "⭐⭐ MEDIUM" if score >= CONFLUENCE_MEDIUM
                              else "⭐ LOW")
                    trade  = calculate_wick_trade_short(current_price, wick_short) if in_zone else None
                    results.append({
                        "pair":             pair,
                        "direction":        "SHORT",
                        "tf":               tf,
                        "tf_label":         TF_LABEL[tf],
                        "priority":         TF_PRIORITY[tf],
                        "wick":             wick_short,
                        "ema_info":         ema_info,
                        "in_entry_zone":    in_zone,
                        "current_price":    current_price,
                        "trade":            trade,
                        "confluence_score": score,
                        "confluence_label": label,
                        "confluence_notes": notes,
                    })

        except Exception as e:
            log.error(f"[WICK ERROR] {pair} {tf}: {e}")

    results.sort(key=lambda x: tf_order.get(x["tf"], 99))
    return results
