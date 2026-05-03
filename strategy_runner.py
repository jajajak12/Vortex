"""
strategy_runner.py — active Vortex S1-S6 strategy dispatch.

Each scan_sN function handles cooldown, weight gating, open-trade dedup,
risk manager evaluation, alert emission, and trade logging.
"""

from collections.abc import Callable

from config import ENABLE_MACRO_FILTER, OWN_MACRO_PAIRS, get_strategy_min_rr
from core.signal_handler import Signal
from risk_manager import TradeSetup
from scanner_utils import PairContext, ScanState
from trade_tracker import log_signal
from vortex_logger import get_logger
from weights import apply_weight_gate

log = get_logger(__name__)

SetupScanner = Callable[[str], list[dict]]


def check_weight_gate(
    state: ScanState,
    strategy_id: str,
    base_score: float,
    pair: str = "",
    direction: str = "",
) -> tuple[bool, float]:
    """Darwinian gate + lesson modifier. Returns (approved, final_score)."""
    from lessons_injector import get_score_modifier
    from weights import MIN_ACCEPTED_SCORE

    approved, final = apply_weight_gate(strategy_id, base_score)
    modifier = get_score_modifier(strategy_id, pair, direction)
    final += modifier
    if modifier != 0.0:
        log.info(f"  [LESSON] {strategy_id} modifier={modifier:+.1f} -> final={final:.2f}")
    if not approved or final < MIN_ACCEPTED_SCORE:
        log.info(f"  [WEIGHT GATE] {strategy_id} score={final:.2f} rejected")
        return False, final
    return True, final


def _macro_ok(ctx: PairContext, direction: str) -> bool:
    if not ENABLE_MACRO_FILTER or ctx.pair in OWN_MACRO_PAIRS:
        return True
    if direction == "LONG" and ctx.btc_macro == "BEAR":
        return False
    if direction == "SHORT" and ctx.btc_macro == "BULL":
        return False
    return True


def _already_open(pair: str, strategy_id: str, direction: str) -> bool:
    from db import get_open_trades

    for trade in get_open_trades(pair):
        if trade["strategy"] == strategy_id and trade["direction"] == direction:
            log.info(f"  [DEDUP] {pair} {strategy_id} {direction} already open")
            return True
    return False


def _emit_setup(
    ctx: PairContext,
    state: ScanState,
    strategy_id: str,
    scanner: SetupScanner,
    description: str,
    cooldown_store_name: str = "cd_ob_e",
) -> None:
    if ctx.lesson_ctx:
        log.info(f"[{strategy_id}] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")

    try:
        pair_cd_key = f"{ctx.pair}_{strategy_id}"
        cooldown = getattr(state, cooldown_store_name)
        if cooldown.is_on_cooldown(pair_cd_key):
            return

        for setup in scanner(ctx.pair):
            direction = setup["direction"]
            base_score = setup.get("confidence_score", 7.0)
            if strategy_id == "S4" and base_score < 8.0:
                continue
            if not _macro_ok(ctx, direction):
                continue
            if not setup.get("in_zone", True):
                continue
            if cooldown.is_on_cooldown(setup["zone_key"]):
                continue
            if _already_open(ctx.pair, strategy_id, direction):
                continue

            approved, _ = check_weight_gate(
                state, strategy_id, base_score, ctx.pair, direction
            )
            if not approved:
                continue

            trade = setup["trade"]
            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair,
                direction=direction,
                entry=trade["entry"],
                sl=trade["sl"],
                tp=trade["tp2"],
                strategy=strategy_id,
                atr=setup.get("atr", 0.0),
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=get_strategy_min_rr(strategy_id),
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"[{strategy_id}] risk rejected: {ctx.pair} - {risk.reason}")
                continue

            reason = _reason(strategy_id, setup, description)
            invalidation = trade["sl"]

            cooldown.set(setup["zone_key"])
            cooldown.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id=strategy_id,
                symbol=ctx.pair,
                direction=direction,
                timeframe=setup.get("tf_label", "4H"),
                entry_price=trade["entry"],
                sl_price=trade["sl"],
                tp1_price=trade["tp1"],
                tp2_price=trade["tp2"],
                rr=risk.rr_ratio,
                score=base_score,
                reason=reason,
                risk_percent=ctx.params["RISK_PCT"],
                position_size=risk.position_usdt,
                invalidation_price=invalidation,
            ))
            log_signal(
                ctx.pair,
                direction,
                trade["entry"],
                trade["sl"],
                trade["tp2"],
                confluence_score=base_score,
                regime_state=ctx.btc_macro,
                strategy=strategy_id,
                position_usdt=risk.position_usdt,
                rr=risk.rr_ratio,
            )
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            log.info(
                f"[{strategy_id}] {setup.get('type', description)} {direction}: "
                f"{ctx.pair} entry={trade['entry']} sl={trade['sl']} "
                f"tp={trade['tp2']} rr={risk.rr_ratio} score={base_score}"
            )
            break

    except Exception as exc:
        log.error(f"[{strategy_id} ERROR] {ctx.pair}: {exc}", exc_info=True)


def _reason(strategy_id: str, setup: dict, description: str) -> str:
    parts = [description]
    if "vol_ratio" in setup:
        parts.append(f"Volume {setup['vol_ratio']}x average.")
    if "body_ratio" in setup:
        parts.append(f"Body {setup['body_ratio']:.0%} of candle range.")
    if "swing_extreme" in setup:
        parts.append(f"Extreme level {setup['swing_extreme']:.4f}.")
    if "donchian_high" in setup:
        parts.append(f"Breakout above Donchian high {setup['donchian_high']:.4f}.")
    if setup.get("mss"):
        parts.append("MSS confirmed.")
    if setup.get("choch"):
        parts.append("CHOCH confirmed.")
    return " ".join(parts)


def scan_s1(ctx: PairContext, state: ScanState):
    from strategy1_bos_mss import scan_bos_mss

    _emit_setup(ctx, state, "S1", scan_bos_mss, "S4-MOMENTUM BOS+MSS setup.")


def scan_s2(ctx: PairContext, state: ScanState):
    from strategy2_ema_stack import scan_ema_stack

    _emit_setup(ctx, state, "S2", scan_ema_stack, "EMA stack pullback continuation.")


def scan_s3(ctx: PairContext, state: ScanState):
    from strategy3_p10_swing import scan_p10_swing

    _emit_setup(ctx, state, "S3", scan_p10_swing, "P10 swing extreme reversal.")


def scan_s4(ctx: PairContext, state: ScanState):
    from strategy4_vol_surge_bear import scan_vol_surge_bear

    _emit_setup(ctx, state, "S4", scan_vol_surge_bear, "Volume surge bear short.")


def scan_s5(ctx: PairContext, state: ScanState):
    from strategy5_vol_impulse import scan_vol_impulse

    _emit_setup(ctx, state, "S5", scan_vol_impulse, "4H bullish volume impulse close-high.")


def scan_s6(ctx: PairContext, state: ScanState):
    from strategy6_donchian_breakout import scan_donchian_breakout

    _emit_setup(ctx, state, "S6", scan_donchian_breakout, "4H 50-period Donchian breakout long.")


def run_all_strategies(ctx: PairContext, state: ScanState, current_price: float):
    scan_s1(ctx, state)
    scan_s2(ctx, state)
    scan_s3(ctx, state)
    scan_s4(ctx, state)
    scan_s5(ctx, state)
    scan_s6(ctx, state)
