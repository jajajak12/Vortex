import numpy as np
from strategy1_liquidity import get_candles

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
    Deteksi long downside wick (bullish rejection).
    Kriteria:
    - Lower wick >= 1.5x body size, ATAU
    - Lower wick >= 30% total candle range
    Return dict dengan detail wick jika valid, None jika tidak.
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

    if total_range == 0:
        return None

    # Pastikan ada lower wick yang signifikan
    if lower_wick <= 0:
        return None

    wick_body_ratio = lower_wick / body_size if body_size > 0 else 999
    wick_range_ratio = lower_wick / total_range

    is_valid = (
        wick_body_ratio  >= WICK_MIN_BODY_RATIO or
        wick_range_ratio >= WICK_MIN_RANGE_RATIO
    )

    if not is_valid:
        return None

    # Hitung level wick fill
    wick_low      = l            # 0% — wick low (SL reference)
    wick_50pct    = l + (lower_wick * 0.5)   # 50% fill level
    wick_100pct   = body_bottom  # 100% fill = body bottom

    return {
        "wick_low":        round(wick_low, 4),
        "wick_50pct":      round(wick_50pct, 4),
        "wick_100pct":     round(wick_100pct, 4),
        "lower_wick_size": round(lower_wick, 4),
        "body_size":       round(body_size, 4),
        "wick_body_ratio": round(wick_body_ratio, 2),
        "wick_range_ratio":round(wick_range_ratio, 2),
        "candle_high":     h,
        "candle_low":      l,
        "candle_open":     o,
        "candle_close":    c,
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

def is_price_in_entry_zone(current_price: float, wick: dict) -> bool:
    """
    Cek apakah harga saat ini berada di entry zone.
    Entry zone = antara wick low dan 50% level.
    """
    return wick["wick_low"] <= current_price <= wick["wick_50pct"]

def is_setup_invalidated(current_price: float, wick: dict, last_close: float) -> bool:
    """Setup invalid jika price CLOSE di bawah wick low (acceptance below wick)."""
    return last_close < wick["wick_low"]

def is_wick_mitigated(wick: dict, candles: list[dict], wick_index: int) -> bool:
    """
    Wick dianggap mitigated (sudah pernah ditest) jika setelah candle wick terbentuk,
    ada candle berikutnya yang low-nya masuk ke dalam area wick (≤ 50% fill level).
    Wick yang sudah mitigated probabilitasnya lebih rendah.
    """
    for c in candles[wick_index + 1:]:
        if c["low"] <= wick["wick_50pct"]:
            return True
    return False

def calculate_wick_trade(current_price: float, wick: dict) -> dict:
    """
    Hitung entry, SL, TP1, TP2 berdasarkan wick fill rules.
    - Entry: current price (di entry zone)
    - SL: 0.8% di bawah wick low
    - TP1: 50% wick fill level
    - TP2: 100% wick fill level
    """
    entry = current_price
    sl    = wick["wick_low"] * (1 - SL_BUFFER_PCT)
    tp1   = wick["wick_50pct"]
    tp2   = wick["wick_100pct"]

    sl_dist  = entry - sl
    rr1 = round((tp1 - entry) / sl_dist, 2) if sl_dist > 0 else 0
    rr2 = round((tp2 - entry) / sl_dist, 2) if sl_dist > 0 else 0

    return {
        "entry": round(entry, 4),
        "sl":    round(sl, 4),
        "tp1":   round(tp1, 4),
        "tp2":   round(tp2, 4),
        "rr1":   f"1:{rr1}",
        "rr2":   f"1:{rr2}",
        "sl_pct":  round((entry - sl) / entry * 100, 2),
        "tp1_pct": round((tp1 - entry) / entry * 100, 2),
        "tp2_pct": round((tp2 - entry) / entry * 100, 2),
    }

def scan_wick_setups(pair: str) -> list[dict]:
    """
    Scan semua TF (1W, 1D, 4H) untuk wick fill setups.
    Return list setup yang valid dan dalam entry zone.
    """
    results = []

    for tf in WICK_TIMEFRAMES:
        try:
            candles = get_candles(pair, tf, limit=100)
            if len(candles) < EMA_PERIOD + 5:
                continue

            current_price = candles[-1]["close"]
            current_ema   = _get_current_ema(candles)  # computed once per TF

            for i in range(len(candles) - 4, len(candles) - 1):
                candle = candles[i]
                wick   = is_long_downside_wick(candle)

                if wick is None:
                    continue

                if is_setup_invalidated(current_price, wick, candles[-1]["close"]):
                    continue

                if is_wick_mitigated(wick, candles, i):
                    continue

                in_entry_zone = is_price_in_entry_zone(current_price, wick)
                ema_info = check_ema_confluence(wick["wick_low"], current_ema)

                confluence_score = 0
                confluence_notes = []

                if ema_info["has_confluence"]:
                    confluence_score += 2
                    confluence_notes.append(f"✅ Dekat 1W50EMA (${ema_info['ema_value']})")
                else:
                    confluence_notes.append(f"⬜ 1W50EMA jauh (${ema_info['ema_value']}, {ema_info['distance_pct']}% away)")

                if wick["wick_body_ratio"] >= WICK_MASSIVE_RATIO:
                    confluence_score += 1
                    confluence_notes.append(f"✅ Massive wick ({wick['wick_body_ratio']}x body)")
                elif wick["wick_body_ratio"] >= WICK_STRONG_RATIO:
                    confluence_notes.append(f"✅ Strong wick ({wick['wick_body_ratio']}x body)")
                else:
                    confluence_notes.append(f"⬜ Moderate wick ({wick['wick_body_ratio']}x body)")

                if tf == "1w":
                    confluence_score += 2
                elif tf == "1d":
                    confluence_score += 1

                if confluence_score >= CONFLUENCE_HIGH:
                    confluence_label = "⭐⭐⭐ HIGH"
                elif confluence_score >= CONFLUENCE_MEDIUM:
                    confluence_label = "⭐⭐ MEDIUM"
                else:
                    confluence_label = "⭐ LOW"

                trade = calculate_wick_trade(current_price, wick) if in_entry_zone else None

                results.append({
                    "pair":             pair,
                    "tf":               tf,
                    "tf_label":         TF_LABEL[tf],
                    "priority":         TF_PRIORITY[tf],
                    "wick":             wick,
                    "ema_info":         ema_info,
                    "in_entry_zone":    in_entry_zone,
                    "current_price":    current_price,
                    "trade":            trade,
                    "confluence_score": confluence_score,
                    "confluence_label": confluence_label,
                    "confluence_notes": confluence_notes,
                })

        except Exception as e:
            print(f"[WICK ERROR] {pair} {tf}: {e}")

    # Sort by priority (1W dulu, lalu 1D, lalu 4H)
    tf_order = {"1w": 0, "1d": 1, "4h": 2}
    results.sort(key=lambda x: tf_order.get(x["tf"], 99))

    return results
