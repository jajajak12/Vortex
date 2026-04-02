# CLAUDE.md — Vortex Trading Agent

## Project
AI trading signal agent (scanner only, no auto-execute) untuk crypto.
Repo: https://github.com/jajajak12/Vortex
VPS: /home/prospera/vortex

## Stack
- Python 3.12, Binance API (read-only), Telegram Bot
- Dependencies: python-binance==1.0.19, requests==2.31.0, numpy==1.26.4
- Install: `pip install --break-system-packages -r requirements.txt` (no venv, sudah installed)

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
- `telegram_bot.py` — alert functions (touch, entry, result, stats)
- `trade_tracker.py` — catat signal, monitor TP/SL, hitung winrate
- `trades.json` — hasil tracking (auto-generated saat runtime)

## Pairs Aktif
BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT

## Strategi: Fresh Liquidity Grab + Rejection
1. **Zone detection** (4H): cari swing high/low yang:
   - Terbentuk ≥10 candle lalu (`is_fresh`)
   - Belum pernah dikunjungi lagi setelah terbentuk (`is_mitigated` = False)
2. **Touch alert** (30m): harga masuk range zona [low-buffer, high+buffer] — harus dalam range, bukan hanya satu sisi
3. **Entry signal** (5m): false breakout — candle tembus zona lalu close kembali di dalam
4. **Trade calc**:
   - SL: tepat di luar batas zona itu sendiri (LONG: zone.low × 0.998 / SHORT: zone.high × 1.002)
   - TP: 1:1 RR dari SL distance
5. **Winrate tracking**: setiap signal dicatat di trades.json, monitor TP/SL hit otomatis, report ke Telegram setiap 60 scan (~1 jam)

## Alert Telegram
- ⚠️ TOUCH — harga menyentuh zona
- ✅ ENTRY SIGNAL — rejection confirmed
- ✅ WIN / ❌ LOSS — hasil trade otomatis
- 📊 WINRATE — laporan tiap ~1 jam

## Bug yang Sudah Difix
1. `is_touching_zone` — sebelumnya tidak ada batas bawah/atas, sekarang price harus dalam [low-buffer, high+buffer]
2. `is_mitigated` — zona yang sudah pernah dikunjungi setelah terbentuk dibuang
3. SL placement — sebelumnya pakai prev liquidity yang bisa sangat jauh (contoh: 7% SL), sekarang pakai zone boundary

## Git / Push
```bash
git add <files>   # jangan add config.py
git commit -m "..."
git remote set-url origin https://jajajak12:<PAT>@github.com/jajajak12/Vortex.git
git push origin main
git remote set-url origin https://github.com/jajajak12/Vortex.git  # reset token setelah push
```
PAT: tanya user (classic token dengan scope repo).

## Rencana ke Depan
- Observasi winrate dulu (paper trading) 2-4 minggu
- Jika winrate >45-50%, pertimbangkan real trade
- Potential improvement: filter trend HTF, naikkan RR ke 1:2
