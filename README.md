# Vortex

AI trading signal agent untuk crypto & gold — scanner only, tidak eksekusi trade otomatis.

Menjalankan **3 strategi** secara paralel setiap scan cycle dengan risk management terintegrasi di **13 pairs** (12 crypto + XAUUSDT).

---

## Strategi

### Strategy 1 — Fresh Liquidity Grab + Rejection

1. **Zone Detection (4H)** — identifikasi swing high/low yang:
   - Terbentuk ≥10 candle lalu (`is_fresh`)
   - Belum pernah dikunjungi lagi setelah terbentuk (`is_mitigated = False`)
2. **HTF Trend Filter** — 4H EMA50 + BTC EMA200 1W harus searah (double confirmation)
3. **Touch Alert (30m)** — deteksi ketika harga memasuki area zona
4. **Entry Signal (5m)** — konfirmasi 2 candle:
   - Candle 1: false breakout (spike tembus zona, close kembali masuk dengan strength ≥30%)
   - Candle 2: konfirmasi arah (bullish untuk LONG, bearish untuk SHORT)
5. **Trade Calculation** — SL tepat di luar batas zona, TP ke next liquidity dari struktur pasar (fallback ke 1:1.5 RR)

### Strategy 2 — Wick Fill (LONG & SHORT)

1. **Wick Detection (1W / 1D / 4H)** — identifikasi candle dengan long wick signifikan:
   - **LONG**: downside wick — lower wick ≥ 1.5x body, ATAU ≥ 30% total range
   - **SHORT**: upside wick — upper wick ≥ 1.5x body, ATAU ≥ 30% total range
   - Wick belum pernah ditest ulang (`is_wick_mitigated = False`)
2. **Entry Zone** — LONG: antara wick low dan 50% level | SHORT: antara wick high dan 50% level
3. **Entry Signal** — harga masuk entry zone + konfirmasi rejection di 5m
4. **Trade Calculation**:
   - LONG: SL 0.8% di bawah wick low, TP1: 50% fill, TP2: 100% fill (body bottom)
   - SHORT: SL 0.8% di atas wick high, TP1: 50% fill, TP2: 100% fill (body top)
5. **Confluence Scoring** — dekat EMA50 (+2), massive wick (+1), TF lebih tinggi (+1/+2)

### Strategy 3 — FVG Reclaim after Liquidity Sweep

1. **Liquidity Sweep Detection (4H)** — candle menembus swing low/high lalu close kembali di dalam (false breakout di level penting)
2. **FVG Detection** — setelah sweep, cari displacement candle yang meninggalkan Fair Value Gap (imbalance):
   - Bullish FVG: `high[i+2] < low[i]` → zone = `[high[i+2], low[i]]`
   - Bearish FVG: `low[i+2] > high[i]` → zone = `[high[i], low[i+2]]`
3. **Reclaim** — harga retrace ke FVG zone = entry opportunity
4. **Entry Signal** — rejection candle di 5m (shared dengan Strat 1 & 2)
5. **Confluence Scoring (max 10)**: base sweep+FVG (5) + HTF 4H alignment (+2) + Strat 2 wick overlap (+2) + FVG size > ATR (+1)
6. Signal hanya dikirim jika score ≥ 7

---

## Risk Management

Terintegrasi di semua strategi via `RiskManager` singleton:

| Gate | Kondisi | Tindakan |
|------|---------|----------|
| Min RR | RR < 2.0 (atau override per pair) | Reject signal |
| ATR SL | SL < 0.5× ATR | Reject signal (SL terlalu sempit) |
| Max Open | Open trades ≥ 5 | Reject signal |
| Daily Risk | Total risk hari ini + risk baru > 3% | Reject signal |

**Position sizing**: `risk_amount / sl_pct` → notional USDT face value

---

## Pairs

```
BTCUSDT  ETHUSDT  BNBUSDT  SOLUSDT
XRPUSDT  ADAUSDT  AVAXUSDT DOGEUSDT
DOTUSDT  LINKUSDT MATICUSDT ATOMUSDT
XAUUSDT  (Gold — parameter khusus, session filter aktif)
```

**XAUUSDT — Treatment Khusus:**

| Parameter | Crypto default | XAUUSDT |
|-----------|---------------|---------|
| Risk per trade | 1% | 0.5% (volatility lebih tinggi) |
| Min RR | 2.0 | 2.5 (lebih selektif) |
| ATR SL multiplier | 0.5× | 1.0× (stop hunt lebih ganas) |
| Volume spike required | Ya | Tidak (volume gold di Binance kurang reliable) |
| Touch threshold | 0.3% | 0.5% (spread lebih besar) |
| Macro filter | BTC EMA200 1W | Gold EMA50 4H sendiri (tidak ikut BTC) |
| Session filter | Tidak | Ya — skip Jumat 21:00 UTC → Minggu 22:00 UTC (weekend gap) |

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

### 3. Buat config.py

```python
# Telegram
TELEGRAM_BOT_TOKEN = "your_bot_token"   # dari @BotFather
TELEGRAM_CHAT_ID   = "your_chat_id"     # dari @userinfobot

# Binance (read-only)
BINANCE_API_KEY    = "your_api_key"
BINANCE_API_SECRET = "your_api_secret"

# Pairs
CRYPTO_PAIRS = ["BTCUSDT", "ETHUSDT", ...]

# Risk
ACCOUNT_BALANCE    = 10000.0
RISK_PCT_DEFAULT   = 1.0
MAX_DAILY_RISK_PCT = 3.0
MAX_OPEN_TRADES    = 5
MIN_RR_RATIO       = 2.0
```

### 4. Jalankan

```bash
python3 -u scanner.py
```

Background di VPS:

```bash
python3 -u scanner.py > /tmp/scanner.log 2>&1 &
tail -f /tmp/scanner.log
```

---

## Alert Telegram

| Alert | Strategi | Keterangan |
|-------|----------|------------|
| ⚠️ TOUCH | S1 | Harga memasuki zona liquidity |
| ✅ [S1] ENTRY SIGNAL | S1 | Rejection confirmed + searah HTF |
| 🕯️ [S2] WICK DETECTED | S2 | Long wick terdeteksi (LONG downside / SHORT upside) |
| ✅ [S2] WICK FILL ENTRY | S2 | Harga masuk entry zone + rejection 5m |
| 🔷 [S3] FVG SETUP | S3 | Liquidity sweep + FVG terdeteksi |
| ✅ [S3] FVG ENTRY | S3 | Harga retrace ke FVG + rejection 5m |
| ✅/❌ [Sx] WIN/LOSS | Semua | Hasil trade setelah hit TP atau SL |
| 📊 WINRATE | Semua | Laporan harian (sekali per hari) — total + breakdown per strategi |

---

## File Structure

```
├── config.py              # Kredensial & parameter (gitignored, jangan di-push)
├── scanner.py             # Main loop — VortexScanner class, 3 strategi per pair
├── strategy1_liquidity.py # S1: Liquidity Grab + shared utilities (ATR, candles, zones)
├── strategy2_wick.py      # S2: Wick Fill (1W/1D/4H)
├── strategy3_fvg.py       # S3: FVG Reclaim after Liquidity Sweep
├── risk_manager.py        # RiskManager: position sizing + 4 risk gates
├── telegram_bot.py        # Alert S1 + stats
├── wick_alerts.py         # Alert S2
├── fvg_alerts.py          # Alert S3
├── trade_tracker.py       # Catat sinyal, monitor TP/SL, winrate + per-strategy breakdown
├── trades.json            # Data hasil tracking (auto-generated, auto-trimmed 500 entries)
└── requirements.txt
```

---

## Catatan

- Binance API hanya butuh permission **Read Info** — tidak ada trading otomatis
- `config.py` di-gitignore, jangan pernah di-push ke repo
- BTC macro (EMA200 1W) di-cache 1 jam — tidak fetch API tiap cycle
- `trades.json` otomatis trim ke 500 closed trades terbaru setiap hari
- Startup warmup: wick & FVG yang sudah ada di-seed ke `_seen_wick`/`_seen_fvg` (permanent set) saat launch — alert "DETECTED" hanya fire sekali per wick candle unik, tidak blast ulang setelah cooldown expire
- Sedang dalam fase **paper trading** — observasi winrate sebelum real trade
- Target winrate >45-50% sebelum pertimbangkan real trade
