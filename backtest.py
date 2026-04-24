#!/usr/bin/env python3
"""
backtest.py — P3.1: Vortex Backtesting Framework

Fetches historical candles from Binance, replays strategy detection logic
over a sliding window, simulates entry/exit, reports metrics.

Usage:
  python3 backtest.py --pair BTCUSDT --strategy S3 --days 90
  python3 backtest.py --pair ETHUSDT --strategy all --days 60

How it works:
  1. Fetch N days of historical candles (all TFs)
  2. Slide a window over 1H candles (or entry TF)
  3. At each step, patch get_candles() to return historical slice
  4. Run strategy scan function → collect signals
  5. Simulate TP/SL hits using subsequent candles
  6. Report: winrate, avg RR, max drawdown, false signal rate
"""

import argparse
import functools
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

# ── Setup path ───────────────────────────────────────────────────────────────
VORTEX_DIR = Path(__file__).parent
sys.path.insert(0, str(VORTEX_DIR))

# Load env vars
_env_file = VORTEX_DIR / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


# ── Imports (after env setup) ─────────────────────────────────────────────────
import strategy1_liquidity as _s1l  # source of get_candles + client
from vortex_logger import get_logger

log = get_logger("backtest")

TF_MAP = _s1l.TF_MAP

STRATEGY_CHOICES = ["S1", "S2", "S3", "S4", "S5", "all"]  # S6 merged into S4


# ── Data Fetching ─────────────────────────────────────────────────────────────

def fetch_history(pair: str, days: int = 90) -> dict[str, list]:
    """Fetch historical OHLCV for all TFs. Returns {tf: [candle_dicts]}."""
    from binance.client import Client

    # Limits per TF to cover 'days'
    tf_limits = {
        "5m":  min(1000, days * 288),
        "15m": min(1000, days * 96),
        "30m": min(1000, days * 48),
        "1h":  min(1000, days * 24),
        "4h":  min(1000, days * 6),
        "1d":  min(1000, days),
        "1w":  min(500,  days // 7 + 4),
    }

    history: dict[str, list] = {}
    log.info(f"[BACKTEST] Fetching {pair} history ({days}d)...")

    for tf, limit in tf_limits.items():
        try:
            raw = _s1l.client.get_klines(
                symbol=pair, interval=TF_MAP[tf], limit=limit
            )
            history[tf] = [
                {
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                }
                for c in raw
            ]
            log.info(f"  {tf}: {len(history[tf])} candles")
        except Exception as e:
            log.warning(f"  {tf}: fetch failed — {e}")
            history[tf] = []

    return history


# ── Mock candle provider ──────────────────────────────────────────────────────

def make_mock_get_candles(history: dict, end_idx_1h: int):
    """
    Returns a patched get_candles() that serves historical slices up to end_idx_1h.
    end_idx_1h is the current 1H candle index in the walk-forward.
    Other TFs are scaled proportionally.
    """
    tf_ratios = {"5m": 12, "15m": 4, "30m": 2, "1h": 1,
                 "4h": 0.25, "1d": 1/24, "1w": 1/168}

    def mock(pair: str, tf: str, limit: int = 100) -> list:
        ratio = tf_ratios.get(tf, 1.0)
        end   = max(1, int(end_idx_1h * ratio))
        data  = history.get(tf, [])
        end   = min(end, len(data))
        return data[max(0, end - limit): end]

    return mock


# ── Signal simulation ─────────────────────────────────────────────────────────

def simulate_exit(direction: str, entry: float, sl: float, tp: float,
                  future_candles: list) -> str | None:
    """
    Walk future candles to find first TP or SL hit.
    Returns "WIN", "LOSS", or None (still open after all candles).
    """
    for c in future_candles:
        if direction == "LONG":
            if c["high"] >= tp:
                return "WIN"
            if c["low"] <= sl:
                return "LOSS"
        else:
            if c["low"] <= tp:
                return "WIN"
            if c["high"] >= sl:
                return "LOSS"
    return None


# ── Strategy runners ──────────────────────────────────────────────────────────

def _run_strategy(pair: str, strategy: str, history: dict,
                  start_window: int = 50) -> list[dict]:
    """
    Walk forward over 1H candles, run strategy at each step, collect signals.
    Returns list of {direction, entry, sl, tp, score, step_idx}.
    """
    signals = []
    hist_1h = history.get("1h", [])
    total   = len(hist_1h)

    if total < start_window + 10:
        log.warning(f"[BACKTEST] Insufficient 1H candles: {total}")
        return []

    seen_zones: set[str] = set()  # deduplicate same zone across steps

    for i in range(start_window, total):
        mock_fn = make_mock_get_candles(history, i)
        orig    = _s1l.get_candles
        _s1l.get_candles = mock_fn  # patch

        try:
            found = _detect(pair, strategy)
        except Exception as e:
            found = []
        finally:
            _s1l.get_candles = orig  # restore

        for sig in found:
            zone_key = f"{sig['direction']}_{sig['entry']:.4f}"
            if zone_key in seen_zones:
                continue
            seen_zones.add(zone_key)
            signals.append({**sig, "step_idx": i})

    return signals


def _detect(pair: str, strategy: str) -> list[dict]:
    """Call the appropriate strategy scan function. Returns list of signal dicts."""
    results = []

    if strategy in ("S3", "all"):
        from strategy3_fvg_imbalance import scan_fvg_imbalance
        for s in scan_fvg_imbalance(pair):
            if s.get("in_zone") and s.get("confidence_score", 0) >= 7.0:
                t = s.get("trade", {})
                if t.get("entry") and t.get("sl") and t.get("tp2"):
                    results.append({
                        "strategy": "S3",
                        "direction": s["direction"],
                        "entry":  t["entry"],
                        "sl":     t["sl"],
                        "tp":     t["tp2"],
                        "score":  s.get("confidence_score", 0),
                    })

    if strategy in ("S2", "all"):
        from strategy2_wick import scan_wick_setups
        for s in scan_wick_setups(pair):
            if s.get("in_entry_zone"):
                t = s.get("trade", {})
                if t.get("entry") and t.get("sl") and t.get("tp2"):
                    results.append({
                        "strategy": "S2",
                        "direction": s.get("direction", "LONG"),
                        "entry":  t["entry"],
                        "sl":     t["sl"],
                        "tp":     t["tp2"],
                        "score":  s.get("confluence_score", 0) * 2.0,
                    })

    if strategy in ("S4", "all"):
        # S4 = merged OB/BB/BOS/CHOCH (ex-S4 + ex-S6)
        from strategy4_ob_bos import scan_ob_bos
        for s in scan_ob_bos(pair):
            if s.get("in_zone") and s.get("confidence_score", 0) >= 8.0:
                t = s.get("trade", {})
                if t.get("entry") and t.get("sl") and t.get("tp2"):
                    results.append({
                        "strategy": f"S4-{s.get('entry_mode', 'RETEST')}",
                        "direction": s["direction"],
                        "entry":  t["entry"],
                        "sl":     t["sl"],
                        "tp":     t["tp2"],
                        "score":  s.get("confidence_score", 0),
                    })

    if strategy in ("S5", "all"):
        from strategy5_engineered import scan_engineered
        for s in scan_engineered(pair):
            if s.get("in_zone") and s.get("confidence_score", 0) >= 8.0:
                t = s.get("trade", {})
                if t.get("entry") and t.get("sl") and t.get("tp2"):
                    results.append({
                        "strategy": "S5",
                        "direction": s["direction"],
                        "entry":  t["entry"],
                        "sl":     t["sl"],
                        "tp":     t["tp2"],
                        "score":  s.get("confidence_score", 0),
                    })

    return results


# ── Metrics ───────────────────────────────────────────────────────────────────

def compute_metrics(signals: list[dict], history: dict) -> dict:
    hist_1h = history.get("1h", [])
    results = []

    for sig in signals:
        i        = sig["step_idx"]
        future   = hist_1h[i: i + 100]  # up to 100 candles forward (~4 days on 1H)
        outcome  = simulate_exit(sig["direction"], sig["entry"], sig["sl"], sig["tp"], future)
        candles  = next(
            (j + 1 for j, c in enumerate(future)
             if (sig["direction"] == "LONG" and (c["high"] >= sig["tp"] or c["low"] <= sig["sl"]))
             or (sig["direction"] == "SHORT" and (c["low"] <= sig["tp"] or c["high"] >= sig["sl"]))),
            None,
        )
        rr = abs(sig["tp"] - sig["entry"]) / max(abs(sig["entry"] - sig["sl"]), 1e-10)
        results.append({**sig, "outcome": outcome, "rr": round(rr, 2), "candles": candles})

    closed  = [r for r in results if r["outcome"] is not None]
    wins    = [r for r in closed if r["outcome"] == "WIN"]
    losses  = [r for r in closed if r["outcome"] == "LOSS"]
    false_s = [r for r in losses if (r["candles"] or 999) < 5]

    total    = len(closed)
    win_n    = len(wins)
    winrate  = round(win_n / total * 100, 1) if total else 0.0
    avg_rr_v = round(sum(r["rr"] for r in closed) / total, 2) if total else 0.0
    fsr      = round(len(false_s) / total * 100, 1) if total else 0.0

    # Max drawdown (simplified equity curve)
    equity = 1.0
    peak   = 1.0
    max_dd = 0.0
    for r in closed:
        risk = 0.01  # assume 1% risk per trade
        if r["outcome"] == "WIN":
            equity *= (1 + risk * r["rr"])
        else:
            equity *= (1 - risk)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        "total_signals": len(signals),
        "closed":        total,
        "open":          len(signals) - total,
        "wins":          win_n,
        "losses":        len(losses),
        "winrate":       winrate,
        "avg_rr":        avg_rr_v,
        "false_signal_rate": fsr,
        "max_drawdown":  round(max_dd, 2),
        "breakeven_rr2": "~33% WR needed",
        "trades":        results,
    }


def print_report(pair: str, strategy: str, days: int, metrics: dict):
    print("\n" + "=" * 55)
    print(f"  VORTEX BACKTEST — {pair} | {strategy} | {days}d")
    print("=" * 55)
    print(f"  Total signals  : {metrics['total_signals']}")
    print(f"  Closed trades  : {metrics['closed']}")
    print(f"  Still open     : {metrics['open']}")
    print(f"  Wins           : {metrics['wins']}")
    print(f"  Losses         : {metrics['losses']}")
    print(f"  Winrate        : {metrics['winrate']}%")
    print(f"  Avg RR         : {metrics['avg_rr']}")
    print(f"  False sig rate : {metrics['false_signal_rate']}%")
    print(f"  Max drawdown   : {metrics['max_drawdown']}%")
    print("=" * 55)

    # Per-strategy breakdown if "all"
    if strategy == "all":
        by_strat: dict[str, dict] = {}
        for t in metrics["trades"]:
            s = t.get("strategy", "?")
            if s not in by_strat:
                by_strat[s] = {"wins": 0, "losses": 0, "total": 0}
            if t["outcome"] == "WIN":
                by_strat[s]["wins"] += 1
            elif t["outcome"] == "LOSS":
                by_strat[s]["losses"] += 1
            if t["outcome"] is not None:
                by_strat[s]["total"] += 1
        print("\n  Per-Strategy:")
        for s, d in sorted(by_strat.items()):
            wr = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0.0
            print(f"    {s:10s}  {d['wins']}W {d['losses']}L  WR={wr}%  n={d['total']}")
        print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Vortex Backtester")
    parser.add_argument("--pair",     default="BTCUSDT",  help="Trading pair")
    parser.add_argument("--strategy", default="S3",
                        choices=STRATEGY_CHOICES,          help="Strategy to backtest")
    parser.add_argument("--days",     type=int, default=90, help="History days to fetch")
    args = parser.parse_args()

    history = fetch_history(args.pair, args.days)
    if not history.get("1h"):
        log.error("No 1H data — cannot run backtest.")
        sys.exit(1)

    strats = list(set(STRATEGY_CHOICES) - {"all"}) if args.strategy == "all" else [args.strategy]

    all_signals = []
    for s in strats:
        log.info(f"[BACKTEST] Running {s} on {args.pair}...")
        sigs = _run_strategy(args.pair, s, history)
        log.info(f"  → {len(sigs)} signals found")
        all_signals.extend(sigs)

    metrics = compute_metrics(all_signals, history)
    print_report(args.pair, args.strategy, args.days, metrics)


if __name__ == "__main__":
    main()
