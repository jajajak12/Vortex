# CLAUDE.md — Vortex Trading Agent

Caveman mode ON.
Reply short and direct. No fluff. No "happy to help". No long explanations unless I ask "why" or "explain".
No repeated headers. No tables. No emojis. No long stories.
Output ONLY the changed code + max 2 lines summary at the end.
You are Vortex Senior Quant Engineer.

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
- `scanner.py` — main loop + VortexScanner class (warmup, session filter, 4 strategi)
- `strategy1_liquidity.py` — S1 logic + shared utilities (candles, ATR, zones, rejection check)
- `strategy2_wick.py` — S2 Wick Fill (1W/1D/4H, LONG & SHORT)
- `strategy3_fvg.py` — S3 FVG Reclaim after Liquidity Sweep
- `risk_manager.py` — RiskManager: position sizing + 4 gate (RR, ATR SL, max open, daily risk)
- `telegram_bot.py` / `wick_alerts.py` / `fvg_alerts.py` / `alerts/` — alert per strategi
- `trade_tracker.py` — catat signal, monitor TP/SL, hitung winrate per strategi
- `trades.json` — hasil tracking (auto-generated saat runtime)

## Pairs Aktif
BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT,
XRPUSDT, ADAUSDT, AVAXUSDT, DOGEUSDT,
DOTUSDT, LINKUSDT, MATICUSDT, ATOMUSDT,
XAUUSDT (gold — session filter + own macro)

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
- ⚠️ TOUCH — harga menyentuh zona liquidity (S1)
- ✅ ENTRY SIGNAL — rejection confirmed (S1/S2/S3/S4)
- ✅ WIN / ❌ LOSS — hasil trade otomatis
- 📊 WINRATE — laporan harian

> **Catatan**: Alert "DETECTED" (wick/FVG/OB ditemukan) dinonaktifkan — tidak actionable
> sebelum harga masuk zona. Hanya ENTRY yang dikirim ke Telegram.

## Bug yang Sudah Difix
1. `is_touching_zone` — price harus dalam [low-buffer, high+buffer], bukan satu sisi
2. `is_mitigated` — zona yang sudah dikunjungi ulang setelah terbentuk dibuang
3. SL placement — pakai zone boundary, bukan prev liquidity (dulu bisa 7%+ jauh)
4. Daily risk calc, pair-specific touch threshold, S2 SHORT wick detection
5. Startup alert blast — `_warmup()` pre-seed `_seen_wick`/`_seen_fvg`/`_seen_ob` saat launch
6. Cooldown expiry blast — replaced with permanent seen set (no blast on cooldown expiry)
7. Telegram spam — DETECTED alerts (wick/FVG) disabled; only actionable ENTRY sent to Telegram.
8. S4 V Pattern removed — replaced with Order Block + Breaker Block (Apr 2026)

## Git / Push
PAT disimpan di `config.py` (GITHUB_PAT) — tidak perlu paste manual.
```bash
git add <files>   # jangan add config.py
git commit -m "..."
source <(python3 -c "import config; print(f'PAT={config.GITHUB_PAT}')")
git remote set-url origin https://jajajak12:${PAT}@github.com/jajajak12/Vortex.git
git push origin main
git remote set-url origin https://github.com/jajajak12/Vortex.git
```

## XAUUSDT — Catatan Penting
- `OWN_MACRO_PAIRS` — pakai htf_bias EMA50 4H gold sendiri, bukan BTC EMA200 1W
- `SESSION_FILTER_PAIRS` — skip scan saat weekend gap: Jumat 21:00 UTC → Minggu 22:00 UTC
- Data masih Spot client — ganti ke `futures_klines()` nanti jika perlu lebih liquid
- PAIR_OVERRIDES di config sudah ada: risk 0.5%, min RR 2.5, ATR SL 1×, no vol spike

## Rencana ke Depan
- Phase 1 (sekarang): paper trading 13 pairs, observasi winrate 2-4 minggu
- Phase 2 (jika winrate bagus): automation order execution
  - Perlu: `order_executor.py`, Binance write API key, position reconciliation
- Target winrate >45-50% sebelum real trade
