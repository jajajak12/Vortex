"""
weights.py — Darwinian Weighting for Vortex strategies.

Setiap strategi punya bobot dinamis berdasarkan performa:
  - Win          → weight +0.15  (max 2.5)
  - Loss         → weight -0.10  (min 0.3)
  - False signal → weight -0.20  (min 0.3)

Bobot dipakai di scanner.py:
  score_final = base_score × weight
  if score_final < MIN_ACCEPTED_SCORE (6.0): reject signal
"""

import json
import os
from pathlib import Path

WEIGHTS_FILE = Path("/home/prospera/vortex/weights.json")

# Bobot awal semua strategi
DEFAULT_WEIGHTS = {
    "S1-LIQ":   1.0,
    "S1-CHART": 1.0,
    "S2":       1.0,
    "S3":       1.0,
    "S4":       1.0,
    "S5":       1.0,
    "S6":       1.0,
}

WEIGHT_MIN  = 0.3
WEIGHT_MAX  = 2.5
WIN_DELTA   = 0.15
LOSS_DELTA  = -0.10
FALSE_DELTA = -0.20

# Min score untuk accept signal setelah weight applied
MIN_ACCEPTED_SCORE = 7.0

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    if WEIGHTS_FILE.exists():
        with open(WEIGHTS_FILE) as f:
            raw = json.load(f)
        # Merge with defaults (handle new strategies added later)
        merged = dict(DEFAULT_WEIGHTS)
        merged.update(raw)
        _cache = merged
    else:
        _cache = dict(DEFAULT_WEIGHTS)
    return _cache


def _save(w: dict):
    global _cache
    _cache = w
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(w, f, indent=2)


def get_weight(strategy_id: str) -> float:
    """Return bobot saat ini untuk strategi."""
    w = _load()
    return w.get(strategy_id, 1.0)


def get_all_weights() -> dict:
    """Return semua bobot."""
    return dict(_load())


def get_adjusted_score(strategy_id: str, base_score: float) -> float:
    """Apply weight ke base score. Return score_final."""
    return base_score * get_weight(strategy_id)


def apply_weight_gate(strategy_id: str, base_score: float) -> tuple[bool, float]:
    """
    Return (approved, score_final).
    Jika score_final < MIN_ACCEPTED_SCORE → reject (approved=False).
    """
    score_final = get_adjusted_score(strategy_id, base_score)
    return score_final >= MIN_ACCEPTED_SCORE, score_final


def update_weight(strategy_id: str, result: str) -> float:
    """
    Update bobot berdasarkan hasil trade.
    result: "WIN" | "LOSS" | "FALSE_SIGNAL"
    Return bobot baru.
    """
    w = _load()
    current = w.get(strategy_id, 1.0)

    if result == "WIN":
        delta = WIN_DELTA
    elif result == "LOSS":
        delta = LOSS_DELTA
    elif result == "FALSE_SIGNAL":
        delta = FALSE_DELTA
    else:
        return current

    new_weight = max(WEIGHT_MIN, min(WEIGHT_MAX, current + delta))
    w[strategy_id] = round(new_weight, 4)
    _save(w)
    return new_weight


def update_from_closed_trade(trade: dict) -> float | None:
    """
    Dipanggil saat trade closed. Ambil result dari trade dict.
    Return bobot baru, atau None jika tidak perlu update.
    """
    result = trade.get("result")
    strategy = trade.get("strategy")
    if not result or not strategy:
        return None
    # Skip OPEN trades
    if trade.get("status") != "CLOSED":
        return None
    return update_weight(strategy, result)


def reset_weights():
    """Reset semua bobot ke default. Untuk emergency use."""
    _save(dict(DEFAULT_WEIGHTS))
