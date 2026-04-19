#!/usr/bin/env python3
"""
daily_analysis.py — Vortex Daily Performance Analysis
Phase 1: Auto-improvement loop

Run: python3 daily_analysis.py
Crontab: 0 0 * * * cd /home/prospera/vortex && python3 daily_analysis.py >> /tmp/daily_analysis.log 2>&1
"""

import json
import logging
import os
import sys
import re
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict
from functools import lru_cache

# ── Setup ──────────────────────────────────────────────────────────────────

VORTEX_DIR = Path("/home/prospera/vortex")
TRADES_FILE = VORTEX_DIR / "trades.json"
REPORTS_DIR = VORTEX_DIR / "analysis_reports"
LOG_FILE    = Path("/tmp/daily_analysis.log")

REPORTS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("daily_analysis")


# ── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """Kirim pesan ke Telegram (baca token dari config.py di VORTEX_DIR)."""
    sys.path.insert(0, str(VORTEX_DIR))
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
        }
        import requests
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram sent successfully")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


# ── Data Loading ────────────────────────────────────────────────────────────

def load_trades(path: Path) -> list[dict]:
    """Load trades dari trades.json."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("trades", [])
        return data
    except FileNotFoundError:
        log.error(f"trades.json not found at {path}")
        return []
    except json.JSONDecodeError as e:
        log.error(f"trades.json corrupted: {e}")
        return []


# ── Metrics ─────────────────────────────────────────────────────────────────

def winrate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    return wins / len(trades) * 100


def avg_rr(trades: list[dict]) -> float:
    rrs = [t["rr"] for t in trades if t.get("rr") is not None]
    return sum(rrs) / len(rrs) if rrs else 0.0


def max_drawdown(trades: list[dict]) -> tuple[float, str, str]:
    """
    Hitung max drawdown sederhana (peak-to-trough equity).
    Returns (dd_pct, peak_date, trough_date).
    """
    if not trades:
        return 0.0, "", ""

    equity_curve = []
    equity = 1.0  # start normalized
    for t in trades:
        if t.get("result") == "WIN":
            rr = t.get("rr", 1.0)
            risk_pct = t.get("risk_pct", 1.0) / 100
            equity *= (1 + risk_pct * rr)
        else:
            risk_pct = t.get("risk_pct", 1.0) / 100
            equity *= (1 - risk_pct)
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_dd = 0.0
    peak_date = trades[0].get("time", "")
    peak_idx = 0
    trough_date = ""

    for i, eq in enumerate(equity_curve):
        if eq > peak:
            peak = eq
            peak_date = trades[i].get("time", "")
            peak_idx = i
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd
            trough_date = trades[i].get("time", "")

    return round(max_dd, 2), peak_date[:10] if peak_date else "", trough_date[:10] if trough_date else ""


def false_signal_rate(trades: list[dict]) -> float:
    """
    False signal: trade yang loss dalam <5 candles (entry→SL hit cepat).
    Indicates wick/rejection false breakout.
    """
    if not trades:
        return 0.0
    false_count = sum(
        1 for t in trades
        if t.get("result") == "LOSS"
        and isinstance(t.get("candles_to_resolve"), int)
        and t["candles_to_resolve"] <= 5
    )
    return false_count / len(trades) * 100


# ── Strategy Breakdown ───────────────────────────────────────────────────────

def analyze_by_strategy(trades: list[dict]) -> dict:
    by_strat = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0, "rrs": [], "pairs": set(), "false_count": 0})
    for t in trades:
        s = t.get("strategy", t.get("strategy_id", "UNKNOWN"))
        by_strat[s]["total"] += 1
        by_strat[s]["pairs"].add(t.get("symbol", t.get("pair", "?")))
        if t.get("result") == "WIN":
            by_strat[s]["wins"] += 1
        else:
            by_strat[s]["losses"] += 1
        if t.get("rr") is not None:
            by_strat[s]["rrs"].append(t["rr"])
        if (t.get("result") == "LOSS"
                and isinstance(t.get("candles_to_resolve"), int)
                and t["candles_to_resolve"] <= 5):
            by_strat[s]["false_count"] += 1

    result = {}
    for s, d in by_strat.items():
        wr = d["wins"] / d["total"] * 100 if d["total"] > 0 else 0.0
        avg_rr = sum(d["rrs"]) / len(d["rrs"]) if d["rrs"] else 0.0
        fsr = d["false_count"] / d["total"] * 100 if d["total"] > 0 else 0.0
        result[s] = {
            "total":      d["total"],
            "winrate":    round(wr, 1),
            "avg_rr":     round(avg_rr, 2),
            "false_rate": round(fsr, 1),
            "pairs":      sorted(d["pairs"]),
        }
    return result


# ── Pair Breakdown ──────────────────────────────────────────────────────────

def analyze_by_pair(trades: list[dict]) -> dict:
    by_pair = defaultdict(lambda: {"wins": 0, "losses": 0, "total": 0, "rrs": []})
    for t in trades:
        p = t.get("symbol", t.get("pair", "?"))
        by_pair[p]["total"] += 1
        if t.get("result") == "WIN":
            by_pair[p]["wins"] += 1
        else:
            by_pair[p]["losses"] += 1
        if t.get("rr") is not None:
            by_pair[p]["rrs"].append(t["rr"])

    result = {}
    for p, d in by_pair.items():
        wr = d["wins"] / d["total"] * 100 if d["total"] > 0 else 0.0
        result[p] = {
            "total":   d["total"],
            "winrate": round(wr, 1),
            "avg_rr":  round(sum(d["rrs"]) / len(d["rrs"]), 2) if d["rrs"] else 0.0,
        }
    return result


# ── Insights & Recommendations ──────────────────────────────────────────────

def generate_insights(trades: list[dict], strat_stats: dict, pair_stats: dict,
                      overall_wr: float, avg_rr_val: float,
                      fsr: float, dd: float, dd_peak: str, dd_trough: str) -> list[str]:
    insights = []

    # 1. Winrate assessment
    if overall_wr < 40:
        insights.append(
            f"⚠️ WINRATE {overall_wr:.1f}% < 40% threshold — experiment gagal, "
            f"consider revert TF to 4H per experiment rules."
        )
    elif overall_wr < 48:
        insights.append(
            f"⚠️ WINRATE {overall_wr:.1f}% di bawah target 48%. "
            f"Perlu monitor — apakah temporary atau pattern?"
        )
    else:
        insights.append(f"✅ WINRATE {overall_wr:.1f}% on track (target ≥48%).")

    # 2. False signal rate
    if fsr > 20:
        insights.append(
            f"⚠️ FALSE SIGNAL RATE {fsr:.1f}% (>20%). "
            f"Kemungkinan wick rejection gate terlalu lemah atau TF terlalu rendah."
        )
    elif fsr > 10:
        insights.append(
            f"⚡ FALSE SIGNAL {fsr:.1f}% — monitor closely, tidak urgent."
        )
    else:
        insights.append(f"✅ FALSE SIGNAL RATE {fsr:.1f}% acceptable (<10%).")

    # 3. Drawdown
    if dd > 15:
        insights.append(
            f"📉 MAX DRAWDOWN {dd:.1f}% (peak {dd_peak} → trough {dd_trough}). "
            f"Pertimbangkan reduce risk_pct jika DD konsisten >15%."
        )
    elif dd > 8:
        insights.append(f"📊 MAX DRAWDOWN {dd:.1f}% — acceptable, monitor.")
    else:
        insights.append(f"✅ MAX DRAWDOWN {dd:.1f}% — healthy.")

    return insights


def generate_recommendations(trades: list[dict], strat_stats: dict,
                             overall_wr: float, fsr: float,
                             dd: float, avg_rr_val: float) -> list[str]:
    """
    Max 3 actionable recommendations. Only output if data >= 20 trades.
    """
    recs = []

    if len(trades) < 20:
        return ["📋 Sample size < 20 trades — insufficient data for recommendations."]

    # 1. Worst strategy
    worst_strat = None
    for s, d in strat_stats.items():
        if d["total"] >= 3 and d["winrate"] < 35:
            if worst_strat is None or d["winrate"] < strat_stats.get(worst_strat, {}).get("winrate", 99):
                worst_strat = s

    if worst_strat:
        recs.append(
            f"[ACTION-1] Disable or refine {worst_strat} (winrate {strat_stats[worst_strat]['winrate']}%, "
            f"{strat_stats[worst_strat]['total']} trades). "
            f"Consider tightening displacement threshold or adding wick confirmation."
        )

    # 2. High false signal rate
    if fsr > 15:
        recs.append(
            f"[ACTION-2] False signal rate {fsr:.1f}% > 15%. "
            f"Review S4/S6 wick rejection gates — add extra displacement filter "
            f"or widen entry zone tolerance."
        )

    # 3. Drawdown / risk management
    if dd > 15 or (dd > 10 and overall_wr < 48):
        recs.append(
            f"[ACTION-3] High drawdown {dd:.1f}% + winrate {overall_wr:.1f}%. "
            f"Consider reduce risk_pct by 0.25% (e.g., 1.0% → 0.75%) for next 2 weeks."
        )

    # 4. R:R quality
    if avg_rr_val > 0 and avg_rr_val < 1.5:
        recs.append(
            f"[INFO] Average RR {avg_rr_val:.2f} < 1.5 — "
            f"ensure MIN_RR_RATIO gate is enforced at {str(2.0)}+."
        )

    return recs[:3]  # cap at 3


# ── Report Generation ───────────────────────────────────────────────────────

def generate_markdown(date_str: str, trades: list[dict],
                      overall_wr: float, avg_rr_val: float,
                      fsr: float, dd: float,
                      dd_peak: str, dd_trough: str,
                      strat_stats: dict, pair_stats: dict,
                      insights: list[str], recs: list[str]) -> str:

    today = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")

    md = f"""# Vortex Daily Analysis Report
**Generated:** {today}
**Mode:** `TF_EXPERIMENT_MODE = 1H_AGGRESSIVE` (if active)

---

## 📊 Overall Performance

| Metric | Value |
|--------|-------|
| Total Trades | {len(trades)} |
| Winrate | {overall_wr:.1f}% |
| Avg RR | {avg_rr_val:.2f} |
| False Signal Rate | {fsr:.1f}% |
| Max Drawdown | {dd:.1f}% ({dd_peak} → {dd_trough}) |
| Breakeven WR (RR≥2) | ~33% |

---

## 🧠 Key Insights

{"".join(f"{i+1}. {line}\n" for i, line in enumerate(insights))}

---

## 🎯 Per-Strategy Breakdown

| Strategy | Trades | Winrate | Avg RR | False Rate |
|----------|--------|---------|--------|------------|
{"".join(f"| {s} | {d['total']} | {d['winrate']}% | {d['avg_rr']} | {d['false_rate']}% |\n" for s, d in sorted(strat_stats.items()))}

---

## 📈 Per-Pair Breakdown

| Pair | Trades | Winrate | Avg RR |
|------|--------|---------|--------|
{"".join(f"| {p} | {d['total']} | {d['winrate']}% | {d['avg_rr']} |\n" for p, d in sorted(pair_stats.items()))}

---

## 🔧 Recommended Actions (Max 3)

{"".join(f"{i+1}. {line}\n" for i, line in enumerate(recs))}

---

## 📋 Recent Trades (Last 10)

| Time | Pair | Strategy | Direction | Entry | SL | TP | Result | RR |
|------|------|----------|-----------|-------|----|----|--------|----|
{"".join(f"| {t.get('time','')} | {t.get('symbol', t.get('pair','?'))} | {t.get('strategy','?')} | {t.get('direction','?')} | {t.get('entry','?')} | {t.get('sl','?')} | {t.get('tp','?')} | {t.get('result','?')} | {t.get('rr','?')} |\n" for t in trades[-10:])}

---

*Report auto-generated by Vortex Daily Analysis Engine*
"""
    return md


# ── Telegram Summary ────────────────────────────────────────────────────────

def telegram_summary(date_str: str, total: int, overall_wr: float,
                     avg_rr_val: float, fsr: float, dd: float,
                     strat_stats: dict, recs: list[str]) -> str:

    worst = ""
    best = ""
    if strat_stats:
        sorted_strats = sorted(strat_stats.items(), key=lambda x: x[1]["winrate"])
        worst = f" Worst: {sorted_strats[0][0]} ({sorted_strats[0][1]['winrate']}%)"
        best = f" Best: {sorted_strats[-1][0]} ({sorted_strats[-1][1]['winrate']}%)"

    msg = (
        f"📊 <b>VORTEX DAILY — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Trades   : {total}\n"
        f"Winrate  : {overall_wr:.1f}%\n"
        f"Avg RR   : {avg_rr_val:.2f}\n"
        f"False Sig: {fsr:.1f}%\n"
        f"Max DD   : {dd:.1f}%\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{worst}{best}\n"
    )

    if recs:
        for r in recs:
            recs_line = re.sub(r'\[ACTION-\d+\]', '▸', r)
            msg += f"\n{recs_line}"

    msg += f"\n━━━━━━━━━━━━━━━\n<i>Full report: analysis_reports/{date_str}_analysis.md</i>"
    return msg


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    today = date.today()
    date_str = today.isoformat()
    report_path = REPORTS_DIR / f"{date_str}_analysis.md"

    log.info(f"=== Daily Analysis Started: {date_str} ===")

    trades = load_trades(TRADES_FILE)
    if not trades:
        msg = (
            f"📊 <b>VORTEX DAILY — {date_str}</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"❌ No trades found in trades.json\n"
            f"Scanner may not be running."
        )
        send_telegram(msg)
        log.warning("No trades found.")
        return

    # ── Core metrics ──
    overall_wr  = winrate(trades)
    avg_rr_val  = avg_rr(trades)
    fsr         = false_signal_rate(trades)
    dd, dd_p, dd_t = max_drawdown(trades)
    strat_stats = analyze_by_strategy(trades)
    pair_stats  = analyze_by_pair(trades)

    log.info(f"Trades: {len(trades)} | WR: {overall_wr:.1f}% | Avg RR: {avg_rr_val:.2f} | "
             f"FSR: {fsr:.1f}% | Max DD: {dd:.1f}%")

    # ── Generate insights + recs ──
    insights = generate_insights(trades, strat_stats, pair_stats,
                                  overall_wr, avg_rr_val, fsr, dd, dd_p, dd_t)
    recs = generate_recommendations(trades, strat_stats,
                                    overall_wr, fsr, dd, avg_rr_val)

    for i, ins in enumerate(insights, 1):
        log.info(f"Insight {i}: {ins}")
    for i, rec in enumerate(recs, 1):
        log.info(f"Rec {i}: {rec}")

    # ── Save report ──
    report_md = generate_markdown(
        date_str, trades, overall_wr, avg_rr_val,
        fsr, dd, dd_p, dd_t,
        strat_stats, pair_stats,
        insights, recs
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    log.info(f"Report saved: {report_path}")

    # ── Telegram ──
    summary = telegram_summary(date_str, len(trades), overall_wr,
                               avg_rr_val, fsr, dd, strat_stats, recs)
    send_telegram(summary)

    log.info(f"=== Daily Analysis Complete: {date_str} ===")
    return {
        "date":        date_str,
        "total":       len(trades),
        "winrate":     overall_wr,
        "avg_rr":      avg_rr_val,
        "fsr":         fsr,
        "max_dd":      dd,
        "recs":        recs,
        "report_path": str(report_path),
    }


if __name__ == "__main__":
    run()
