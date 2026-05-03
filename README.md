# Vortex Trading Agent

AI-powered crypto scanner for Binance Spot. Paper-trading scanner only, no auto-execution.

## Setup

```bash
cd ~/vortex
pip install --break-system-packages -r requirements.txt
cp config.example.py config.py   # add your API keys + Telegram bot token
python3 -u scanner.py > /tmp/vortex.log 2>&1 &
tail -f /tmp/vortex.log
```

## Active Strategies

Final S1-S6 mapping from 2-cycle backtest validation:

| ID | Strategy | Direction | Base TF | RR | File |
|----|----------|-----------|---------|----|------|
| S1 | S4-MOMENTUM BOS+MSS | LONG/SHORT | 4H | 1:1 | `strategy1_bos_mss.py` |
| S2 | S6 EMA Stack | LONG/SHORT | 4H | 1:2 | `strategy2_ema_stack.py` |
| S3 | S7 P10 Swing Reversal | LONG/SHORT | 1H | 1:1 | `strategy3_p10_swing.py` |
| S4 | S8 Volume Surge Bear SHORT | SHORT | 4H | 1:2 | `strategy4_vol_surge_bear.py` |
| S5 | volume_impulse_bull_close_high | LONG | 4H | 1:2 | `strategy5_vol_impulse.py` |
| S6 | donchian_breakout 50-period | LONG | 4H | 1:2 | `strategy6_donchian_breakout.py` |

## Detection Logic

| Strategy | Detect | Confirm | Entry |
|----------|--------|---------|-------|
| S1 BOS+MSS | 4H BOS/CHOCH momentum break | 4H holds beyond structure | 30m signal alert |
| S2 EMA Stack | 1W/1D trend alignment + 4H EMA20 pullback | 1H bounce from EMA20 | 30m signal alert |
| S3 P10 Swing | 1H 20-bar swing high/low touch | high-volume reversal candle in London/NY session | 1H close |
| S4 Volume Surge Bear | 4H bearish surge near 50-bar high | volume/body/close-low filters | 4H close |
| S5 Volume Impulse | 4H bullish impulse candle | volume expansion + close near high | 4H close |
| S6 Donchian Breakout | 4H close above prior 50-candle high | volume filter | 4H close |

## Hard Gates

- Each strategy uses its own validated minimum RR:
  - S1: `1.0`
  - S2: `2.0`
  - S3: `1.0`
  - S4: `2.0`
  - S5: `2.0`
  - S6: `2.0`
- Weight gate: active strategy keys are `S1` through `S6`, all default `1.0`.
- Risk manager checks minimum RR, ATR SL distance, max open trades, and daily risk.
- Macro filter skips setups against BTC regime unless the pair is configured as own-macro.

## Scoring

Each strategy emits a 1-10 confidence score. `strategy_runner.py` applies:

- lesson score modifier
- strategy weight gate
- duplicate-open-trade check
- setup cooldown
- risk manager approval

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

- `ENTRY [S1-S6]` — setup approved by strategy, weight gate, and risk manager
- `WIN / LOSS` — TP/SL hit automatically
- `WINRATE` — daily report

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
