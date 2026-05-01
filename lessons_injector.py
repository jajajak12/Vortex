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
    "SOLUSDT": ["SOLUSDT", "SOL"],
    "ETHUSDT": ["ETHUSDT", "ETH"],
    "BTCUSDT": ["BTCUSDT", "BTC"],
    "XAUUSDT": ["XAUUSDT", "GOLD", "XAU"],
}

_cache: list | None = None
_cache_mtime: float = 0.0


def _load_lessons() -> list:
    """Load lessons dengan mtime-based cache invalidation.
    Auto-reload jika lessons.json diupdate oleh daily_analysis.
    """
    global _cache, _cache_mtime
    if not LESSONS_FILE.exists():
        _cache = []
        return _cache
    mtime = LESSONS_FILE.stat().st_mtime
    if _cache is not None and mtime <= _cache_mtime:
        return _cache  # file belum berubah
    with open(LESSONS_FILE) as f:
        _cache = json.load(f)
    _cache_mtime = mtime
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
    lesson_strats = [s.upper() for s in (lesson.get("strategies") or [])]
    strategy_matched = False
    for tag in strat_tags:
        if tag in text or any(tag in ls for ls in lesson_strats):
            score += 0.4
            strategy_matched = True
            break

    # Pair match
    pair_matched = False
    ltype = lesson.get("type", "").upper()
    if pair:
        # DIRECTIONAL lessons with no strategies field: extract explicit pair from
        # description (first word before SHORT/LONG) — must match exactly.
        # Prevents evidence text from leaking into wrong-pair scans.
        if ltype == "DIRECTIONAL" and not lesson.get("strategies"):
            m = re.search(r'\b([A-Z]{2,10}USDT)\b\s+(SHORT|LONG)', desc)
            extracted_pair = m.group(1) if m else None
            if extracted_pair != pair.upper():
                return 0.0
            pair_matched = True
            score += 0.3
        else:
            pair_tags = _PAIR_TAGS.get(pair, [pair.replace("USDT", "").upper()])
            for tag in pair_tags:
                if re.search(r'\b' + re.escape(tag) + r'\b', text):
                    score += 0.3
                    pair_matched = True
                    break

    # Lesson type weight — only add if at least one anchor (strategy or pair) matched.
    # Lessons with no strategy tag must have a pair match to get the type bonus;
    # otherwise they'd leak into every strategy via the 0.2 AVOID bonus alone.
    has_strategy_tag = bool(lesson.get("strategies"))
    if strategy_matched or pair_matched or has_strategy_tag:
        if ltype == "AVOID":
            score += 0.2
        elif ltype == "PREFER":
            score += 0.1
    elif score == 0.0:
        return 0.0

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


def get_score_modifier(
    strategy_id: str,
    pair: str = "",
    direction: str = "",
) -> float:
    """
    Return score delta berdasarkan lessons yang relevan.
    Dipanggil oleh scanner sebelum weight gate — memodifikasi base_score.

    AVOID   → -0.5 per lesson (cap -1.5)
    PREFER  → +0.3 per lesson (cap +0.9)
    DIRECTIONAL → +0.2 jika arah cocok, -0.2 jika berlawanan
    """
    lessons = get_relevant_lessons(strategy_id, pair, max_lessons=3)
    delta = 0.0
    for lesson in lessons:
        ltype = lesson.get("type", "").upper()
        if ltype == "AVOID":
            delta -= 0.5
        elif ltype == "PREFER":
            delta += 0.3
        elif ltype == "DIRECTIONAL" and direction:
            # Only apply if lesson is explicitly about this pair (not a different pair's lesson
            # that happens to mention this pair in evidence)
            lesson_desc = lesson.get("description", "").upper()
            if pair and pair.upper() not in lesson_desc:
                continue
            if direction.upper() in lesson_desc:
                delta += 0.2
            elif ("LONG" in lesson_desc and direction == "SHORT") or \
                 ("SHORT" in lesson_desc and direction == "LONG"):
                delta -= 0.2
    return max(-1.5, min(0.9, round(delta, 2)))


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
