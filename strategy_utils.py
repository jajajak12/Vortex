import time as _time

import numpy as np
from binance.client import Client
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    BTC_PAIR, MACRO_EMA_PERIOD, ATR_PERIOD,
)

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

_candle_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL = 55

_circuit: dict[str, dict] = {}
_CB_FAIL_THRESHOLD = 3
_CB_COOLDOWN_SECS  = 300

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
    key    = f"{pair}_{tf}_{limit}"
    cb_key = f"{pair}_{tf}"
    now    = _time.time()

    cb = _circuit.get(cb_key)
    if cb and cb["open_until"] > now:
        remaining = int(cb["open_until"] - now)
        raise Exception(f"[CB] Circuit open: {cb_key} — retry in {remaining}s")

    cached = _candle_cache.get(key)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            raw = client.get_klines(symbol=pair, interval=TF_MAP[tf], limit=limit)
            candles = [
                {
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                }
                for c in raw
            ]
            _candle_cache[key] = (now, candles)
            if cb_key in _circuit:
                _circuit[cb_key] = {"failures": 0, "open_until": 0.0}
            return candles
        except Exception as e:
            last_exc = e
            if attempt < 2:
                _time.sleep(0.5 * (attempt + 1))

    entry = _circuit.get(cb_key, {"failures": 0, "open_until": 0.0})
    entry["failures"] += 1
    if entry["failures"] >= _CB_FAIL_THRESHOLD:
        entry["open_until"] = now + _CB_COOLDOWN_SECS
        try:
            from telegram_bot import alert_info
            alert_info(f"⚠️ [CIRCUIT OPEN] Binance API {cb_key} suspended 5min ({entry['failures']} failures)")
        except Exception:
            pass
    _circuit[cb_key] = entry
    raise last_exc  # type: ignore[misc]


def calculate_atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        prev = candles[i - 1]["close"]
        h, l = candles[i]["high"], candles[i]["low"]
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
    return float(np.mean(trs[-period:]))


def find_swing_lows(candles: list[dict], lookback: int = 5) -> list[dict]:
    swings = []
    for i in range(lookback, len(candles) - lookback):
        low_i = candles[i]["low"]
        window = [candles[j]["low"] for j in range(i - lookback, i + lookback + 1) if j != i]
        if low_i < min(window):
            swings.append({"index": i, "price": low_i, "candle": candles[i]})
    return swings


def find_swing_highs(candles: list[dict], lookback: int = 5) -> list[dict]:
    swings = []
    for i in range(lookback, len(candles) - lookback):
        high_i = candles[i]["high"]
        window = [candles[j]["high"] for j in range(i - lookback, i + lookback + 1) if j != i]
        if high_i > max(window):
            swings.append({"index": i, "price": high_i, "candle": candles[i]})
    return swings


def _compute_htf_bias(candles: list[dict]) -> str:
    if len(candles) < 50:
        return "LONG"
    closes = [c["close"] for c in candles]
    k   = 2 / (50 + 1)
    ema = sum(closes[:50]) / 50
    for price in closes[50:]:
        ema = price * k + ema * (1 - k)
    return "LONG" if candles[-1]["close"] > ema else "SHORT"


def get_btc_macro_regime() -> str:
    candles = get_candles(BTC_PAIR, "1w", limit=MACRO_EMA_PERIOD + 20)
    if len(candles) < MACRO_EMA_PERIOD:
        return "BULL"
    closes = [c["close"] for c in candles]
    k   = 2 / (MACRO_EMA_PERIOD + 1)
    ema = sum(closes[:MACRO_EMA_PERIOD]) / MACRO_EMA_PERIOD
    for price in closes[MACRO_EMA_PERIOD:]:
        ema = price * k + ema * (1 - k)
    return "BULL" if candles[-1]["close"] > ema else "BEAR"
