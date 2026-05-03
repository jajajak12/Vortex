# Vortex Trading Agent

AI-powered crypto scanner for Binance Spot — paper trading, 6 strategies.

## Setup

```bash
cd ~/vortex
pip install --break-system-packages -r requirements.txt
cp config.example.py config.py   # add your API keys + Telegram bot token
python3 -u scanner.py > /tmp/vortex.log 2>&1 &
tail -f /tmp/vortex.log
```

## Strategies

| ID | Name | TF | Entry | Min Score | Description |
|----|------|----|-------|-----------|-------------|
| S1 | Liquidity Grab | 4H→30m | 30m | 6.0 | Fresh zone touch + 5m rejection |
| S2 | Wick Fill | 4H→30m | 30m | 7.0 | Wick zone entry + 5m rejection |
| S3 | FVG Reclaim | 4H→30m | 30m | 7.0 | FVG + Imbalance after sweep |
| S4 | V Pattern | 4H→30m | 30m | 8.0 | V-bottom/V-top pattern |
| S4 | Order Block | 4H→30m | 30m | 8.0 | OB + Breaker Block retest |
| S5 | Engineered | 4H→30m | 30m | 7.5 | Compression + engineered sweep |
| S6 | BOS + MSS | 4H→30m | 30m | 8.0 | Break of structure + MSS/CHOCH |

## Strategy Ownership (Overlap Prevention)

- **S4 (OB)**: owns REACTIVE RETESTS at broken structure
- **S5 (Eng)**: owns COMPRESSION + ENGINEERED SWEEPS (aggressive)
- **S6 (BOS)**: owns MOMENTUM BREAKS + HOLD (NOT reactive retests)

Overlapping zones → S4 fires first, S5/S6 skip via `_seen_ob` handshake.

## Detection Logic

| Strategy | Detect | Confirm | Entry |
|----------|--------|---------|-------|
| S1 Liquidity | 4H swing zones | 1H zone touch | 30m false breakout |
| S2 Wick | 4H wick | 1H zone in | 30m rejection |
| S3 FVG | 4H FVG/imbalance | 1H in zone | 30m wick rejection |
| S4 OB | 4H order block | 1H touched | 30m rejection |
| S5 Eng | 4H compression | 1H sweep | 30m reclaim |
| S6 BOS | 4H structure break | 1H hold | 30m displacement |

## Hard Gates

- TP1 max 1:3.0, TP2 max 1:4.8 (all strategies)
- Volume spike required for all entries
- Wick rejection MANDATORY at 30m (S3, S4-OB, S5, S6)
- Macro filter: skip opposite-BTC-regime setups

## Scoring (base 5.0, min 7.0–8.0 per strategy)

```
+ Price inside zone:          +0.5 to +1.5
+ HTF 4H aligned:             +1.5
+ Cross-strategy confluence:   +1.0 to +2.0
+ Volume spike 1.5x+:         +0.5 to +1.0
+ Structural level:           +0.5
+ 2+ confluence bonus:        +0.5 to +1.0
```

## S3 Upgraded Thresholds (More Entries)

- FVG lookback: 6 candles (tighter)
- FVG min ATR: 25% (was 30%)
- Imbalance min ATR: 40% (was 50%)
- Displacement body: 50% (was 55%)
- Displacement volume: 1.3x (was 1.5x)
- Entry zone tolerance: 20% ATR (was 25%)
- Inside-zone score bonus: +1.0 (was +0.5)

## Files

```
config.py              — API keys, thresholds (gitignored)
scanner.py             — main loop + all strategy scanners
strategy_utils.py      — shared candle, ATR, swing, macro helpers
strategy1_bos_mss.py   — S1 BOS+MSS momentum (RR 1:1)
strategy2_ema_stack.py — S2 EMA stack (RR 1:2)
strategy3_p10_swing.py — S3 P10 swing reversal (RR 1:1)
strategy4_vol_surge_bear.py — S4 volume surge bear short (RR 1:2)
strategy5_vol_impulse.py    — S5 volume impulse bull close-high (RR 1:2)
strategy6_donchian_breakout.py — S6 50-period Donchian breakout long (RR 1:2)
risk_manager.py        — position sizing + 4 gate checks
telegram_bot.py        — alerts (DETECTED disabled, only ENTRY)
trade_tracker.py       — trades.json + winrate tracking
core/signal_handler.py — unified Signal + Telegram delivery
```

## Telegram Alerts

Only actionable alerts sent to Telegram:

- `⚠️ TOUCH` — price hit S1 zone (no entry yet)
- `✅ ENTRY [S1–S6]` — rejection confirmed, ready to trade
- `✅ WIN / ❌ LOSS` — TP/SL hit automatically
- `📊 WINRATE` — daily report

## Git Push

```bash
git add <files>    # don't add config.py
git commit -m "..."
source <(python3 -c "import config; print(f'PAT={config.GITHUB_PAT}')")
git remote set-url origin https://jajajak12:${PAT}@github.com/jajajak12/Vortex.git
git push origin main
git remote set-url origin https://github.com/jajajak12/Vortex.git
```

## Phase 1 Status

Paper trading 13 pairs. Target: winrate >45–50% before Phase 2 automation.
