# CLAUDE.md — Vortex Trading Agent

## Project
AI trading signal agent (scanner only, no auto-execute) untuk crypto.
Repo: https://github.com/jajajak12/Vortex
VPS: /home/prospera/vortex

## Stack
- Python 3.12, Binance API (read-only), Telegram Bot
- Dependencies: python-binance==1.0.19, requests==2.31.0, numpy==1.26.4
- Install: `pip install --break-system-packages -r requirements.txt` (no venv needed, sudah installed)

## Menjalankan Scanner
```bash
cd ~/vortex
python3 -u scanner.py > /tmp/scanner.log 2>&1 &
tail -f /tmp/scanner.log
```

## File Structure
- `config.py` — kredensial & parameter (di-.gitignore, JANGAN push)
- `scanner.py` — main loop, scan setiap 60s
- `strategy1_liquidity.py` — logika strategi + Binance API calls
- `telegram_bot.py` — alert functions
- `trade_tracker.py` — catat signal, monitor TP/SL, hitung winrate
- `trades.json` — hasil tracking (auto-generated saat runtime)

## Pairs
BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT

## Strategi: Fresh Liquidity Grab + Rejection
1. **Zone detection** (4H): cari swing high/low yang belum tersentuh ≥10 candle
2. **Touch alert** (30m): harga masuk ke range zona [low-buffer, high+buffer]
3. **Entry signal** (5m): false breakout — candle tembus zona lalu close kembali di dalam
4. **Trade calc**: SL di liquidity sebelumnya, TP = 1:1 RR
5. **Winrate tracking**: setiap signal dicatat di trades.json, monitor TP/SL hit otomatis

## Winrate Report
Dikirim otomatis ke Telegram setiap 60 scan (~1 jam).

## Git / Push
```bash
git add <files>   # jangan add config.py
git commit -m "..."
git remote set-url origin https://jajajak12:<PAT>@github.com/jajajak12/Vortex.git
git push origin main
git remote set-url origin https://github.com/jajajak12/Vortex.git  # reset token setelah push
```
PAT tersimpan di sesi user (tanya user jika tidak ada).

## Rencana ke Depan
- Observasi winrate dulu (paper trading) selama 2-4 minggu
- Jika winrate >45-50%, pertimbangkan real trade
- Potensi improvement: filter trend HTF, RR 1:2, backtesting historis
