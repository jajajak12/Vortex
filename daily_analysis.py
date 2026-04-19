#!/usr/bin/env python3
"""
daily_analysis.py — Vortex Auto Improvement Engine
Phase 2: LLM-powered daily analysis with MiniMax API

Run: python3 daily_analysis.py
Crontab: 0 0 * * * cd /home/prospera/vortex && python3 daily_analysis.py >> /tmp/daily_analysis.log 2>&1
"""

import json
import logging
import os
import sys
import re
import requests
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict

# ── Setup ──────────────────────────────────────────────────────────────────

VORTEX_DIR   = Path("/home/prospera/vortex")
TRADES_FILE  = VORTEX_DIR / "trades.json"
REPORTS_DIR  = VORTEX_DIR / "analysis_reports"
LESSONS_FILE = VORTEX_DIR / "lessons.json"
LOG_FILE     = Path("/tmp/daily_analysis.log")

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


# ── Config ─────────────────────────────────────────────────────────────────

sys.path.insert(0, str(VORTEX_DIR))
try:
    from config import (
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
        MINIMAX_API_KEY, MINIMAX_GROUP_ID,
        TF_EXPERIMENT_MODE,
    )
except ImportError:
    TELEGRAM_BOT_TOKEN = TELEGRAM_CHAT_ID = ""
    MINIMAX_API_KEY    = os.environ.get("MINIMAX_API_KEY", "")
    MINIMAX_GROUP_ID   = os.environ.get("MINIMAX_GROUP_ID", "")
    TF_EXPERIMENT_MODE = "UNKNOWN"


# ── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram sent")
    except Exception as e:
        log.warning(f"Telegram failed: {e}")


# ── Data Loading ────────────────────────────────────────────────────────────

def load_trades(path: Path) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data.get("trades", [])
        return data
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error(f"trades.json error: {e}")
        return []


def load_lessons(path: Path) -> list[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_lessons(path: Path, lessons: list[dict]):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(lessons, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Failed to save lessons: {e}")


# ── Metrics ────────────────────────────────────────────────────────────────

def winrate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("result") == "WIN")
    return wins / len(trades) * 100


def avg_rr(trades: list[dict]) -> float:
    rrs = [t["rr"] for t in trades if t.get("rr") is not None]
    return sum(rrs) / len(rrs) if rrs else 0.0


def max_drawdown(trades: list[dict]) -> tuple[float, str, str]:
    if not trades:
        return 0.0, "", ""
    equity_curve = []
    equity = 1.0
    for t in trades:
        risk_pct = (t.get("risk_pct") or 1.0) / 100
        if t.get("result") == "WIN":
            equity *= (1 + risk_pct * (t.get("rr") or 1.0))
        else:
            equity *= (1 - risk_pct)
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_dd = 0.0
    peak_date = trades[0].get("time", "")[:10]
    trough_date = ""
    for i, eq in enumerate(equity_curve):
        if eq > peak:
            peak = eq
            peak_date = trades[i].get("time", "")[:10]
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd
            trough_date = trades[i].get("time", "")[:10]
    return round(max_dd, 2), peak_date, trough_date


def false_signal_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    false_count = sum(
        1 for t in trades
        if t.get("result") == "LOSS"
        and isinstance(t.get("candles_to_resolve"), int)
        and t["candles_to_resolve"] <= 5
    )
    return round(false_count / len(trades) * 100, 1)


# ── Strategy / Pair Breakdown ───────────────────────────────────────────────

def analyze_by_strategy(trades: list[dict]) -> dict:
    by_strat = defaultdict(lambda: {
        "wins": 0, "losses": 0, "total": 0,
        "rrs": [], "pairs": set(), "false_count": 0,
        "losses_detail": [],
    })
    for t in trades:
        s = t.get("strategy", t.get("strategy_id", "UNKNOWN"))
        by_strat[s]["total"] += 1
        by_strat[s]["pairs"].add(t.get("symbol", t.get("pair", "?")))
        if t.get("result") == "WIN":
            by_strat[s]["wins"] += 1
        else:
            by_strat[s]["losses"] += 1
            by_strat[s]["losses_detail"].append({
                "pair": t.get("symbol", t.get("pair", "?")),
                "direction": t.get("direction", "?"),
                "rr": t.get("rr"),
                "candles": t.get("candles_to_resolve"),
            })
        if t.get("rr") is not None:
            by_strat[s]["rrs"].append(t["rr"])
        if (t.get("result") == "LOSS"
                and isinstance(t.get("candles_to_resolve"), int)
                and t["candles_to_resolve"] <= 5):
            by_strat[s]["false_count"] += 1

    result = {}
    for s, d in by_strat.items():
        wr  = d["wins"] / d["total"] * 100 if d["total"] > 0 else 0.0
        avg_rr_v = sum(d["rrs"]) / len(d["rrs"]) if d["rrs"] else 0.0
        fsr = d["false_count"] / d["total"] * 100 if d["total"] > 0 else 0.0
        result[s] = {
            "total":       d["total"],
            "wins":        d["wins"],
            "losses":      d["losses"],
            "winrate":     round(wr, 1),
            "avg_rr":      round(avg_rr_v, 2),
            "false_rate":  round(fsr, 1),
            "pairs":       sorted(d["pairs"]),
            "losses_detail": d["losses_detail"],
        }
    return result


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


# ── Analysis Summary (text) ─────────────────────────────────────────────────

def build_analysis_text(trades: list[dict], strat_stats: dict,
                       pair_stats: dict, overall_wr: float,
                       avg_rr_v: float, fsr: float,
                       dd: float, dd_p: str, dd_t: str) -> str:

    lines = [
        f"## VORTEX TRADING PERFORMANCE SUMMARY (as of {date.today().isoformat()})",
        f"",
        f"### Overall Metrics",
        f"- Total Trades: {len(trades)}",
        f"- Winrate: {overall_wr:.1f}%",
        f"- Average RR: {avg_rr_v:.2f}",
        f"- False Signal Rate: {fsr:.1}% (losses within 5 candles of entry)",
        f"- Max Drawdown: {dd:.1f}% ({dd_p} to {dd_t})",
        f"- TF Experiment Mode: {TF_EXPERIMENT_MODE}",
        f"- Breakeven Winrate (RR≥2): ~33%",
        f"",
        f"### Per-Strategy Breakdown",
    ]
    for s, d in sorted(strat_stats.items()):
        lines.append(
            f"- {s}: {d['total']} trades | {d['winrate']}% WR | "
            f"avg RR={d['avg_rr']} | false_rate={d['false_rate']}%"
        )
        if d["losses_detail"]:
            lines.append(f"  Recent losses: {d['losses_detail'][-3:]}")
    lines.append("")
    lines.append(f"### Per-Pair Breakdown")
    for p, d in sorted(pair_stats.items()):
        lines.append(f"- {p}: {d['total']} trades | {d['winrate']}% WR | avg RR={d['avg_rr']}")
    lines.append("")
    lines.append(f"### Recent Trades (last 10)")
    for t in trades[-10:]:
        lines.append(
            f"- {t.get('time','')} | {t.get('symbol',t.get('pair','?'))} | "
            f"{t.get('direction','?')} | {t.get('strategy','?')} | "
            f"Entry={t.get('entry')} SL={t.get('sl')} TP={t.get('tp')} | "
            f"{t.get('result')} | RR={t.get('rr')} | candles_to_SL={t.get('candles_to_resolve')}"
        )
    return "\n".join(lines)


# ── MiniMax LLM ─────────────────────────────────────────────────────────────

MINIMAX_URL = "https://api.minimax.chat/v1/text/chatcompletion_v2"
MINIMAX_MODEL = "MiniMax-Text-01"


def generate_llm_improvement(analysis_text: str, total_trades: int) -> dict:
    """
    Kirim analysis ke MiniMax API, minta improvement recommendations.

    Returns dict with keys:
      - status: "success" | "error" | "insufficient_data"
      - insights: list[str]
      - recommendations: list[str]
      - lessons: list[dict]
      - raw_response: str
    """
    if not MINIMAX_API_KEY:
        return {
            "status":  "error",
            "insights": [],
            "recommendations": [],
            "lessons": [],
            "raw_response": "MINIMAX_API_KEY not configured",
        }

    is_observasi = total_trades < 30
    mode_prompt = (
        "OBSERVASI MODE (data < 30 trades): "
        "Only provide qualitative observations and patterns you notice. "
        "Do NOT recommend any rule changes. "
        "Focus on: what patterns are emerging, which setups look promising, "
        "what data points are too early to conclude."
        if is_observasi else
        "IMPROVEMENT MODE (data >= 30 trades): "
        "Provide max 3 specific, data-driven improvement recommendations. "
        "For each: cite the specific data that supports it. "
        "Also extract any LEARNABLE LESSONS (PREFER/AVOID patterns) "
        "that can be injected into future strategy decisions."
    )

    system_prompt = (
        "You are Vortex Senior Quant Engineer. "
        "You analyze crypto trading agent performance data. "
        "You are data-driven, cautious, and precise. "
        "You NEVER over-optimize. You focus on stability and risk management. "
        "Your output must be in valid JSON with keys: "
        "insights (list), recommendations (list, max 3), lessons (list of {type, description, data_evidence})."
    )

    user_prompt = (
        f"{mode_prompt}\n\n"
        f"MODE: {'OBSERVASI' if is_observasi else 'IMPROVEMENT'}\n\n"
        f"ANALYSIS DATA:\n{analysis_text}\n\n"
        f"Respond ONLY with valid JSON in this exact format:\n"
        f'{{"insights": ["..."], "recommendations": ["..."], "lessons": [{{"type": "PREFER|AVOID|DIRECTIONAL", "description": "...", "data_evidence": "..."}}]}}'
    )

    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model":       MINIMAX_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }
    if MINIMAX_GROUP_ID:
        payload["group_id"] = MINIMAX_GROUP_ID

    try:
        r = requests.post(MINIMAX_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        resp_json = r.json()
        raw = resp_json.get("choices", [{}])[0].get("messages", [{}])[0].get("text", "")

        # Try parse JSON from response
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # Try extract JSON block
            m = re.search(r'\{[\s\S]*\}', raw)
            if m:
                parsed = json.loads(m.group())
            else:
                return {
                    "status":  "error",
                    "insights": [f"Failed to parse LLM response: {raw[:200]}"],
                    "recommendations": [],
                    "lessons": [],
                    "raw_response": raw[:1000],
                }

        return {
            "status":         "success",
            "insights":        parsed.get("insights", []),
            "recommendations": parsed.get("recommendations", [])[:3],
            "lessons":         parsed.get("lessons", []),
            "raw_response":    raw[:2000],
        }

    except requests.exceptions.Timeout:
        return {
            "status":  "error",
            "insights": ["LLM API timeout after 30s"],
            "recommendations": [],
            "lessons": [],
            "raw_response": "timeout",
        }
    except requests.exceptions.RequestException as e:
        return {
            "status":  "error",
            "insights": [f"LLM API error: {e}"],
            "recommendations": [],
            "lessons": [],
            "raw_response": str(e),
        }
    except Exception as e:
        return {
            "status":  "error",
            "insights": [f"Unexpected error: {e}"],
            "recommendations": [],
            "lessons": [],
            "raw_response": str(e),
        }


# ── Lessons Management ─────────────────────────────────────────────────────

def merge_lessons(existing: list[dict], new_lessons: list[dict]) -> list[dict]:
    """
    Append new lessons to existing. Deduplicate by (type, description).
    Keep max 50 lessons (FIFO).
    """
    if not new_lessons:
        return existing[-50:]

    seen = {(l.get("type"), l.get("description")) for l in existing}
    merged = list(existing)
    for lesson in new_lessons:
        key = (lesson.get("type"), lesson.get("description"))
        if key not in seen and lesson.get("description"):
            merged.append({
                "type":         lesson.get("type", "UNKNOWN"),
                "description":  lesson.get("description", ""),
                "data_evidence": lesson.get("data_evidence", ""),
                "added_date":    date.today().isoformat(),
            })
            seen.add(key)

    return merged[-50:]  # keep last 50


# ── Rule-Based Insights ────────────────────────────────────────────────────

def rule_insights(trades: list[dict], strat_stats: dict,
                  overall_wr: float, avg_rr_v: float,
                  fsr: float, dd: float) -> list[str]:

    insights = []

    if overall_wr < 40:
        insights.append(
            f"⚠️ WINRATE {overall_wr:.1f}% < 40% — below experiment threshold. "
            f"Review if TF 1H is too noisy or if displacement gates need tightening."
        )
    elif overall_wr < 48:
        insights.append(
            f"⚡ WINRATE {overall_wr:.1f}% — below 48% target. "
            f"Monitor closely — temporary variance or structural issue?"
        )
    else:
        insights.append(f"✅ WINRATE {overall_wr:.1f}% — on track (≥48% target).")

    if fsr > 20:
        insights.append(
            f"🚨 FALSE SIGNAL RATE {fsr:.1f}% > 20% — wick rejection gates "
            f"likely too loose or TF noise too high."
        )
    elif fsr > 10:
        insights.append(f"⚡ FALSE SIGNAL {fsr:.1f}% — monitor, not urgent yet.")
    else:
        insights.append(f"✅ FALSE SIGNAL RATE {fsr:.1f}% — healthy (<10%).")

    if dd > 15:
        insights.append(
            f"📉 MAX DRAWDOWN {dd:.1f}% > 15% — "
            f"consider reducing risk_pct by 0.25%."
        )
    elif dd > 8:
        insights.append(f"📊 MAX DRAWDOWN {dd:.1f}% — acceptable.")
    else:
        insights.append(f"✅ MAX DRAWDOWN {dd:.1f}% — healthy.")

    if avg_rr_v > 0 and avg_rr_v < 1.5:
        insights.append(
            f"⚠️ AVG RR {avg_rr_v:.2f} < 1.5 — check MIN_RR_RATIO gate "
            f"is enforced at 2.0+."
        )

    # Per-strategy flags
    for s, d in strat_stats.items():
        if d["total"] >= 3 and d["winrate"] < 35:
            insights.append(
                f"⚠️ {s} winrate {d['winrate']}% ({d['total']} trades) — "
                f"worst performing strategy. Needs review."
            )
        if d["false_rate"] > 25:
            insights.append(
                f"🚨 {s} false_rate {d['false_rate']}% > 25% — "
                f"wick gate may be broken for this strategy."
            )

    return insights


# ── Report Generation ───────────────────────────────────────────────────────

def generate_full_report(date_str: str, trades: list[dict],
                       overall_wr: float, avg_rr_v: float,
                       fsr: float, dd: float, dd_p: str, dd_t: str,
                       strat_stats: dict, pair_stats: dict,
                       rule_ins: list[str],
                       llm_result: dict) -> str:

    llm_ins     = llm_result.get("insights", [])
    llm_recs    = llm_result.get("recommendations", [])
    llm_lessons = llm_result.get("lessons", [])

    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

    report = f"""# Vortex Full Analysis Report
**Date:** {date_str}
**Generated:** {now}
**TF Mode:** {TF_EXPERIMENT_MODE}
**LLM Status:** {llm_result.get('status', 'unknown')}

---

## 📊 Overall Performance

| Metric | Value |
|--------|-------|
| Total Trades | {len(trades)} |
| Winrate | {overall_wr:.1f}% |
| Avg RR | {avg_rr_v:.2f} |
| False Signal Rate | {fsr:.1f}% |
| Max Drawdown | {dd:.1f}% ({dd_p} → {dd_t}) |
| Breakeven WR (RR≥2) | ~33% |
| Experiment Threshold | 40% |

---

## 🧠 Rule-Based Insights

{"".join(f"{i+1}. {line}\n" for i, line in enumerate(rule_ins))}

---

## 🤖 LLM Insights (MiniMax)

{"".join(f"{i+1}. {line}\n" for i, line in enumerate(llm_ins))}

---

## 🔧 LLM Recommendations (Max 3)

{"".join(f"{i+1}. {line}\n" for i, line in enumerate(llm_recs))}

---

## 📚 Learned Lessons

{"".join(f"- [{l.get('type','?')}] {l.get('description','')} (evidence: {l.get('data_evidence','')})\n" for l in llm_lessons)}

---

## 🎯 Per-Strategy Breakdown

| Strategy | Trades | Win | Loss | Winrate | Avg RR | False Rate |
|----------|--------|-----|------|---------|--------|------------|
{"".join(f"| {s} | {d['total']} | {d['wins']} | {d['losses']} | {d['winrate']}% | {d['avg_rr']} | {d['false_rate']}% |\n" for s, d in sorted(strat_stats.items()))}

---

## 📈 Per-Pair Breakdown

| Pair | Trades | Winrate | Avg RR |
|------|--------|---------|--------|
{"".join(f"| {p} | {d['total']} | {d['winrate']}% | {d['avg_rr']} |\n" for p, d in sorted(pair_stats.items()))}

---

## 📋 Recent Trades (Last 10)

| Time | Pair | Strat | Dir | Entry | SL | TP | Result | RR | Candles |
|------|------|-------|-----|-------|----|----|--------|----|---------|
{"".join(f"| {t.get('time','')} | {t.get('symbol',t.get('pair','?'))} | {t.get('strategy','?')} | {t.get('direction','?')} | {t.get('entry','?')} | {t.get('sl','?')} | {t.get('tp','?')} | {t.get('result','?')} | {t.get('rr','?')} | {t.get('candles_to_resolve','?')} |\n" for t in trades[-10:])}

---

## 📦 Raw LLM Response

```
{llm_result.get('raw_response', 'N/A')}
```

---

*Auto-generated by Vortex Auto Improvement Engine*
"""
    return report


def telegram_summary(date_str: str, total: int, overall_wr: float,
                    avg_rr_v: float, fsr: float, dd: float,
                    strat_stats: dict, llm_recs: list[str],
                    llm_status: str) -> str:

    worst = best = ""
    if strat_stats:
        sorted_s = sorted(strat_stats.items(), key=lambda x: x[1]["winrate"])
        if sorted_s:
            worst = f" Worst: {sorted_s[0][0]} {sorted_s[0][1]['winrate']}%"
            best  = f" Best: {sorted_s[-1][0]} {sorted_s[-1][1]['winrate']}%"

    status_icon = "✅" if llm_status == "success" else "⚠️"
    recs_block = ""
    if llm_recs:
        for r in llm_recs[:3]:
            clean = re.sub(r'\[ACTION-\d+\]', '▸', r)
            clean = re.sub(r'\*+', '', clean)
            recs_block += f"\n▸ {clean[:120]}"

    return (
        f"📊 VORTEX DAILY — {date_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Trades   : {total}\n"
        f"Winrate  : {overall_wr:.1f}%\n"
        f"Avg RR   : {avg_rr_v:.2f}\n"
        f"False Sig: {fsr:.1f}%\n"
        f"Max DD   : {dd:.1f}%\n"
        f"{worst}{best}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{status_icon} LLM: {llm_status}\n"
        f"{recs_block}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📁 analysis_reports/{date_str}_full_analysis.md"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    today = date.today()
    date_str = today.isoformat()
    report_path = REPORTS_DIR / f"{date_str}_full_analysis.md"

    log.info(f"=== Vortex Auto Improvement Engine: {date_str} ===")

    # 1. Load data
    trades = load_trades(TRADES_FILE)
    if not trades:
        send_telegram(
            f"📊 VORTEX DAILY — {date_str}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"❌ No trades found. Scanner may be down."
        )
        log.warning("No trades.")
        return

    # 2. Compute metrics
    overall_wr = winrate(trades)
    avg_rr_v   = avg_rr(trades)
    fsr        = false_signal_rate(trades)
    dd, dd_p, dd_t = max_drawdown(trades)
    strat_stats = analyze_by_strategy(trades)
    pair_stats  = analyze_by_pair(trades)

    log.info(
        f"Metrics: {len(trades)} trades | WR={overall_wr:.1f}% | "
        f"AvgRR={avg_rr_v:.2f} | FSR={fsr:.1f}% | DD={dd:.1f}%"
    )

    # 3. Rule-based insights
    rule_ins = rule_insights(trades, strat_stats, overall_wr, avg_rr_v, fsr, dd)

    # 4. Build analysis text
    analysis_text = build_analysis_text(
        trades, strat_stats, pair_stats,
        overall_wr, avg_rr_v, fsr, dd, dd_p, dd_t
    )

    # 5. LLM analysis
    log.info(f"Calling MiniMax API (mode={'OBSERVASI' if len(trades) < 30 else 'IMPROVEMENT'})...")
    llm_result = generate_llm_improvement(analysis_text, len(trades))
    log.info(f"LLM status: {llm_result['status']}")
    for ins in llm_result.get("insights", []):
        log.info(f"  LLM insight: {ins[:80]}")

    # 6. Save lessons
    if llm_result.get("lessons"):
        existing = load_lessons(LESSONS_FILE)
        merged   = merge_lessons(existing, llm_result["lessons"])
        save_lessons(LESSONS_FILE, merged)
        log.info(f"Lessons saved: {len(llm_result['lessons'])} new, {len(merged)} total")

    # 7. Generate full report
    report_md = generate_full_report(
        date_str, trades, overall_wr, avg_rr_v, fsr, dd, dd_p, dd_t,
        strat_stats, pair_stats, rule_ins, llm_result
    )
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    log.info(f"Report saved: {report_path}")

    # 8. Telegram
    summary = telegram_summary(
        date_str, len(trades), overall_wr, avg_rr_v, fsr, dd,
        strat_stats, llm_result.get("recommendations", []),
        llm_result.get("status", "unknown")
    )
    send_telegram(summary)

    log.info(f"=== Complete: {date_str} ===")
    return llm_result


if __name__ == "__main__":
    run()
