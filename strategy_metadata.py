"""
Shared Vortex strategy display metadata.

Separates:
  - Vortex strategy ID (S1-S6)
  - clean production strategy name
  - legacy/backtest label
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyMeta:
    strategy_id: str
    strategy_name: str
    legacy_label: str


STRATEGY_META: dict[str, StrategyMeta] = {
    "S1": StrategyMeta("S1", "BOS+MSS Momentum", "S4-MOMENTUM BOS+MSS"),
    "S2": StrategyMeta("S2", "EMA Stack Trend", "S6 EMA Stack"),
    "S3": StrategyMeta("S3", "P10 Swing Reversal", "S7 P10 Swing Reversal"),
    "S4": StrategyMeta("S4", "Volume Surge Bear SHORT", "S8 Volume Surge Bear SHORT"),
    "S5": StrategyMeta("S5", "Volume Impulse Bull Close-High LONG", "volume_impulse_bull_close_high LONG"),
    "S6": StrategyMeta("S6", "Donchian Breakout LONG 50", "donchian_breakout LONG 50-period"),
}


def get_strategy_meta(strategy_id: str) -> StrategyMeta:
    return STRATEGY_META.get(
        strategy_id,
        StrategyMeta(strategy_id, strategy_id, strategy_id),
    )
