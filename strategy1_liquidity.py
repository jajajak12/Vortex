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
    Zona dianggap fresh jika belum tersentuh minimal
    LIQUIDITY_CANDLES_MIN candle 4H dari posisi swing ke candle terakhir.
    """
    candles_since = total_candles - 1 - swing_index
    return candles_since >= min_candles

def get_fresh_liquidity_zones(pair: str) -> dict:
    """
    Ambil zona liquidity fresh dari TF 4H.
    Return: dict berisi list zona LONG (swing low) dan SHORT (swing high).
    """
    candles = get_candles(pair, TF_ZONE, limit=SWING_LOOKBACK + 20)
    total   = len(candles)

    swing_lows  = find_swing_lows(candles, lookback=5)
    swing_highs = find_swing_highs(candles, lookback=5)

    fresh_lows  = [s for s in swing_lows  if is_fresh(s["index"], total)]
    fresh_highs = [s for s in swing_highs if is_fresh(s["index"], total)]

    # Zona = area sekitar swing (±0.5% untuk toleransi)
    long_zones  = [{"low": s["price"] * 0.995, "high": s["price"] * 1.005,
                    "pivot": s["price"], "type": "LONG"} for s in fresh_lows]
    short_zones = [{"low": s["price"] * 0.995, "high": s["price"] * 1.005,
                    "pivot": s["price"], "type": "SHORT"} for s in fresh_highs]

    return {"LONG": long_zones, "SHORT": short_zones}

def is_touching_zone(current_price: float, zone: dict) -> bool:
    """
    Cek apakah harga saat ini menyentuh zona liquidity.
    Toleransi: TOUCH_THRESHOLD_PCT dari batas zona.
    """
    buffer = zone["pivot"] * TOUCH_THRESHOLD_PCT
    if zone["type"] == "LONG":
        return current_price <= zone["high"] + buffer
    else:  # SHORT
        return current_price >= zone["low"] - buffer

def check_volume_spike(candles: list[dict], index: int, window: int = 20) -> bool:
    """Cek apakah volume candle[index] >= 1.5x rata-rata volume sebelumnya."""
    if index < window:
        return False
    avg_vol = np.mean([c["volume"] for c in candles[index - window:index]])
    return candles[index]["volume"] >= avg_vol * VOLUME_SPIKE_MULTIPLIER

def check_rejection_long(candles_5m: list[dict], zone: dict) -> dict | None:
    """
    Cek rejection LONG di TF 5m:
    - Candle menembus bawah zona (low < zone.low)
    - Candle CLOSE kembali di atas zona (close > zone.low)
    → False breakout = liquidity grab confirmed
    """
    if len(candles_5m) < 3:
        return None

    # Cek 3 candle terakhir (exclude candle yang masih berjalan)
    for i in range(len(candles_5m) - 3, len(candles_5m) - 1):
        c = candles_5m[i]
        broke_below = c["low"]   < zone["low"]
        recovered   = c["close"] > zone["low"]
        vol_spike   = check_volume_spike(candles_5m, i)

        if broke_below and recovered:
            return {
                "confirmed": True,
                "candle_index": i,
                "entry_price": candles_5m[i + 1]["open"] if i + 1 < len(candles_5m) else c["close"],
                "rejection_low": c["low"],
                "volume_spike": vol_spike
            }
    return None

def check_rejection_short(candles_5m: list[dict], zone: dict) -> dict | None:
    """
    Cek rejection SHORT di TF 5m:
    - Candle menembus atas zona (high > zone.high)
    - Candle CLOSE kembali di bawah zona (close < zone.high)
    → False breakout = liquidity grab confirmed
    """
    if len(candles_5m) < 3:
        return None

    for i in range(len(candles_5m) - 3, len(candles_5m) - 1):
        c = candles_5m[i]
        broke_above = c["high"]  > zone["high"]
        recovered   = c["close"] < zone["high"]
        vol_spike   = check_volume_spike(candles_5m, i)

        if broke_above and recovered:
            return {
                "confirmed": True,
                "candle_index": i,
                "entry_price": candles_5m[i + 1]["open"] if i + 1 < len(candles_5m) else c["close"],
                "rejection_high": c["high"],
                "volume_spike": vol_spike
            }
    return None

def calculate_trade(direction: str, entry: float, zone_pivot: float,
                    prev_liquidity_price: float) -> dict:
    """
    Hitung SL dan TP berdasarkan rules:
    - SL: di bawah/atas fresh liquidity sebelumnya
    - TP: 1:1 dengan SL (RR = 1:1)
    """
    if direction == "LONG":
        sl      = prev_liquidity_price * 0.998  # Sedikit di bawah liquidity sebelumnya
        sl_dist = entry - sl
        tp      = entry + sl_dist
    else:  # SHORT
        sl      = prev_liquidity_price * 1.002
        sl_dist = sl - entry
        tp      = entry - sl_dist

    rr_ratio = round(abs(tp - entry) / abs(entry - sl), 2) if abs(entry - sl) > 0 else 1.0

    return {
        "entry": round(entry, 4),
        "sl":    round(sl, 4),
        "tp":    round(tp, 4),
        "rr":    f"1:{rr_ratio}"
    }
