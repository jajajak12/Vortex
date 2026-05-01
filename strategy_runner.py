"""
strategy_runner.py — P1.1: Strategy scan functions as module-level functions.

Extracted from VortexScanner._scan_sX() methods.
Each function takes (ctx: PairContext, state: ScanState) — no self., fully testable.
scanner.py calls these via run_all_strategies().
"""

from vortex_logger import get_logger
from config import ENABLE_MACRO_FILTER, OWN_MACRO_PAIRS
from scanner_utils import PairContext, ScanState
from strategy1_liquidity import (
    get_fresh_liquidity_zones, get_candles,
    is_touching_zone, check_rejection_long, check_rejection_short,
    calculate_trade,
)
from core.signal_handler import Signal
from trade_tracker import log_signal
from risk_manager import TradeSetup
from weights import apply_weight_gate
from telegram_bot import alert_touch

log = get_logger(__name__)


# ── Shared helpers ────────────────────────────────────────────────────────────

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
        log.info(f"  📚 [LESSON] {strategy_id} modifier={modifier:+.1f} → final={final:.2f}")
    if not approved or final < MIN_ACCEPTED_SCORE:
        log.info(f"  ⛔ [WEIGHT GATE] {strategy_id} score={final:.2f} — REJECTED")
        return False, final
    return True, final


def get_wick_rejection(
    pair: str, direction: str, zone_price: float,
    zone_data: dict | None = None,
) -> dict | None:
    """MANDATORY wick rejection check at 30m/15m/5m. Used by S1-CHART, S4, S6."""
    try:
        from strategy2_wick import is_long_downside_wick, is_long_upside_wick
    except Exception:
        return None
    for tf in ("30m", "15m", "5m"):
        try:
            candles_tf = get_candles(pair, tf, limit=20)
        except Exception:
            continue
        if len(candles_tf) < 2:
            continue
        for c in candles_tf[-4:]:
            if direction == "LONG":
                wick = is_long_downside_wick(c)
                if wick and abs(wick["wick_low"] - zone_price) / zone_price < 0.005:
                    return {"tf": tf, "wick": wick}
            else:
                wick = is_long_upside_wick(c)
                if wick and abs(wick["wick_high"] - zone_price) / zone_price < 0.005:
                    return {"tf": tf, "wick": wick}
    return None


def _find_tp_target(entry: float, opposite_zones: list, direction: str) -> float | None:
    if direction == "LONG":
        candidates = [z["pivot"] for z in opposite_zones if z["pivot"] > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [z["pivot"] for z in opposite_zones if z["pivot"] < entry]
        return max(candidates) if candidates else None


def _macro_ok(ctx: PairContext, direction: str) -> bool:
    """Return True if macro filter allows this direction."""
    if not ENABLE_MACRO_FILTER or ctx.pair in OWN_MACRO_PAIRS:
        return True
    if direction == "LONG"  and ctx.btc_macro == "BEAR": return False
    if direction == "SHORT" and ctx.btc_macro == "BULL": return False
    return True


def _already_open(pair: str, strategy_prefix: str, direction: str) -> bool:
    """True if DB has an OPEN trade for this pair+strategy+direction. Persists across restarts."""
    from db import get_open_trades
    for t in get_open_trades(pair):
        if t["strategy"].startswith(strategy_prefix) and t["direction"] == direction:
            log.info(f"  🚫 [DEDUP] {pair} {strategy_prefix} {direction} already OPEN — skip")
            return True
    return False


# ── S1: Fresh Liquidity Grab ──────────────────────────────────────────────────

def scan_s1(ctx: PairContext, state: ScanState, current_price: float):
    if ctx.lesson_ctx:
        log.info(f"[S1] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")
    try:
        pair_cd_key = f"{ctx.pair}_S1"
        if state.cd_entry.is_on_cooldown(pair_cd_key):
            return

        zones    = get_fresh_liquidity_zones(ctx.pair)
        htf_bias = zones["htf_bias"]

        if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS \
                and htf_bias != ctx.btc_macro:
            return
        valid_dir = htf_bias
        all_zones = [z for z in zones["LONG"] + zones["SHORT"] if z["type"] == valid_dir]
        if not all_zones:
            return

        for zone in all_zones:
            ckey = f"{ctx.pair}_{zone['type']}_{zone['pivot']:.4f}"

            if not is_touching_zone(current_price, zone,
                                    threshold_pct=ctx.params["TOUCH_THRESHOLD_PCT"]):
                continue

            if not state.cd_touch.is_on_cooldown(ckey):
                log.info(f"⚠️  [S1] TOUCH: {ctx.pair} @ {current_price:.4f} "
                         f"| {zone['type']} {zone['pivot']:.4f}")
                alert_touch(ctx.pair, current_price, zone["low"], zone["high"], zone["type"])
                state.cd_touch.set(ckey)

            if state.cd_entry.is_on_cooldown(ckey):
                continue

            candles_5m = get_candles(ctx.pair, "5m", limit=50)
            req_vol    = ctx.params["REQUIRE_VOLUME_SPIKE"]
            rejection  = (check_rejection_long(candles_5m, zone, vol_spike_required=req_vol)
                          if valid_dir == "LONG"
                          else check_rejection_short(candles_5m, zone, vol_spike_required=req_vol))

            if not (rejection and rejection["confirmed"]):
                continue

            tp_zones  = zones["SHORT"] if valid_dir == "LONG" else zones["LONG"]
            tp_target = _find_tp_target(rejection["entry_price"], tp_zones, valid_dir)
            trade     = calculate_trade(valid_dir, rejection["entry_price"], zone, tp_target)

            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=valid_dir,
                entry=trade["entry"], sl=trade["sl"], tp=trade["tp"],
                strategy="S1",
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S1] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            vol     = rejection.get("volume_spike", False)
            s1_score = 6.0 + (1.5 if vol else 0) + (1.0 if risk.rr_ratio >= 2.5 else 0.5 if risk.rr_ratio >= 2.0 else 0)

            approved_w, _ = check_weight_gate(state, "S1", s1_score, ctx.pair, valid_dir)
            if not approved_w:
                continue

            if _already_open(ctx.pair, "S1", valid_dir):
                continue

            vol_note = " dengan volume spike" if vol else ""
            log.info(f"✅ [S1] ENTRY: {ctx.pair} {valid_dir} | "
                     f"E={trade['entry']} SL={trade['sl']} TP={trade['tp']} "
                     f"RR={risk.rr_ratio} Size=${risk.position_usdt}"
                     + (" 🔥 Volume spike!" if vol else ""))
            reason = (f"Fresh {valid_dir} liquidity zone "
                      f"${zone['low']:.4f}–${zone['high']:.4f}. "
                      f"False breakout 5m{vol_note} confirmed, close kembali di dalam zona.")
            state.cd_entry.set(ckey)
            state.cd_entry.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id="S1", symbol=ctx.pair, direction=valid_dir, timeframe="30m",
                entry_price=trade["entry"], sl_price=trade["sl"], tp1_price=trade["tp"],
                tp2_price=None, rr=risk.rr_ratio, score=s1_score, reason=reason,
                risk_percent=ctx.params["RISK_PCT"], position_size=risk.position_usdt,
                invalidation_price=zone["low"] if valid_dir == "LONG" else zone["high"],
            ))
            log_signal(ctx.pair, valid_dir, trade["entry"], trade["sl"], trade["tp"],
                       regime_state=ctx.btc_macro, strategy="S1",
                       position_usdt=risk.position_usdt, rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per cycle

    except Exception as e:
        log.error(f"[S1 ERROR] {ctx.pair}: {e}", exc_info=True)


# ── S1-CHART: Classic Chart Patterns ─────────────────────────────────────────

def scan_s1_chart(ctx: PairContext, state: ScanState, current_price: float):
    if ctx.lesson_ctx:
        log.info(f"[S1-CHART] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")
    try:
        if not ctx.chart_setups:
            return
        pair_cd_key = f"{ctx.pair}_S1CHART"
        if state.cd_entry.is_on_cooldown(pair_cd_key):
            return
        for setup in ctx.chart_setups:
            direction = setup["direction"]
            if not _macro_ok(ctx, direction):
                continue

            zone_mid  = setup["zone_mid"]
            ckey = f"{ctx.pair}_{direction}_{setup['pattern']}_{zone_mid:.4f}"
            if state.cd_entry.is_on_cooldown(ckey):
                continue

            zone_high = setup["zone_high"]
            zone_low  = setup["zone_low"]
            ref_price = zone_high if direction == "LONG" else zone_low
            if not get_wick_rejection(ctx.pair, direction, ref_price):
                continue

            entry = zone_mid
            atr   = ctx.params.get("ATR_SL_MIN_MULT", 1.0) * ctx.params.get("atr_mult", 0.015) * entry
            sl    = (zone_low  - 1.5 * atr) if direction == "LONG" else (zone_high + 1.5 * atr)
            dist  = abs(entry - sl)
            tp1   = entry + dist * 2.0 if direction == "LONG" else entry - dist * 2.0
            tp2   = entry + dist * 3.0 if direction == "LONG" else entry - dist * 3.0

            score = 5.5
            if setup.get("vol_confirmed"): score += 1.5
            score += min(setup.get("atr_pct", 0) / 10.0, 1.5)
            if score < 8.0:
                continue

            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=direction,
                entry=entry, sl=sl, tp=tp1, strategy="S1-CHART",
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S1-CHART] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            if _already_open(ctx.pair, "S1", direction):
                continue

            log.info(f"✅ [S1-CHART] ENTRY {direction}: {ctx.pair} {setup['pattern']} "
                     f"E={entry:.4f} SL={sl:.4f} TP1={tp1:.4f} RR={risk.rr_ratio:.1f} Score={score:.1f}")
            reason = (f"{setup['pattern']} confirmed on 4H. "
                      f"Breakout on 1H with {setup['vol_ratio']:.1f}x volume. "
                      f"Entry at broken channel {direction}.")
            inv = zone_low if direction == "LONG" else zone_high
            state.cd_entry.set(ckey)
            state.cd_entry.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id="S1-CHART", symbol=ctx.pair, direction=direction, timeframe="30m",
                entry_price=entry, sl_price=sl, tp1_price=tp1, tp2_price=tp2,
                rr=risk.rr_ratio, score=round(score, 1), reason=reason,
                risk_percent=ctx.params["RISK_PCT"], position_size=risk.position_usdt,
                invalidation_price=inv,
            ))
            log_signal(ctx.pair, direction, entry, sl, tp1,
                       regime_state=ctx.btc_macro, strategy="S1-CHART",
                       position_usdt=risk.position_usdt, rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per cycle

    except Exception as e:
        log.error(f"[S1-CHART ERROR] {ctx.pair}: {e}", exc_info=True)


# ── S2: Wick Fill ─────────────────────────────────────────────────────────────

def scan_s2(ctx: PairContext, state: ScanState):
    try:
        pair_cd_key = f"{ctx.pair}_S2"
        if state.cd_wick_e.is_on_cooldown(pair_cd_key):
            return
        for setup in ctx.wick_setups:
            direction = setup.get("direction", "LONG")
            w   = setup["wick"]
            ref = w.get("wick_low") or w.get("wick_high", 0)
            wk  = f"{ctx.pair}_{direction}_{setup['tf']}_{ref:.4f}"

            if wk not in state.seen_wick:
                log.info(f"🕯️  [S2] WICK {direction}: {ctx.pair} "
                         f"{setup['tf_label']} | Ref={ref} | {setup['confluence_label']}")
                state.seen_wick.add(wk)

            if not _macro_ok(ctx, direction):
                continue
            if not setup["in_entry_zone"]:
                continue
            if state.cd_wick_e.is_on_cooldown(wk):
                continue

            candles_5m = get_candles(ctx.pair, "5m", limit=50)
            req_vol    = ctx.params["REQUIRE_VOLUME_SPIKE"]
            if direction == "LONG":
                wick_zone = {"low": w["wick_low"], "high": w["wick_50pct"], "pivot": w["wick_low"]}
                rejection = check_rejection_long(candles_5m, wick_zone, vol_spike_required=req_vol)
            else:
                wick_zone = {"low": w["wick_50pct"], "high": w["wick_high"], "pivot": w["wick_high"]}
                rejection = check_rejection_short(candles_5m, wick_zone, vol_spike_required=req_vol)

            if not (rejection and rejection["confirmed"]):
                continue

            t    = setup["trade"]
            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=direction,
                entry=t["entry"], sl=t["sl"], tp=t["tp2"], strategy="S2",
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S2] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            if _already_open(ctx.pair, "S2", direction):
                continue

            log.info(f"✅ [S2] WICK ENTRY {direction}: {ctx.pair} "
                     f"{setup['tf_label']} @ {setup['current_price']} Size=${risk.position_usdt}")
            ema_note  = " dekat EMA50" if setup["ema_info"]["has_confluence"] else ""
            reason_s2 = (f"{setup['tf_label']} wick {setup['wick']['wick_body_ratio']}x body{ema_note}. "
                         f"Price di fill zone, rejection 5m confirmed.")
            inv_s2 = w["wick_low"] if direction == "LONG" else w["wick_high"]
            state.cd_wick_e.set(wk)
            state.cd_wick_e.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id="S2", symbol=ctx.pair, direction=direction,
                timeframe=setup["tf_label"], entry_price=t["entry"],
                sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                rr=risk.rr_ratio, score=setup["confluence_score"] * 2.0,
                reason=reason_s2, risk_percent=ctx.params["RISK_PCT"],
                position_size=risk.position_usdt, invalidation_price=inv_s2,
            ))
            log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                       regime_state=ctx.btc_macro, strategy="S2",
                       position_usdt=risk.position_usdt, rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per cycle

    except Exception as e:
        log.error(f"[S2 ERROR] {ctx.pair}: {e}", exc_info=True)


# ── S3: FVG + Imbalance (UPGRADED) ───────────────────────────────────────────

def scan_s3_imbal(ctx: PairContext, state: ScanState):
    if ctx.lesson_ctx:
        log.info(f"[S3] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")
    try:
        pair_cd_key = f"{ctx.pair}_S3"
        if state.cd_fvg_e.is_on_cooldown(pair_cd_key):
            return
        for setup in ctx.fvg_imbal_setups:
            base_score = setup["confidence_score"]
            approved, _ = check_weight_gate(state, "S3", base_score, ctx.pair)
            if not approved or base_score < 7.5:  # S3_MIN_SCORE
                continue

            direction = setup["direction"]
            fk = f"{ctx.pair}_{direction}_{setup['fvg']['fvg_low']:.4f}"

            if not _macro_ok(ctx, direction):
                continue
            if not setup["in_zone"]:
                continue
            if state.cd_fvg_e.is_on_cooldown(fk):
                continue

            wick_rej = setup.get("wick_rejection")
            if not wick_rej:
                continue

            t    = setup["trade"]
            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=direction,
                entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                strategy="S3", atr=setup.get("atr", 0.0),
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S3] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            if _already_open(ctx.pair, "S3", direction):
                continue

            conf_parts = (["S2"] if setup.get("has_s2_confluence") else []) + \
                         (["S5"] if setup.get("has_s5_confluence") else [])
            conf_note  = f" [{', '.join(conf_parts)}]" if conf_parts else ""
            log.info(f"✅ [S3] FVG/IMB ENTRY: {ctx.pair} {direction} "
                     f"@ {t['entry']} | Score={base_score} RR={risk.rr_ratio} "
                     f"Size=${risk.position_usdt}{conf_note}")
            inv    = setup["fvg"]["fvg_low"] if direction == "LONG" else setup["fvg"]["fvg_high"]
            reason = (f"{setup['type']} {direction} "
                      f"${setup['fvg']['fvg_low']:.4f}–${setup['fvg']['fvg_high']:.4f}. "
                      f"Reclaim + wick rejection {wick_rej['tf']} confirmed{conf_note}.")
            state.cd_fvg_e.set(fk)
            state.cd_fvg_e.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id="S3", symbol=ctx.pair, direction=direction,
                timeframe=setup["tf_label"], entry_price=t["entry"],
                sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                rr=risk.rr_ratio, score=base_score, reason=reason,
                risk_percent=ctx.params["RISK_PCT"], position_size=risk.position_usdt,
                invalidation_price=inv,
            ))
            log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                       confluence_score=base_score, regime_state=ctx.btc_macro,
                       strategy="S3", position_usdt=risk.position_usdt, rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per cycle

    except Exception as e:
        log.error(f"[S3_IMBAL ERROR] {ctx.pair}: {e}", exc_info=True)


# ── S4+S6 Merged: OB / BB / BOS / CHOCH ─────────────────────────────────────

def scan_s4_ob_bos(ctx: PairContext, state: ScanState):
    """
    Merged S4+S6: OB/BB reactive retests + BOS/CHOCH momentum breaks.
    Single cooldown store (cd_ob_e). Same zone_key → RETEST wins over MOMENTUM.
    Writes to seen_ob so S5 skips owned zones.
    """
    if ctx.lesson_ctx:
        log.info(f"[S4+S6] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")
    try:
        from strategy4_ob_bos import scan_ob_bos
        from strategy1_liquidity import get_fresh_liquidity_zones

        s1_zones = {}
        try:
            s1_zones = get_fresh_liquidity_zones(ctx.pair)
        except Exception:
            pass

        with state._ob_lock:
            seen_ob_snapshot = frozenset(state.seen_ob)

        setups = scan_ob_bos(
            ctx.pair,
            s1_zones=s1_zones,
            fvg_setups=ctx.fvg_imbal_setups,
            eng_setups=ctx.eng_setups,
            seen_ob=seen_ob_snapshot,
        )

        pair_cd_key = f"{ctx.pair}_S4"

        if state.cd_ob_e.is_on_cooldown(pair_cd_key):
            return

        for setup in setups:
            base_score = setup["confidence_score"]
            mode     = setup["entry_mode"]          # "RETEST" or "MOMENTUM"
            strat_id = f"S4-{mode}"                 # matches weight key + logged strategy
            approved, _ = check_weight_gate(state, strat_id, base_score, ctx.pair)
            if not approved:
                continue

            direction = setup["direction"]
            if not _macro_ok(ctx, direction):
                continue
            if not setup["in_zone"]:
                continue
            if state.cd_ob_e.is_on_cooldown(setup["zone_key"]):
                continue

            # MANDATORY wick rejection at 30m/15m/5m
            ob   = setup["ob"]
            wick_rej = get_wick_rejection(ctx.pair, direction, ob["ob_mid"], ob)
            if not wick_rej:
                continue

            t    = setup["trade"]
            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=direction,
                entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                strategy=strat_id, atr=setup.get("atr", 0.0),
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S4] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            if _already_open(ctx.pair, "S4", direction):
                continue

            state.ob_add(setup["zone_key"])   # thread-safe write → S5 skips

            ob_type = setup["type"]
            inv = ob["ob_low"] if direction == "LONG" else ob["ob_high"]

            mss_tag   = " [MSS]"   if setup.get("mss")   else ""
            choch_tag = " [CHOCH]" if setup.get("choch") else ""
            log.info(f"✅ [S4/{mode}] {ob_type} {direction}: {ctx.pair} "
                     f"@ {t['entry']} | Score={base_score} RR={risk.rr_ratio} "
                     f"Size=${risk.position_usdt}{mss_tag}{choch_tag}")

            if mode == "RETEST":
                reason = (f"{ob_type} {direction}: zone "
                          f"${ob['ob_low']:.4f}–${ob['ob_high']:.4f}. "
                          f"Reactive retest at broken structure.")
            else:
                break_lvl = ob.get("break_level", ob["ob_mid"])
                mss_note   = " MSS confirmed."   if setup.get("mss")   else ""
                choch_note = " CHOCH confirmed."  if setup.get("choch") else ""
                reason = (f"{ob_type} {direction}: break of ${break_lvl:.4f}."
                          f"{mss_note}{choch_note}")

            # Set cooldowns before send_alert — prevents re-fire if log_signal throws
            state.cd_ob_e.set(setup["zone_key"])
            state.cd_ob_e.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id=f"S4-{mode}", symbol=ctx.pair, direction=direction,
                timeframe=setup["tf_label"], entry_price=t["entry"],
                sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                rr=risk.rr_ratio, score=base_score, reason=reason,
                risk_percent=ctx.params["RISK_PCT"], position_size=risk.position_usdt,
                invalidation_price=inv,
            ))
            log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                       confluence_score=base_score, regime_state=ctx.btc_macro,
                       strategy=f"S4-{mode}", position_usdt=risk.position_usdt,
                       rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per scan cycle

    except Exception as e:
        log.error(f"[S4_OB_BOS ERROR] {ctx.pair}: {e}", exc_info=True)


# ── S5: Engineered Liquidity Reversal ────────────────────────────────────────

def scan_s5_eng(ctx: PairContext, state: ScanState):
    if ctx.lesson_ctx:
        log.info(f"[S5] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")
    try:
        pair_cd_key = f"{ctx.pair}_S5"
        if state.cd_eng_e.is_on_cooldown(pair_cd_key):
            return
        for setup in ctx.eng_setups:
            base_score = setup["confidence_score"]
            approved, _ = check_weight_gate(state, "S5", base_score, ctx.pair)
            if not approved or base_score < 8.0:
                continue

            direction = setup["direction"]
            if not _macro_ok(ctx, direction):
                continue
            if state.ob_seen(setup["zone_key"]):   # thread-safe read — skip S4-owned zones
                continue
            if not setup["in_zone"]:
                continue
            if state.cd_eng_e.is_on_cooldown(setup["zone_key"]):
                continue

            t    = setup["trade"]
            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=direction,
                entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                strategy="S5", atr=setup.get("atr", 0.0),
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S5] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            if _already_open(ctx.pair, "S5", direction):
                continue

            zone   = setup["zone"]
            inv    = zone["zone_low"] if direction == "LONG" else zone["zone_high"]
            log.info(f"✅ [S5] {setup['type']} {direction}: {ctx.pair} "
                     f"@ {t['entry']} | Score={base_score} RR={risk.rr_ratio} Size=${risk.position_usdt}")
            reason = (f"{setup['type']} {direction} at "
                      f"${zone['zone_low']:.4f}–${zone['zone_high']:.4f}. Compression sweep + reclaim.")
            state.cd_eng_e.set(setup["zone_key"])
            state.cd_eng_e.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id="S5", symbol=ctx.pair, direction=direction,
                timeframe=setup["tf_label"], entry_price=t["entry"],
                sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                rr=risk.rr_ratio, score=base_score, reason=reason,
                risk_percent=ctx.params["RISK_PCT"], position_size=risk.position_usdt,
                invalidation_price=inv,
            ))
            log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                       confluence_score=base_score, regime_state=ctx.btc_macro,
                       strategy="S5", position_usdt=risk.position_usdt, rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per cycle

    except Exception as e:
        log.error(f"[S5_ENG ERROR] {ctx.pair}: {e}", exc_info=True)


# ── S6: EMA Stack Momentum ───────────────────────────────────────────────────

def scan_s6_ema(ctx: PairContext, state: ScanState):
    """
    S6: Multi-TF EMA Stack pullback. Trend-following — zero ICT/zone overlap.
    Uses cd_ob_e cooldown (no new cooldown store needed).
    """
    if ctx.lesson_ctx:
        log.info(f"[S6] {ctx.pair} lessons: {ctx.lesson_ctx[:200]}")
    try:
        from strategy6_ema_stack import scan_ema_stack
        from strategy1_liquidity import get_fresh_liquidity_zones

        s1_zones = {}
        try:
            s1_zones = get_fresh_liquidity_zones(ctx.pair)
        except Exception:
            pass

        setups = scan_ema_stack(ctx.pair, s1_zones=s1_zones)

        pair_cd_key = f"{ctx.pair}_S6"
        if state.cd_ob_e.is_on_cooldown(pair_cd_key):
            return

        for setup in setups:
            base_score = setup["confidence_score"]
            approved, _ = check_weight_gate(state, "S6", base_score, ctx.pair,
                                            setup["direction"])
            if not approved:
                continue

            direction = setup["direction"]
            if not _macro_ok(ctx, direction):
                continue
            if state.cd_ob_e.is_on_cooldown(setup["zone_key"]):
                continue

            # MANDATORY wick rejection at 30m/15m/5m near EMA20
            wick_rej = get_wick_rejection(ctx.pair, direction, setup["ema_4h"])
            if not wick_rej:
                continue

            t    = setup["trade"]
            risk = state.risk_mgr.evaluate(TradeSetup(
                pair=ctx.pair, direction=direction,
                entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                strategy="S6", atr=setup.get("atr", 0.0),
                risk_pct=ctx.params["RISK_PCT"],
                min_rr=ctx.params["MIN_RR_RATIO"],
                atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
            ))
            if not risk.approved:
                log.warning(f"⛔ [S6] RISK REJECTED: {ctx.pair} — {risk.reason}")
                continue

            if _already_open(ctx.pair, "S6", direction):
                continue

            pb   = setup["pullback"]
            bn   = setup["bounce"]
            log.info(
                f"✅ [S6] EMA STACK {direction}: {ctx.pair} "
                f"@ {t['entry']} | EMA4H={setup['ema_4h']} "
                f"| Score={base_score} RR={risk.rr_ratio} Size=${risk.position_usdt} "
                f"| Touch={pb['diff_pct']}% Body={bn['body_ratio']} Vol={bn['vol_ratio']}x"
            )
            reason = (
                f"EMA Stack {direction}: 1W/1D/4H aligned. "
                f"Pullback {pb['diff_pct']}% from EMA20 "
                f"({setup['ema_4h']:.4f}). "
                f"1H bounce body={bn['body_ratio']:.0%} vol={bn['vol_ratio']:.1f}x. "
                f"{'Precise touch. ' if pb['precise_touch'] else ''}"
                f"{'At structure. ' if setup['at_structure'] else ''}"
                f"Wick rejection {wick_rej['tf']} confirmed."
            )
            inv = setup["zone_low"] if direction == "LONG" else setup["zone_high"]

            state.cd_ob_e.set(setup["zone_key"])
            state.cd_ob_e.set(pair_cd_key)
            state.signal_handler.send_alert(Signal(
                strategy_id="S6", symbol=ctx.pair, direction=direction,
                timeframe="4H", entry_price=t["entry"],
                sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                rr=risk.rr_ratio, score=base_score, reason=reason,
                risk_percent=ctx.params["RISK_PCT"], position_size=risk.position_usdt,
                invalidation_price=inv,
            ))
            log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                       confluence_score=base_score, regime_state=ctx.btc_macro,
                       strategy="S6", position_usdt=risk.position_usdt,
                       rr=risk.rr_ratio)
            state.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
            state.rate_mon.track(ctx.pair)
            break  # one signal per pair per cycle

    except Exception as e:
        log.error(f"[S6_EMA ERROR] {ctx.pair}: {e}", exc_info=True)


# ── Dispatch ──────────────────────────────────────────────────────────────────

def run_all_strategies(ctx: PairContext, state: ScanState, current_price: float):
    """
    Run all active strategies for one pair.
    S1 (Liquidity) → S1-CHART → S2 (Wick) → S3 (FVG) → S4 (OB+BOS merged) → S5 (Engineered) → S6 (EMA Stack)
    S4 writes seen_ob; S5 reads it. Order matters.
    """
    scan_s1(ctx, state, current_price)
    scan_s1_chart(ctx, state, current_price)
    scan_s2(ctx, state)
    scan_s3_imbal(ctx, state)
    scan_s4_ob_bos(ctx, state)   # merged S4+S6 (ICT)
    scan_s5_eng(ctx, state)
    scan_s6_ema(ctx, state)      # trend-following (EMA momentum)
