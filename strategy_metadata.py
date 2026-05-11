"""
Shared Vortex strategy display metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy_registry import STRATEGY_REGISTRY


@dataclass(frozen=True)
class StrategyMeta:
    strategy_id: str
    strategy_name: str
    legacy_label: str


STRATEGY_META: dict[str, StrategyMeta] = {
    strategy_id: StrategyMeta(
        strategy_id=definition.strategy_id,
        strategy_name=definition.display_name,
        legacy_label=definition.legacy_label,
    )
    for strategy_id, definition in STRATEGY_REGISTRY.items()
}


def get_strategy_meta(strategy_id: str) -> StrategyMeta:
    return STRATEGY_META.get(
        strategy_id,
        StrategyMeta(strategy_id, strategy_id, strategy_id),
    )
