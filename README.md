# Vortex

AI trading signal agent untuk crypto — scanner only, tidak eksekusi trade otomatis.

Menjalankan **2 strategi** secara paralel setiap scan cycle.

---

## Strategi

### Strategy 1 — Fresh Liquidity Grab + Rejection

1. **Zone Detection (4H)** — identifikasi swing high/low yang:
   - Terbentuk ≥10 candle lalu (`is_fresh`)
   - Belum pernah dikunjungi lagi setelah terbentuk (`is_mitigated = False`)
2. **HTF Trend Filter (EMA50 4H)** — hanya proses zona yang searah trend dominan (LONG jika close > EMA50, SHORT jika sebaliknya)
3. **Touch Alert (30m)** — deteksi ketika harga memasuki area zona
4. **Entry Signal (5m)** — konfirmasi 2 candle:
   - Candle 1: false breakout (spike tembus zona, close kembali masuk dengan strength ≥30% zona)
   - Candle 2: konfirmasi arah (close bullish untuk LONG, close bearish untuk SHORT)
5. **Trade Calculation** — SL tepat di luar batas zona, TP dengan RR 1:1

### Strategy 2 — Wick Fill

1. **Wick Detection (1W / 1D / 4H)** — identifikasi candle dengan long downside wick:
   - Lower wick ≥ 1.5x body size, ATAU lower wick ≥ 30% total candle range
   - Wick belum pernah ditest ulang (`is_wick_mitigated = False`)
2. **Entry Zone** — antara wick low dan level 50% fill
3. **Entry Signal** — harga masuk entry zone + konfirmasi rejection di 5m (sama dengan mekanisme Strategy 1)
4. **Trade Calculation**:
   - SL: 0.8% di bawah wick low
   - TP1: 50% fill level
   - TP2: 100% fill level (body bottom candle wick)
5. **Confluence Scoring** — bonus poin: dekat 1W50 EMA (+2), massive wick (+1), TF lebih tinggi (+1/+2)

---

## Pairs

`BTCUSDT` `ETHUSDT` `BNBUSDT` `SOLUSDT`

---

## Setup

### 1. Clone repo

```bash
git clone https://github.com/jajajak12/Vortex.git
cd Vortex
```

### 2. Install dependencies

```bash
pip install --break-system-packages -r requirements.txt
```

### 3. Isi config.py

```python
TELEGRAM_BOT_TOKEN = "your_bot_token"   # dari @BotFather
TELEGRAM_CHAT_ID   = "your_chat_id"     # dari @userinfobot
BINANCE_API_KEY    = "your_api_key"     # read-only cukup
BINANCE_API_SECRET = "your_api_secret"

CRYPTO_PAIRS           = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
SCAN_INTERVAL_SECONDS  = 60
TF_ZONE                = "4h"
TF_MONITOR             = "30m"
TF_ENTRY               = "5m"
LIQUIDITY_CANDLES_MIN  = 10
SWING_LOOKBACK         = 80
TOUCH_THRESHOLD_PCT    = 0.005
VOLUME_SPIKE_MULTIPLIER = 1.5
```

### 4. Jalankan

```bash
python3 -u scanner.py
```

Untuk jalan terus di background (VPS):

```bash
nohup python3 -u scanner.py > scanner.log 2>&1 &
tail -f scanner.log
```

---

## Alert Telegram

### Strategy 1

| Alert | Keterangan |
|---|---|
| ⚠️ TOUCH | Harga memasuki zona liquidity |
| ✅ ENTRY SIGNAL | 2-candle rejection confirmed + searah HTF trend |
| ✅ WIN / ❌ LOSS | Hasil trade setelah hit TP atau SL |
| 📊 WINRATE | Laporan winrate otomatis setiap ~1 jam |

### Strategy 2

| Alert | Keterangan |
|---|---|
| 🕯️ WICK DETECTED | Long downside wick terdeteksi di 1W/1D/4H |
| ✅ WICK FILL ENTRY | Harga masuk entry zone + rejection 5m confirmed |

---

## File Structure

```
├── config.py              # Kredensial & parameter (jangan di-push)
├── scanner.py             # Main loop, scan kedua strategi paralel per pair
├── strategy1_liquidity.py # Strategy 1: Liquidity Grab + HTF filter
├── strategy2_wick.py      # Strategy 2: Wick Fill (1W/1D/4H)
├── telegram_bot.py        # Alert functions Strategy 1
├── wick_alerts.py         # Alert functions Strategy 2
├── trade_tracker.py       # Catat sinyal, monitor TP/SL, winrate
├── trades.json            # Data hasil tracking (auto-generated)
└── requirements.txt
```

---

## Catatan

- Binance API hanya butuh permission **Read Info** (tidak trading otomatis)
- `config.py` di-gitignore, jangan pernah di-push ke repo
- Sedang dalam fase **paper trading** — observasi winrate sebelum real trade
- Target winrate >45-50% sebelum pertimbangkan real trade
