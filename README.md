# Vortex

AI trading signal agent untuk crypto — berbasis strategi **Fresh Liquidity Grab + Rejection**.

Agent ini **hanya mengirim sinyal**, tidak eksekusi trade otomatis.

---

## Strategi

1. **Zone Detection (4H)** — identifikasi swing high/low yang belum pernah dikunjungi ulang (unmitigated)
2. **Touch Alert (30m)** — deteksi ketika harga memasuki area zona
3. **Entry Signal (5m)** — konfirmasi false breakout: candle tembus zona lalu close kembali di dalam
4. **Trade Calculation** — SL tepat di luar batas zona, TP dengan RR 1:1

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

| Alert | Keterangan |
|---|---|
| ⚠️ TOUCH | Harga memasuki zona liquidity |
| ✅ ENTRY SIGNAL | Rejection confirmed, sinyal entry |
| ✅ WIN / ❌ LOSS | Hasil trade setelah hit TP atau SL |
| 📊 WINRATE | Laporan winrate otomatis setiap ~1 jam |

---

## File Structure

```
├── config.py              # Kredensial & parameter (jangan di-push)
├── scanner.py             # Main loop
├── strategy1_liquidity.py # Logika strategi + Binance API
├── telegram_bot.py        # Alert functions
├── trade_tracker.py       # Catat sinyal, monitor TP/SL, winrate
├── trades.json            # Data hasil tracking (auto-generated)
└── requirements.txt
```

---

## Catatan

- Binance API hanya butuh permission **Read Info** (tidak trading otomatis)
- `config.py` di-gitignore, jangan pernah di-push ke repo
- Sedang dalam fase **paper trading** — observasi winrate sebelum real trade
