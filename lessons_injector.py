"""
lessons_injector.py — Lesson Injection for Vortex strategies.

Flow:
  1. Load lessons.json saat runtime
  2. Filter lessons yang relevant untuk strategi yang sedang discan
  3. Inject max 3 lessons paling relevan ke prompt/context
  4. Scanner pakai injected lessons untuk加强/减弱 decision

Lesson types:
  - PREFER  → strategi cenderung bagus di kondisi ini, boost confidence
  - AVOID   → strategi cenderung gagal di kondisi ini, warn + mungkin reject
  - DIRECTIONAL → macro context untuk keputusan direction
"""

import json
import re
from pathlib import Path
from typing import Optional

LESSONS_FILE = Path("/home/prospera/vortex/lessons.json")

# Mapping strategi → keyword yang relevance-check
# Strategi specific tags
_STRATEGY_TAGS = {
    "S1-LIQ":   ["S1", "LIQUIDITY", "LIQ", "ZONE", "GRAB"],
    "S1-CHART": ["S1", "CHART", "PATTERN", "WEDGE", "FLAG", "BREAKOUT"],
    "S2":       ["S2", "WICK", "REJECTION", "FILL"],
    "S3":       ["S3", "FVG", "IMBALANCE", "FAIR_VALUE"],
    "S4":       ["S4", "ORDER_BLOCK", "OB", "BREAKER"],
    "S5":       ["S5", "ENGINEERED", "LIQUIDATION", "CLUSTER"],
    "S6":       ["S6", "BOS", "MSS", "CHOC", "BREAK_STRUCTURE"],
}

# Pair-specific lessons
_PAIR_TAGS = {
    "SOLUSDT": ["SOL"],
    "ETHUSDT": ["ETH"],
    "BTCUSDT": ["BTC"],
    "XAUUSDT": ["GOLD", "XAU"],
}

_cache: list | None = None


def _load_lessons() -> list:
    global _cache
    if _cache is not None:
        return _cache
    if not LESSONS_FILE.exists():
        _cache = []
        return _cache
    with open(LESSONS_FILE) as f:
        _cache = json.load(f)
    return _cache


def _score_relevance(lesson: dict, strategy_id: str, pair: str = "") -> float:
    """
    Return relevance score 0.0-1.0.
    Higher = more relevant untuk strategy ini.
    """
    score = 0.0
    desc = lesson.get("description", "").upper()
    evidence = lesson.get("data_evidence", "").upper()
    text = desc + " " + evidence

    # Strategy match
    strat_tags = _STRATEGY_TAGS.get(strategy_id, [])
    lesson_strats = [s.upper() for s in lesson.get("strategies", [])]
    for tag in strat_tags:
        if tag in text or any(tag in ls for ls in lesson_strats):
            score += 0.4
            break

    # Pair match
    if pair:
        pair_tags = _PAIR_TAGS.get(pair, [pair.replace("USDT", "").upper()])
        for tag in pair_tags:
            if tag in text:
                score += 0.3
                break

    # Lesson type weight
    ltype = lesson.get("type", "").upper()
    if ltype == "AVOID":
        score += 0.2  # AVOID lessons always somewhat relevant
    elif ltype == "PREFER":
        score += 0.1

    return min(score, 1.0)


def get_relevant_lessons(
    strategy_id: str,
    pair: str = "",
    max_lessons: int = 3,
    min_relevance: float = 0.1,
) -> list[dict]:
    """
    Return max=max_lessons lessons yang paling relevan untuk strategy_id + pair.
    """
    lessons = _load_lessons()
    if not lessons:
        return []

    scored = []
    for lesson in lessons:
        rel = _score_relevance(lesson, strategy_id, pair)
        if rel >= min_relevance:
            scored.append((rel, lesson))

    # Sort descending by relevance
    scored.sort(key=lambda x: x[0], reverse=True)
    return [l for _, l in scored[:max_lessons]]


def inject_lessons_to_context(
    strategy_id: str,
    pair: str = "",
    extra_context: str = "",
) -> str:
    """
    Return string lessons yang ready untuk diinject ke scanner context.
    Format: newline-separated lesson lines, prefixed dengan type tag.
    Jika tidak ada lessons → return empty string.
    """
    lessons = get_relevant_lessons(strategy_id, pair, max_lessons=3)
    if not lessons:
        return ""

    lines = ["[LESSONS FROM HISTORICAL DATA]"]
    for lesson in lessons:
        ltype = lesson.get("type", "INFO")
        desc  = lesson.get("description", "")
        ev    = lesson.get("data_evidence", "")
        lines.append(f"  [{ltype}] {desc}")
        if ev:
            lines.append(f"         Evidence: {ev}")

    lines.append("")
    return "\n".join(lines)


def lessons_summary() -> str:
    """Compact summary semua lessons untuk logging."""
    lessons = _load_lessons()
    if not lessons:
        return "No lessons yet."
    by_type = {}
    for l in lessons:
        t = l.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
    return ", ".join(f"{v} {k}" for k, v in by_type.items())


# ── Pre-canned prompts per strategy ─────────────────────────────────────────

_PROMPT_TEMPLATES = {
    "S1-LIQ": """[S1-LIQ CONTEXT]
- Detect liquidity grab zones on 4H
- Entry on 30m false breakout with wick rejection
- SL tepat di luar zona (LONG: zone.low × 0.998, SHORT: zone.high × 1.002)
- TP: 1:1 RR dari SL distance
{LESSONS}""",

    "S1-CHART": """[S1-CHART CONTEXT]
- Rising/Falling Wedge, H&S, Inverse H&S, Bull/Bear Flag
- Detection on 4H, confirmation on 1H, entry on 30m
- Pattern harus ada displacement > 50% body确认
{LESSONS}""",

    "S2": """[S2 CONTEXT]
- Wick Fill: price wicked beyond zone, returned inside
- LONG: upside wick above zone.high → price closes back below zone.high
- SHORT: downside wick below zone.low → price closes back above zone.low
- TF: 1W/1D/4H detect, 1H confirm, 30m entry
{LESSONS}""",

    "S3": """[S3 CONTEXT]
- FVG + Imbalance zones
- Detection 4H, confirmation 1H, entry 30m
- Mandatory wick rejection on 30m candle
- Min score 7.0 for entry
{LESSONS}""",

    "S4": """[S4 CONTEXT]
- Order Block + Breaker Block
- OB = 1-3 candle bullish body sebelum bearish move (bullish OB) / vice versa
- Breaker = OB yang udah break dan retest
- Mandatory wick rejection + displacement check
{LESSONS}""",

    "S5": """[S5 CONTEXT]
- Engineered Liquidity Reversal
- Cek cluster liquidity di atas swing high / bawah swing low
- Displacement harus > 50% body
- Min 3T candle untuk displacement确认
{LESSONS}""",

    "S6": """[S6 CONTEXT]
- BOS + MSS / CHOCH
- BOS = Break of Structure pada 4H/1H
- MSS = Market Structure Shift (HH/HL break untuk bullish, LH/LL break untuk bearish)
- CHOCH = Change of Character (retest + rejection setelah BOS)
- Mandatory wick rejection, MSS hold minimal 3 candles
{LESSONS}""",
}


def get_strategy_context(strategy_id: str, pair: str = "") -> str:
    """
    Return base context untuk strategy dengan lessons injected.
    Dipakai di awal _scan_sN() sebelum decision logic.
    """
    template = _PROMPT_TEMPLATES.get(strategy_id, "[STRATEGY {id}]\n{LESSONS}")
    lessons_block = inject_lessons_to_context(strategy_id, pair)
    return template.replace("{LESSONS}", lessons_block).replace("{id}", strategy_id)
