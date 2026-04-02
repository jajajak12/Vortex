import numpy as np
from binance.client import Client
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    LIQUIDITY_CANDLES_MIN, SWING_LOOKBACK,
    TOUCH_THRESHOLD_PCT, VOLUME_SPIKE_MULTIPLIER,
    TF_ZONE, TF_MONITOR, TF_ENTRY
)

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ── Timeframe mapping ────────────────────────────────────────
TF_MAP = {
    "5m":  Client.KLINE_INTERVAL_5MINUTE,
    "15m": Client.KLINE_INTERVAL_15MINUTE,
    "30m": Client.KLINE_INTERVAL_30MINUTE,
    "1h":  Client.KLINE_INTERVAL_1HOUR,
    "4h":  Client.KLINE_INTERVAL_4HOUR,
    "1d":  Client.KLINE_INTERVAL_1DAY,
    "1w":  Client.KLINE_INTERVAL_1WEEK,
}

def get_candles(pair: str, tf: str, limit: int = 100) -> list[dict]:
    """Ambil data OHLCV dari Binance."""
    raw = client.get_klines(symbol=pair, interval=TF_MAP[tf], limit=limit)
    candles = []
    for c in raw:
        candles.append({
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        })
    return candles

def find_swing_lows(candles: list[dict], lookback: int = 5) -> list[dict]:
    """
    Cari swing low: candle yang menjadi titik terendah lokal.
    Kriteria: low[i] < low semua candle dalam window kiri & kanan.
    """
    swings = []
    for i in range(lookback, len(candles) - lookback):
        low_i = candles[i]["low"]
        window = [candles[j]["low"] for j in range(i - lookback, i + lookback + 1) if j != i]
        if low_i < min(window):
            swings.append({"index": i, "price": low_i, "candle": candles[i]})
    return swings

def find_swing_highs(candles: list[dict], lookback: int = 5) -> list[dict]:
    """
    Cari swing high: candle yang menjadi titik tertinggi lokal.
    Kriteria: high[i] > high semua candle dalam window kiri & kanan.
    """
    swings = []
    for i in range(lookback, len(candles) - lookback):
        high_i = candles[i]["high"]
        window = [candles[j]["high"] for j in range(i - lookback, i + lookback + 1) if j != i]
        if high_i > max(window):
            swings.append({"index": i, "price": high_i, "candle": candles[i]})
    return swings

def is_fresh(swing_index: int, total_candles: int, min_candles: int = LIQUIDITY_CANDLES_MIN) -> bool:
    """
    Zona dianggap fresh jika terbentuk minimal LIQUIDITY_CANDLES_MIN candle yang lalu.
    """
    candles_since = total_candles - 1 - swing_index
    return candles_since >= min_candles

def is_mitigated(swing: dict, candles: list[dict], zone_type: str) -> bool:
    """
    Zona dianggap mitigated (tidak fresh) jika setelah swing terbentuk,
    harga pernah kembali menyentuh level tersebut.
    - LONG (swing low): ada candle berikutnya yang low-nya <= pivot + buffer
    - SHORT (swing high): ada candle berikutnya yang high-nya >= pivot - buffer
    """
    idx    = swing["index"]
    price  = swing["price"]
    buffer = price * TOUCH_THRESHOLD_PCT

    for c in candles[idx + 1:]:
        if zone_type == "LONG" and c["low"] <= price + buffer:
            return True
        if zone_type == "SHORT" and c["high"] >= price - buffer:
            return True
    return False

def _compute_htf_bias(candles: list[dict]) -> str:
    """
    Trend bias dari EMA50 — pakai candle 4H yang sudah ada, tanpa API call tambahan.
    Return 'LONG' jika close > EMA50, 'SHORT' jika sebaliknya.
    """
    if len(candles) < 50:
        return "LONG"
    closes = [c["close"] for c in candles]
    k   = 2 / (50 + 1)
    ema = sum(closes[:50]) / 50
    for price in closes[50:]:
        ema = price * k + ema * (1 - k)
    return "LONG" if candles[-1]["close"] > ema else "SHORT"

def get_fresh_liquidity_zones(pair: str) -> dict:
    """
    Ambil zona liquidity fresh dari TF 4H.
    Fresh = terbentuk ≥ LIQUIDITY_CANDLES_MIN candle lalu DAN belum pernah dikunjungi lagi.
    Return: dict berisi list zona LONG, SHORT, dan htf_bias (EMA50 4H).
    """
    candles = get_candles(pair, TF_ZONE, limit=SWING_LOOKBACK + 20)
    total   = len(candles)

    swing_lows  = find_swing_lows(candles, lookback=5)
    swing_highs = find_swing_highs(candles, lookback=5)

    fresh_lows  = [s for s in swing_lows
                   if is_fresh(s["index"], total) and not is_mitigated(s, candles, "LONG")]
    fresh_highs = [s for s in swing_highs
                   if is_fresh(s["index"], total) and not is_mitigated(s, candles, "SHORT")]

    # Zona = area sekitar swing (±0.5% untuk toleransi)
    long_zones  = [{"low": s["price"] * 0.995, "high": s["price"] * 1.005,
                    "pivot": s["price"], "type": "LONG"} for s in fresh_lows]
    short_zones = [{"low": s["price"] * 0.995, "high": s["price"] * 1.005,
                    "pivot": s["price"], "type": "SHORT"} for s in fresh_highs]

    return {"LONG": long_zones, "SHORT": short_zones, "htf_bias": _compute_htf_bias(candles)}

def is_touching_zone(current_price: float, zone: dict) -> bool:
    """
    Cek apakah harga saat ini menyentuh zona liquidity.
    Harga harus berada di dalam [low - buffer, high + buffer].
    """
    buffer = zone["pivot"] * TOUCH_THRESHOLD_PCT
    return (zone["low"] - buffer) <= current_price <= (zone["high"] + buffer)

def check_volume_spike(candles: list[dict], index: int, window: int = 20) -> bool:
    """Cek apakah volume candle[index] >= 1.5x rata-rata volume sebelumnya."""
    if index < window:
        return False
    avg_vol = np.mean([c["volume"] for c in candles[index - window:index]])
    return candles[index]["volume"] >= avg_vol * VOLUME_SPIKE_MULTIPLIER

def check_rejection_long(candles_5m: list[dict], zone: dict) -> dict | None:
    """
    Cek rejection LONG di TF 5m dengan 2-candle confirmation:
    1. Rejection: spike bawah zona + close kembali masuk dengan strength ≥30% zona
    2. Konfirmasi: candle berikutnya harus close bullish
    Entry di open candle setelah konfirmasi.
    """
    if len(candles_5m) < 4:
        return None

    zone_size    = zone["high"] - zone["low"]
    min_recovery = zone["low"] + zone_size * 0.3  # close minimal 30% masuk zona

    for i in range(len(candles_5m) - 4, len(candles_5m) - 2):
        c = candles_5m[i]
        broke_below    = c["low"]   < zone["low"]
        strong_recover = c["close"] >= min_recovery
        vol_spike      = check_volume_spike(candles_5m, i)

        if not (broke_below and strong_recover):
            continue

        conf = candles_5m[i + 1]
        if conf["close"] <= conf["open"]:  # konfirmasi harus bullish
            continue

        return {
            "confirmed":     True,
            "candle_index":  i,
            "entry_price":   candles_5m[i + 2]["open"],
            "rejection_low": c["low"],
            "volume_spike":  vol_spike
        }
    return None

def check_rejection_short(candles_5m: list[dict], zone: dict) -> dict | None:
    """
    Cek rejection SHORT di TF 5m dengan 2-candle confirmation:
    1. Rejection: spike atas zona + close kembali masuk dengan strength ≥30% zona
    2. Konfirmasi: candle berikutnya harus close bearish
    Entry di open candle setelah konfirmasi.
    """
    if len(candles_5m) < 4:
        return None

    zone_size    = zone["high"] - zone["low"]
    max_recovery = zone["high"] - zone_size * 0.3  # close minimal 30% masuk zona

    for i in range(len(candles_5m) - 4, len(candles_5m) - 2):
        c = candles_5m[i]
        broke_above    = c["high"]  > zone["high"]
        strong_recover = c["close"] <= max_recovery
        vol_spike      = check_volume_spike(candles_5m, i)

        if not (broke_above and strong_recover):
            continue

        conf = candles_5m[i + 1]
        if conf["close"] >= conf["open"]:  # konfirmasi harus bearish
            continue

        return {
            "confirmed":      True,
            "candle_index":   i,
            "entry_price":    candles_5m[i + 2]["open"],
            "rejection_high": c["high"],
            "volume_spike":   vol_spike
        }
    return None

def calculate_trade(direction: str, entry: float, zone: dict,
                    prev_liquidity_price: float) -> dict:
    """
    Hitung SL dan TP:
    - SL: tepat di luar batas zona rejection (bukan zona lain yang jauh)
    - TP: 1:1 RR dari SL distance
    """
    if direction == "LONG":
        sl      = zone["low"] * 0.998   # sedikit di bawah low zona
        sl_dist = entry - sl
        tp      = entry + sl_dist
    else:  # SHORT
        sl      = zone["high"] * 1.002  # sedikit di atas high zona
        sl_dist = sl - entry
        tp      = entry - sl_dist

    rr_ratio = round(abs(tp - entry) / abs(entry - sl), 2) if abs(entry - sl) > 0 else 1.0

    return {
        "entry": round(entry, 4),
        "sl":    round(sl, 4),
        "tp":    round(tp, 4),
        "rr":    f"1:{rr_ratio}"
    }
