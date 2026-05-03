#!/usr/bin/env python3
"""
backtest.py — Vortex Backtester using REAL strategy code + CSV historical data.

Approach:
  1. Load CSV candles for 5 pairs × 4 TFs (1h/4h/1d/1w)
  2. Monkey-patch strategy_utils.get_candles (and all downstream imports)
     so every strategy sees time-sliced historical data instead of live API
  3. Walk 1h candles forward (every 4h step), call real scan functions
  4. Collect signals with entry/SL/TP, simulate TP/SL on subsequent 1h candles
  5. Report per strategy per pair: signals, winrate, avg hold, max DD

Missing TFs (5m/15m/30m) → 1h used as proxy. Acceptable for pattern detection.

Usage:
  python3 backtest.py                     # all pairs, all strategies
  python3 backtest.py --pairs BTCUSDT     # single pair
  python3 backtest.py --step 8            # 8h step (faster, fewer signals)
"""

import argparse
import bisect
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# ── 1. Environment setup (must happen before strategy imports) ────────────────

VORTEX_DIR = Path(__file__).parent
sys.path.insert(0, str(VORTEX_DIR))
os.chdir(VORTEX_DIR)

_env = VORTEX_DIR / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Ensure required vars exist even without live credentials
os.environ.setdefault("VORTEX_TELEGRAM_TOKEN", "dummy_backtest")
os.environ.setdefault("VORTEX_EXCHANGE", "binance")
os.environ.setdefault("VORTEX_BINANCE_KEY", "dummy_backtest")
os.environ.setdefault("VORTEX_BINANCE_SECRET", "dummy_backtest")

# ── 2. Silence live side-effect modules BEFORE strategy imports ───────────────

import unittest.mock as _mock

# telegram_bot: silence send_alert / alert_touch / alert_info
_tg_mock = _mock.MagicMock()
sys.modules.setdefault("telegram_bot", _tg_mock)

# db: get_open_trades → [] (no open trades for backtest dedup)
_db_mod = _mock.MagicMock()
_db_mod.get_open_trades.return_value = []
sys.modules.setdefault("db", _db_mod)

# trade_tracker: log_signal → no-op
_tt_mock = _mock.MagicMock()
sys.modules.setdefault("trade_tracker", _tt_mock)

# lessons_injector: get_score_modifier → 0.0
_li_mock = _mock.MagicMock()
_li_mock.get_score_modifier.return_value = 0.0
sys.modules.setdefault("lessons_injector", _li_mock)

# weights: apply_weight_gate → always approve
_wt_mock = _mock.MagicMock()
_wt_mock.apply_weight_gate.return_value = (True, 8.0)
_wt_mock.MIN_ACCEPTED_SCORE = 0.0
sys.modules.setdefault("weights", _wt_mock)

# ── 3. Import strategy modules ────────────────────────────────────────────────

import strategy_utils as _su
import strategy1_bos_mss as _s1
import strategy2_ema_stack as _s2
import strategy3_p10_swing as _s3
import strategy4_vol_surge_bear as _s4
import strategy5_vol_impulse as _s5
import strategy6_donchian_breakout as _s6

from strategy1_bos_mss import scan_bos_mss
from strategy2_ema_stack import scan_ema_stack
from strategy3_p10_swing import scan_p10_swing
from strategy4_vol_surge_bear import scan_vol_surge_bear
from strategy5_vol_impulse import scan_vol_impulse
from strategy6_donchian_breakout import scan_donchian_breakout

# ── 4. Global backtest state ──────────────────────────────────────────────────

DATA_DIR   = VORTEX_DIR / "historical_data"
REPORT_DIR = VORTEX_DIR / "analysis_reports"
REPORT_DIR.mkdir(exist_ok=True)

DEFAULT_PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XAUUSDT"]

# Loaded CSV data: pair → tf → list[candle_dict with open_time]
_DATA: dict[str, dict[str, list[dict]]] = {}
# Parallel timestamp arrays for bisect (pair → tf → [open_time, ...])
_TS:   dict[str, dict[str, list[int]]]  = {}

# Current simulation pointer (set before each strategy call)
_CUR: dict[str, Any] = {"pair": None, "ts": 0}

# ── 5. CSV loader ─────────────────────────────────────────────────────────────

def _load_csv(pair: str, tf: str) -> list[dict]:
    path = DATA_DIR / f"{pair}_{tf}.csv"
    if not path.exists():
        return []
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({
                "open_time": int(r["open_time"]),
                "open":      float(r["open"]),
                "high":      float(r["high"]),
                "low":       float(r["low"]),
                "close":     float(r["close"]),
                "volume":    float(r["volume"]),
            })
    return sorted(rows, key=lambda x: x["open_time"])


def load_all(pairs: list[str]):
    for pair in pairs:
        _DATA[pair] = {}
        _TS[pair]   = {}
        for tf in ("15m", "30m", "1h", "4h", "1d", "1w"):
            rows = _load_csv(pair, tf)
            _DATA[pair][tf] = rows
            _TS[pair][tf]   = [r["open_time"] for r in rows]
        n1h  = len(_DATA[pair]["1h"])
        n4h  = len(_DATA[pair]["4h"])
        n30m = len(_DATA[pair]["30m"])
        n15m = len(_DATA[pair]["15m"])
        print(f"  {pair}: {n1h} 1h / {n4h} 4h / {n30m} 30m / {n15m} 15m candles")


# ── 6. Mocked get_candles (time-slice aware) ──────────────────────────────────

# Fallback chain: try each TF in order until data found
# 5m → 15m → 30m → 1h (closest real data wins)
_TF_FALLBACK: dict[str, list[str]] = {
    "5m":  ["15m", "30m", "1h"],
    "15m": ["15m", "30m", "1h"],
    "30m": ["30m", "1h"],
}


def bt_get_candles(pair: str, tf: str, limit: int = 100) -> list[dict]:
    """
    Return historical candles up to _CUR['ts'] for the given pair/tf.
    Uses real 15m/30m CSV when available; falls back to 1h only when missing.
    Uses bisect for O(log n) timestamp slicing.
    """
    actual_pair = pair if pair in _DATA else _CUR.get("pair")
    if not actual_pair:
        return []

    # Resolve actual TF: use real CSV if available, else fallback chain
    if tf in _DATA.get(actual_pair, {}) and _DATA[actual_pair][tf]:
        actual_tf = tf
    else:
        for candidate in _TF_FALLBACK.get(tf, [tf]):
            if _DATA.get(actual_pair, {}).get(candidate):
                actual_tf = candidate
                break
        else:
            actual_tf  = tf  # will return [] below
    rows = _DATA.get(actual_pair, {}).get(actual_tf, [])
    if not rows:
        return []

    ts_cur = _CUR["ts"]
    if ts_cur > 0:
        ts_arr = _TS.get(actual_pair, {}).get(actual_tf, [])
        end    = bisect.bisect_left(ts_arr, ts_cur)  # index of first candle >= ts_cur
    else:
        end = len(rows)

    start = max(0, end - limit)
    # Strip open_time before returning (strategies don't expect it)
    return [
        {"open": r["open"], "high": r["high"], "low": r["low"],
         "close": r["close"], "volume": r["volume"]}
        for r in rows[start:end]
    ]


def _patch_all():
    """Replace get_candles in every module that imported it directly."""
    _su.get_candles = bt_get_candles
    _s1.get_candles  = bt_get_candles
    _s2.get_candles  = bt_get_candles
    _s3.get_candles  = bt_get_candles
    _s4.get_candles  = bt_get_candles
    _s5.get_candles  = bt_get_candles
    _s6.get_candles  = bt_get_candles
    # get_btc_macro_regime in strategy_utils also calls get_candles.


def set_tf_zone(tf: str):
    """Patch TF_ZONE into all strategy modules for parametric experiments."""
    _s1.TF_DETECT = tf
    _s2.TF_4H = tf
    _s4.TF_DETECT = tf
    _s5.TF_DETECT = tf
    _s6.TF_DETECT = tf


# ── 7. Signal extractors (one per strategy) ───────────────────────────────────

def _trade_entry_sl_tp(t: dict | None) -> tuple[float, float, float] | None:
    """Extract (entry, sl, tp) from a trade dict. Prefer tp2 over tp1."""
    if not t:
        return None
    entry = t.get("entry", 0.0)
    sl    = t.get("sl", 0.0)
    tp    = t.get("tp2") or t.get("tp1") or t.get("tp", 0.0)
    if not all([entry, sl, tp]):
        return None
    return float(entry), float(sl), float(tp)


def _sig(strategy, direction, entry, sl, tp, score, zone_key) -> dict:
    return {
        "strategy":  strategy,
        "direction": direction,
        "entry":     entry,
        "sl":        sl,
        "tp":        tp,
        "score":     score,
        "zone_key":  zone_key,
    }


def _extract_strategy(strategy: str, setups: list[dict], min_score: float = 7.0) -> list[dict]:
    sigs = []
    try:
        for s in setups:
            if not s.get("in_zone") or not s.get("trade"):
                continue
            if s.get("confidence_score", 0) < min_score:
                continue
            t = _trade_entry_sl_tp(s["trade"])
            if not t:
                continue
            sigs.append(_sig(strategy, s["direction"], *t,
                             s["confidence_score"], s.get("zone_key", "")))
    except Exception:
        pass
    return sigs


def extract_s1(pair: str, min_score: float = 8.0) -> list[dict]:
    return _extract_strategy("S1", scan_bos_mss(pair), min_score)


def extract_s2(pair: str, min_score: float = 8.0) -> list[dict]:
    return _extract_strategy("S2", scan_ema_stack(pair), min_score)


def extract_s3(pair: str, min_score: float = 7.0) -> list[dict]:
    return _extract_strategy("S3", scan_p10_swing(pair), min_score)


def extract_s4(pair: str, min_score: float = 7.0) -> list[dict]:
    return _extract_strategy("S4", scan_vol_surge_bear(pair), min_score)


def extract_s5(pair: str, min_score: float = 7.0) -> list[dict]:
    return _extract_strategy("S5", scan_vol_impulse(pair), min_score)


def extract_s6(pair: str, min_score: float = 7.0) -> list[dict]:
    return _extract_strategy("S6", scan_donchian_breakout(pair), min_score)


# ── 8. Trade simulator (wick-aware, 1h candles) ───────────────────────────────

MAX_HOLD_H = 500  # ~21 days


def simulate_trade(sig: dict, all_1h: list[dict], start_idx: int) -> dict:
    entry     = sig["entry"]
    sl        = sig["sl"]
    tp        = sig["tp"]
    direction = sig["direction"]

    for offset in range(1, MAX_HOLD_H + 1):
        idx = start_idx + offset
        if idx >= len(all_1h):
            break
        c = all_1h[idx]

        if direction == "LONG":
            if c["low"] <= sl:
                return {"result": "loss", "pnl": -1.0, "hold_h": offset, "exit": sl}
            if c["high"] >= tp:
                rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
                return {"result": "win",  "pnl": rr,   "hold_h": offset, "exit": tp}
        else:
            if c["high"] >= sl:
                return {"result": "loss", "pnl": -1.0, "hold_h": offset, "exit": sl}
            if c["low"] <= tp:
                rr = abs(tp - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0
                return {"result": "win",  "pnl": rr,   "hold_h": offset, "exit": tp}

    # Timeout: neither TP nor SL hit within MAX_HOLD_H candles
    end_c = all_1h[min(start_idx + MAX_HOLD_H, len(all_1h) - 1)]
    unreal = ((end_c["close"] - entry) / abs(entry - sl)
              if direction == "LONG"
              else (entry - end_c["close"]) / abs(sl - entry))
    return {"result": "timeout", "pnl": unreal, "hold_h": MAX_HOLD_H, "exit": end_c["close"]}


# ── 9. Pair backtest runner ───────────────────────────────────────────────────

ZONE_COOLDOWN_MS = 7 * 24 * 3600 * 1000    # 1 week — same-zone dedup
PAIR_COOLDOWN_MS = 4 * 3600 * 1000          # 4h — matches live CooldownStore.COOLDOWN


def _check_close(ot: dict, c: dict, i: int) -> bool:
    """Check if open trade hits TP or SL on 1h candle. Mutates ot on hit."""
    direction, entry, sl, tp = ot["direction"], ot["entry"], ot["sl"], ot["tp"]
    if direction == "LONG":
        if c["low"] <= sl:
            ot.update(result="loss", pnl=-1.0, hold_h=i - ot["open_1h_idx"], exit=sl)
            return True
        if c["high"] >= tp:
            rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
            ot.update(result="win", pnl=rr, hold_h=i - ot["open_1h_idx"], exit=tp)
            return True
    else:
        if c["high"] >= sl:
            ot.update(result="loss", pnl=-1.0, hold_h=i - ot["open_1h_idx"], exit=sl)
            return True
        if c["low"] <= tp:
            rr = abs(tp - entry) / abs(sl - entry) if abs(sl - entry) > 0 else 0
            ot.update(result="win", pnl=rr, hold_h=i - ot["open_1h_idx"], exit=tp)
            return True
    return False


def run_pair(pair: str, walk_step_h: int = 4,
             rr_override: float | None = None,
             min_scores: dict | None = None) -> list[dict]:
    """
    Walk 1h candles one-by-one:
      - Every candle: check if any open trade hit TP/SL (wick-aware).
      - Every walk_step_h candles: run strategy scans.
    Dedup mirrors live scanner:
      - Max 1 open trade per (strategy_prefix, direction) at a time   [_already_open()]
      - 4h per-pair-strategy cooldown after any signal fires           [cd_ob_e pair_cd_key]
      - 1-week per-zone dedup to prevent same zone re-firing           [seen_ob]
    rr_override: if set, TP = entry ± abs(entry-sl) * rr_override (overrides signal's tp).
    min_scores:  dict with optional keys s3/s4/s5/s6 for min confidence_score thresholds.
    """
    all_1h = _DATA[pair]["1h"]
    if len(all_1h) < 300:
        print(f"  {pair}: not enough data ({len(all_1h)} 1h candles)")
        return []

    # open_trades: (strat_prefix, direction) → trade dict (in-flight)
    open_trades: dict[tuple, dict] = {}
    # pair-level strategy cooldown: strat_prefix → last_signal_ts (ms)
    pair_cd: dict[str, int] = {}
    # zone dedup: zone_key → last_signal_ts (ms)
    zone_seen: dict[str, int] = {}

    completed: list[dict] = []

    for i in range(200, len(all_1h) - 1):
        c  = all_1h[i]
        ts = c["open_time"]

        # ── Close open trades that hit TP/SL this 1h candle ─────────
        for key in list(open_trades.keys()):
            ot = open_trades[key]
            if _check_close(ot, c, i):
                completed.append(ot)
                del open_trades[key]

        # ── Scan for signals every walk_step_h candles ───────────────
        if (i - 200) % walk_step_h != 0:
            continue

        _CUR["pair"] = pair
        _CUR["ts"]   = ts

        ms = min_scores or {}
        all_sigs = (
            extract_s1(pair, ms.get("s1", 8.0))
            + extract_s2(pair, ms.get("s2", 8.0))
            + extract_s3(pair, ms.get("s3", 7.0))
            + extract_s4(pair, ms.get("s4", 7.0))
            + extract_s5(pair, ms.get("s5", 7.0))
            + extract_s6(pair, ms.get("s6", 7.0))
        )

        for sig in all_sigs:
            prefix = sig["strategy"]
            direction = sig["direction"]

            # 1. 4h per-pair-strategy cooldown (matches live cd_ob_e pair_cd_key)
            if ts - pair_cd.get(prefix, 0) < PAIR_COOLDOWN_MS:
                continue

            # 2. Max 1 open trade per (prefix, direction) (matches _already_open())
            if (prefix, direction) in open_trades:
                continue

            # 3. 1-week zone dedup
            dk = f"{sig['strategy']}_{direction}_{sig['zone_key']}"
            if ts - zone_seen.get(dk, 0) < ZONE_COOLDOWN_MS:
                continue

            # ── Accept signal ─────────────────────────────────────────
            # Apply RR override: recalculate TP from entry/SL distance
            if rr_override is not None:
                risk = abs(sig["entry"] - sig["sl"])
                if risk > 0:
                    sig = dict(sig)
                    sig["tp"] = (sig["entry"] + risk * rr_override
                                 if sig["direction"] == "LONG"
                                 else sig["entry"] - risk * rr_override)

            pair_cd[prefix]   = ts
            zone_seen[dk]     = ts

            trade = {
                **sig,
                "pair":        pair,
                "date":        datetime.fromtimestamp(ts / 1000, tz=None).strftime("%Y-%m-%d"),
                "1h_idx":      i,
                "open_1h_idx": i,
                "result":      "timeout",   # overwritten by _check_close
                "pnl":         0.0,
                "hold_h":      0,
                "exit":        0.0,
            }
            open_trades[(prefix, direction)] = trade

    # ── Flush remaining open trades as timeout ────────────────────────
    last_c = all_1h[-1]
    for ot in open_trades.values():
        entry, sl = ot["entry"], ot["sl"]
        direction  = ot["direction"]
        denom = abs(entry - sl) if abs(entry - sl) > 0 else 1
        unreal = ((last_c["close"] - entry) / denom if direction == "LONG"
                  else (entry - last_c["close"]) / denom)
        ot.update(result="timeout", pnl=unreal,
                  hold_h=len(all_1h) - 1 - ot["open_1h_idx"],
                  exit=last_c["close"])
        completed.append(ot)

    return completed


# ── 10. Report aggregation ────────────────────────────────────────────────────

def aggregate(trades: list[dict]) -> dict:
    """Per (pair, strategy) stats."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[f"{t['pair']}::{t['strategy']}"].append(t)

    report = {}
    for key, ts in groups.items():
        pair, strat  = key.split("::", 1)
        wins         = [t for t in ts if t["result"] == "win"]
        losses       = [t for t in ts if t["result"] == "loss"]
        timeouts     = [t for t in ts if t["result"] == "timeout"]
        total        = len(ts)
        wr           = len(wins) / total * 100 if total else 0
        avg_hold     = sum(t["hold_h"] for t in ts) / total if total else 0

        # Max drawdown: peak-to-trough in cumulative W/L units
        running = peak = max_dd = 0.0
        for t in ts:
            running += 1.0 if t["result"] == "win" else -1.0
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        report[key] = {
            "pair":        pair,
            "strategy":    strat,
            "signals":     total,
            "wins":        len(wins),
            "losses":      len(losses),
            "timeouts":    len(timeouts),
            "winrate_pct": round(wr, 1),
            "avg_hold_h":  round(avg_hold, 1),
            "max_dd_trades": round(max_dd, 1),
        }
    return report


def _summary_by_strategy(report: dict) -> dict:
    by_s: dict[str, dict] = defaultdict(lambda: {"signals": 0, "wins": 0, "losses": 0, "pairs": set()})
    for r in report.values():
        s = r["strategy"]
        by_s[s]["signals"] += r["signals"]
        by_s[s]["wins"]    += r["wins"]
        by_s[s]["losses"]  += r["losses"]
        by_s[s]["pairs"].add(r["pair"])
    return {
        s: {
            "signals":     d["signals"],
            "wins":        d["wins"],
            "losses":      d["losses"],
            "winrate_pct": round(d["wins"] / d["signals"] * 100, 1) if d["signals"] else 0,
            "pairs":       sorted(d["pairs"]),
        }
        for s, d in sorted(by_s.items())
    }


# ── 11. CLI / main ────────────────────────────────────────────────────────────

def _print_table(report: dict):
    print(f"\n{'─' * 75}")
    print(f"{'Strategy':<16} {'Pair':<10} {'N':>5} {'W':>4} {'L':>4} "
          f"{'WR%':>6} {'AvgH':>6} {'MaxDD':>6}")
    print("─" * 75)
    for r in sorted(report.values(), key=lambda x: (x["strategy"], x["pair"])):
        print(f"{r['strategy']:<16} {r['pair']:<10} {r['signals']:>5} "
              f"{r['wins']:>4} {r['losses']:>4} {r['winrate_pct']:>6.1f}% "
              f"{r['avg_hold_h']:>5.0f}h {r['max_dd_trades']:>6.1f}")


def _print_summary(summary: dict):
    print(f"\n{'═' * 60}")
    print("SUMMARY ACROSS ALL PAIRS")
    print(f"{'═' * 60}")
    print(f"{'Strategy':<16} {'N':>6} {'W':>5} {'L':>5} {'WR%':>7}")
    print("─" * 45)
    for s, d in summary.items():
        print(f"{s:<16} {d['signals']:>6} {d['wins']:>5} {d['losses']:>5} "
              f"{d['winrate_pct']:>6.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Vortex Backtester — real strategy code")
    parser.add_argument("--pairs",    nargs="+", default=DEFAULT_PAIRS)
    parser.add_argument("--step",     type=int,  default=4,
                        help="Walk step in 1h candles (4=every 4h, 8=every 8h)")
    parser.add_argument("--out",      default=str(REPORT_DIR / "backtest_realstrat.json"))
    parser.add_argument("--rr",       type=float, default=None,
                        help="Override TP: tp = entry ± risk * RR  (e.g. 2.0, 3.0)")
    parser.add_argument("--tf-zone",  default=None,
                        help="Override zone TF across all strategies (e.g. 4h, 1h)")
    parser.add_argument("--s3-score", type=float, default=7.5)
    parser.add_argument("--s4-score", type=float, default=8.0)
    parser.add_argument("--s5-score", type=float, default=8.0)
    parser.add_argument("--s6-score", type=float, default=8.0)
    args = parser.parse_args()

    print("=== Vortex Backtester — Real Strategy Code ===")
    print(f"Pairs: {args.pairs}  step: {args.step}h  rr: {args.rr}  tf-zone: {args.tf_zone}")
    load_all(args.pairs)
    _patch_all()

    if args.tf_zone:
        set_tf_zone(args.tf_zone)

    min_scores = {
        "s3": args.s3_score, "s4": args.s4_score,
        "s5": args.s5_score, "s6": args.s6_score,
    }

    all_trades: list[dict] = []

    for pair in args.pairs:
        print(f"\n─── {pair} ───")
        trades = run_pair(pair, walk_step_h=args.step,
                          rr_override=args.rr, min_scores=min_scores)
        all_trades.extend(trades)
        pair_rpt = {k: v for k, v in aggregate(trades).items()}
        _print_table(pair_rpt)

    report    = aggregate(all_trades)
    summary   = _summary_by_strategy(report)

    _print_summary(summary)

    out = {
        "generated_at":    datetime.now().isoformat(),
        "pairs":           args.pairs,
        "walk_step_h":     args.step,
        "max_hold_h":      MAX_HOLD_H,
        "total_trades":    len(all_trades),
        "notes": {
            "tf_resolution":   "15m and 30m CSVs loaded; 5m still proxied to 15m. "
                               "S1-S6 now primarily use 1H/4H data.",
            "missing_strats":  "Some S1-S6 strategies rarely fire at step>=12h because "
                               "they depend on active candle state. Run --step 4 for "
                               "more representative coverage.",
            "simulation_tp":   "TP = tp2 from active strategy code.",
            "5m_proxy":        "5m TF requests → 15m CSV (closest available).",
        },
        "by_pair_strategy": {k: v for k, v in report.items()},
        "by_strategy":     summary,
        "trades":          all_trades,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\nSaved → {args.out}  ({len(all_trades)} trades)")


if __name__ == "__main__":
    main()
