"""
strategy_runner.py — active Vortex S1-S6 strategy dispatch.

Each scan_sN function handles cooldown, weight gating, open-trade dedup,
risk manager evaluation, alert emission, and trade logging.
"""

from collections.abc import Callable

from config import (
    ENABLE_MACRO_FILTER,
    OWN_MACRO_PAIRS,
    get_strategy_min_rr,
    is_validated_trading_pair,
)
from core.signal_handler import Signal
from risk_manager import TradeSetup
from scanner_utils import PairContext, ScanState, StrategyDecision
from strategy_metadata import get_strategy_meta
from trade_tracker import log_signal
from vortex_logger import get_logger
from weights import apply_weight_gate

log = get_logger(__name__)

SetupScanner = Callable[[str], list[dict]]


def _diag_record(
    state: ScanState,
    pair: str,
    strategy_id: str,
    evaluated: bool,
    raw_signal: bool,
    opened: bool = False,
    blocked_reason: str = "",
    detail: str = "",
) -> None:
    meta = get_strategy_meta(strategy_id)
    state.diagnostics.record(StrategyDecision(
        pair=pair,
        strategy_id=meta.strategy_id,
        strategy_name=meta.strategy_name,
        legacy_label=meta.legacy_label,
        evaluated=evaluated,
        raw_signal=raw_signal,
        opened=opened,
        blocked_reason=blocked_reason,
        detail=detail,
    ))


def check_weight_gate(
    state: ScanState,
    strategy_id: str,
    base_score: float,
    pair: str = "",
    direction: str = "",
) -> tuple[bool, dict]:
    """Darwinian gate + lesson modifier with full diagnostics payload."""
    from lessons_injector import get_score_modifier
    from weights import MIN_ACCEPTED_SCORE

    meta = get_strategy_meta(strategy_id)
    approved, gate_score, weighted_score, weight = apply_weight_gate(strategy_id, base_score)
    modifier = get_score_modifier(strategy_id, pair, direction)
    base_plus_lessons = base_score + modifier
    final_score = gate_score + modifier
    if not approved:
        learn_result = "rejected"
        learn_reason = "gate_score_below_min"
    elif final_score < MIN_ACCEPTED_SCORE:
        learn_result = "rejected"
        learn_reason = "final_score_below_min_after_lessons"
    else:
        learn_result = "approved"
        learn_reason = "approved"
    log.info(
        "[LEARN][LESSON_MODIFIER] "
        f"pair={pair or '-'} "
        f"strategy={strategy_id} "
        f'name="{meta.strategy_name}" '
        f"direction={direction or '-'} "
        f"base={base_score:.2f} "
        f"lesson_modifier={modifier:+.2f} "
        f"base_plus_lessons={base_plus_lessons:.2f} "
        f"weight={weight:.2f} "
        f"weighted_from_base={weighted_score:.2f} "
        f"gate_from_base={gate_score:.2f} "
        f"final={final_score:.2f} "
        f"result={learn_result} "
        f"reason={learn_reason}"
    )
    if not approved or final_score < MIN_ACCEPTED_SCORE:
        log.info(
            f"  [WEIGHT GATE] {strategy_id} rejected "
            f"base={base_score:.2f} weight={weight:.2f} "
            f"weighted={weighted_score:.2f} gate={gate_score:.2f} "
            f"final={final_score:.2f}"
        )
        return False, {
            "base_score": base_score,
            "weight": weight,
            "weighted_score": weighted_score,
            "gate_score": gate_score,
            "modifier": modifier,
            "base_plus_lessons": base_plus_lessons,
            "final_score": final_score,
        }
    return True, {
        "base_score": base_score,
        "weight": weight,
        "weighted_score": weighted_score,
        "gate_score": gate_score,
        "modifier": modifier,
        "base_plus_lessons": base_plus_lessons,
        "final_score": final_score,
    }


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
    if not is_validated_trading_pair(ctx.pair):
        log.info(
            f"[{strategy_id}] {ctx.pair} skipped: "
            "research/watchlist pair not eligible for dry-run trade opening"
        )
        _diag_record(
            state, ctx.pair, strategy_id,
            evaluated=False,
            raw_signal=False,
            blocked_reason="pair_not_validated",
        )
        return

    if ctx.lesson_ctx:
        log.info(f"[{strategy_id}] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")

    try:
        pair_cd_key = f"{ctx.pair}_{strategy_id}"
        cooldown = getattr(state, cooldown_store_name)
        if cooldown.is_on_cooldown(pair_cd_key):
            _diag_record(
                state, ctx.pair, strategy_id,
                evaluated=True,
                raw_signal=False,
                blocked_reason="cooldown_pair",
            )
            return

        setups = scanner(ctx.pair)
        if not setups:
            _diag_record(
                state, ctx.pair, strategy_id,
                evaluated=True,
                raw_signal=False,
                blocked_reason="no_raw_signal",
            )
            return

        blocked_reason = "no_opened_setup"
        blocked_detail = f"raw_setups={len(setups)}"

        for setup in setups:
            direction = setup["direction"]
            base_score = setup.get("confidence_score", 7.0)
            if strategy_id == "S4" and base_score < 8.0:
                blocked_reason = "score_floor_s4"
                blocked_detail = f"score={base_score:.2f}"
                continue
            if not _macro_ok(ctx, direction):
                blocked_reason = "macro_blocked"
                blocked_detail = f"direction={direction} btc_macro={ctx.btc_macro}"
                continue
            if not setup.get("in_zone", True):
                blocked_reason = "not_in_zone"
                continue
            if cooldown.is_on_cooldown(setup["zone_key"]):
                blocked_reason = "cooldown_zone"
                blocked_detail = f"zone_key={setup['zone_key']}"
                continue
            if _already_open(ctx.pair, strategy_id, direction):
                blocked_reason = "duplicate_open_trade"
                blocked_detail = f"direction={direction}"
                continue

            approved, gate_info = check_weight_gate(
                state, strategy_id, base_score, ctx.pair, direction
            )
            if not approved:
                blocked_reason = "weight_gate"
                blocked_detail = (
                    f"base={gate_info['base_score']:.2f} "
                    f"weight={gate_info['weight']:.2f} "
                    f"weighted={gate_info['weighted_score']:.2f} "
                    f"gate={gate_info['gate_score']:.2f} "
                    f"final={gate_info['final_score']:.2f}"
                )
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
                blocked_reason = "risk_gate"
                blocked_detail = risk.reason
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
            _diag_record(
                state, ctx.pair, strategy_id,
                evaluated=True,
                raw_signal=True,
                opened=True,
                detail=(
                    f"direction={direction} "
                    f"base={gate_info['base_score']:.2f} "
                    f"weight={gate_info['weight']:.2f} "
                    f"weighted={gate_info['weighted_score']:.2f} "
                    f"gate={gate_info['gate_score']:.2f} "
                    f"final={gate_info['final_score']:.2f} "
                    f"rr={risk.rr_ratio:.2f}"
                ),
            )
            break
        else:
            _diag_record(
                state, ctx.pair, strategy_id,
                evaluated=True,
                raw_signal=True,
                blocked_reason=blocked_reason,
                detail=blocked_detail,
            )

    except Exception as exc:
        log.error(f"[{strategy_id} ERROR] {ctx.pair}: {exc}", exc_info=True)
        _diag_record(
            state, ctx.pair, strategy_id,
            evaluated=False,
            raw_signal=False,
            blocked_reason="scanner_error",
            detail=str(exc),
        )


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
