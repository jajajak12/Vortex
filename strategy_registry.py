"""
Canonical dry-run strategy registry for the live Vortex runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import VALIDATED_TRADING_PAIRS


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_id: str
    display_name: str
    legacy_label: str
    direction: str
    timeframe: str
    planned_rr: float
    min_rr: float
    auto_open: bool
    active_pairs: tuple[str, ...]
    watch_pairs: tuple[str, ...]
    scanner_module: str
    scanner_func: str
    description: str

    @property
    def opens_trades(self) -> bool:
        return self.auto_open

    def pair_mode(self, pair: str) -> str:
        if pair in self.active_pairs:
            return "active"
        if pair in self.watch_pairs:
            return "watch_only"
        if pair in VALIDATED_TRADING_PAIRS:
            return "disabled"
        return "out_of_universe"


STRATEGY_REGISTRY: dict[str, StrategyDefinition] = {
    "S1": StrategyDefinition(
        strategy_id="S1",
        display_name="BOS+MSS Momentum",
        legacy_label="S4-MOMENTUM BOS+MSS",
        direction="LONG/SHORT",
        timeframe="4H/1H/30M",
        planned_rr=1.0,
        min_rr=1.0,
        auto_open=False,
        active_pairs=(),
        watch_pairs=tuple(VALIDATED_TRADING_PAIRS),
        scanner_module="strategy1_bos_mss",
        scanner_func="scan_bos_mss",
        description="S4-MOMENTUM BOS+MSS setup.",
    ),
    "S2": StrategyDefinition(
        strategy_id="S2",
        display_name="EMA Stack Trend",
        legacy_label="S6 EMA Stack",
        direction="LONG/SHORT",
        timeframe="4H/1H/30M",
        planned_rr=2.0,
        min_rr=2.0,
        auto_open=False,
        active_pairs=(),
        watch_pairs=tuple(VALIDATED_TRADING_PAIRS),
        scanner_module="strategy2_ema_stack",
        scanner_func="scan_ema_stack",
        description="EMA stack pullback continuation.",
    ),
    "S3": StrategyDefinition(
        strategy_id="S3",
        display_name="P10 Swing Reversal",
        legacy_label="S7 P10 Swing Reversal",
        direction="LONG/SHORT",
        timeframe="1H",
        planned_rr=1.0,
        min_rr=1.0,
        auto_open=False,
        active_pairs=(),
        watch_pairs=tuple(VALIDATED_TRADING_PAIRS),
        scanner_module="strategy3_p10_swing",
        scanner_func="scan_p10_swing",
        description="P10 swing extreme reversal.",
    ),
    "S4": StrategyDefinition(
        strategy_id="S4",
        display_name="Volume Surge Bear SHORT",
        legacy_label="S8 Volume Surge Bear SHORT",
        direction="SHORT",
        timeframe="4H",
        planned_rr=2.0,
        min_rr=2.0,
        auto_open=False,
        active_pairs=(),
        watch_pairs=tuple(VALIDATED_TRADING_PAIRS),
        scanner_module="strategy4_vol_surge_bear",
        scanner_func="scan_vol_surge_bear",
        description="Volume surge bear short.",
    ),
    "S5": StrategyDefinition(
        strategy_id="S5",
        display_name="Current Volume Impulse Bull Close-High LONG",
        legacy_label="volume_impulse_bull_close_high LONG",
        direction="LONG",
        timeframe="4H",
        planned_rr=2.0,
        min_rr=2.0,
        auto_open=True,
        active_pairs=("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"),
        watch_pairs=(),
        scanner_module="strategy5_vol_impulse",
        scanner_func="scan_vol_impulse",
        description="4H bullish volume impulse close-high.",
    ),
    "S6": StrategyDefinition(
        strategy_id="S6",
        display_name="Donchian Breakout LONG 50",
        legacy_label="donchian_breakout LONG 50-period",
        direction="LONG",
        timeframe="4H",
        planned_rr=2.0,
        min_rr=2.0,
        auto_open=False,
        active_pairs=(),
        watch_pairs=tuple(VALIDATED_TRADING_PAIRS),
        scanner_module="strategy6_donchian_breakout",
        scanner_func="scan_donchian_breakout",
        description="4H 50-period Donchian breakout long.",
    ),
    "S7": StrategyDefinition(
        strategy_id="S7",
        display_name="EMA200 Pullback",
        legacy_label="EMA200 Pullback",
        direction="LONG/SHORT",
        timeframe="4H",
        planned_rr=3.0,
        min_rr=2.99,
        auto_open=True,
        active_pairs=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        watch_pairs=("BNBUSDT",),
        scanner_module="strategy7_ema200_pullback",
        scanner_func="scan_ema200_pullback",
        description="4H EMA200 pullback continuation.",
    ),
    "S8": StrategyDefinition(
        strategy_id="S8",
        display_name="Donchian Daily EMA200 LONG",
        legacy_label="Donchian Daily EMA200 LONG",
        direction="LONG",
        timeframe="4H + 1D",
        planned_rr=3.0,
        min_rr=2.99,
        auto_open=True,
        active_pairs=("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"),
        watch_pairs=(),
        scanner_module="strategy8_donchian_daily_ema200",
        scanner_func="scan_donchian_daily_ema200_long",
        description="4H Donchian breakout with daily EMA200 filter.",
    ),
    "S9": StrategyDefinition(
        strategy_id="S9",
        display_name="VWAP Bounce",
        legacy_label="VWAP Bounce",
        direction="LONG/SHORT",
        timeframe="4H",
        planned_rr=1.5,
        min_rr=1.49,
        auto_open=True,
        active_pairs=("ETHUSDT", "BNBUSDT", "SOLUSDT"),
        watch_pairs=("BTCUSDT",),
        scanner_module="strategy9_vwap_bounce",
        scanner_func="scan_vwap_bounce",
        description="4H VWAP20 bounce reversal/continuation.",
    ),
    "S10": StrategyDefinition(
        strategy_id="S10",
        display_name="Vol_Break LONG",
        legacy_label="Vol_Break LONG",
        direction="LONG",
        timeframe="4H",
        planned_rr=3.0,
        min_rr=2.99,
        auto_open=True,
        active_pairs=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        watch_pairs=("BNBUSDT",),
        scanner_module="strategy10_vol_break_long",
        scanner_func="scan_vol_break_long",
        description="4H volume breakout above prior 20-bar high.",
    ),
}

STRATEGY_ORDER: tuple[str, ...] = tuple(STRATEGY_REGISTRY.keys())


def get_strategy_definition(strategy_id: str) -> StrategyDefinition:
    return STRATEGY_REGISTRY[strategy_id]


def get_auto_open_strategy_ids() -> list[str]:
    return [sid for sid, definition in STRATEGY_REGISTRY.items() if definition.auto_open]


def get_watch_only_strategy_ids() -> list[str]:
    return [sid for sid, definition in STRATEGY_REGISTRY.items() if not definition.auto_open]
