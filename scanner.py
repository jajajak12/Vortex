import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from vortex_logger import get_logger
from config import (
    CRYPTO_PAIRS, SCAN_INTERVAL_SECONDS,
    ENABLE_MACRO_FILTER, SIGNAL_RATE_MIN, STRAT3_MIN_SCORE,
    OWN_MACRO_PAIRS, SESSION_FILTER_PAIRS,
    get_pair_params,
)
from strategy1_liquidity import (
    get_fresh_liquidity_zones, get_candles, get_btc_macro_regime,
    is_touching_zone, check_rejection_long, check_rejection_short,
    calculate_trade,
)
from strategy1_chartpattern import scan_chartpatterns
from strategy2_wick import scan_wick_setups
from strategy3_fvg import scan_fvg_setups
from strategy3_fvg_imbalance import scan_fvg_imbalance
from strategy4_orderblock import scan_order_blocks
from strategy5_engineered import scan_engineered
from strategy6_bos_mss import scan_bos_mss
from telegram_bot import alert_touch, alert_result, alert_stats, alert_info
from core.signal_handler import Signal, SignalHandler
from trade_tracker import log_signal, update_trades_for_pair, get_stats, trim_old_trades
from risk_manager import RiskManager, TradeSetup
from weights import apply_weight_gate, get_all_weights
from lessons_injector import get_strategy_context

log = get_logger(__name__)


# ── Per-pair scan context ─────────────────────────────────────

@dataclass
class PairContext:
    """State yang dikompute sekali per pair, dipakai semua strategi."""
    pair:             str
    btc_macro:        str
    wick_setups:      list = field(default_factory=list)
    fvg_imbal_setups: list = field(default_factory=list)  # S3 upgraded
    ob_setups:        list = field(default_factory=list)  # S4 OB
    chart_setups:     list = field(default_factory=list)  # S1 Chart Patterns
    eng_setups:       list = field(default_factory=list)  # S5
    params:           dict = field(default_factory=dict)  # pair-specific overrides


# ── Alert cooldown store ──────────────────────────────────────

class CooldownStore:
    """Cooldown per zona key. Auto-expire setelah COOLDOWN detik."""
    COOLDOWN = 4 * 60 * 60  # 4 jam

    def __init__(self):
        self._store: dict[str, float] = {}

    def is_on_cooldown(self, key: str) -> bool:
        if key not in self._store:
            return False
        if time.time() - self._store[key] >= self.COOLDOWN:
            del self._store[key]
            return False
        return True

    def set(self, key: str):
        self._store[key] = time.time()


# ── Signal rate monitor ───────────────────────────────────────

class SignalRateMonitor:
    """Tracking jumlah signal per pair per hari. Warning jika terlalu sedikit."""

    def __init__(self, min_per_day: int = SIGNAL_RATE_MIN):
        self.min_per_day = min_per_day
        self._counts: dict[str, dict[str, int]] = {}  # date → {pair: count}

    def track(self, pair: str):
        today = datetime.now().strftime("%Y-%m-%d")
        self._counts.setdefault(today, {})
        self._counts[today][pair] = self._counts[today].get(pair, 0) + 1

    def check(self, pairs: list[str]):
        today  = datetime.now().strftime("%Y-%m-%d")
        counts = self._counts.get(today, {})
        low    = [p for p in pairs if counts.get(p, 0) < self.min_per_day]
        if low:
            log.warning(f"[SIGNAL RATE] <{self.min_per_day} signal hari ini: {low}")


# ── Main scanner ──────────────────────────────────────────────

class VortexScanner:

    def __init__(self):
        # Cooldown store per alert type
        self.cd_touch  = CooldownStore()
        self.cd_entry  = CooldownStore()
        self.cd_wick_e = CooldownStore()
        self.cd_fvg_e  = CooldownStore()
        self.cd_ob_e   = CooldownStore()   # S4 entry cooldown
        self.cd_eng_e  = CooldownStore()   # S5 entry cooldown
        self.cd_bos_e  = CooldownStore()   # S6 entry cooldown

        # Permanent seen sets
        self._seen_wick:     set[str] = set()
        self._seen_fvg:      set[str] = set()
        self._seen_ob:       set[str] = set()   # S4 writes, S6 reads → overlap prevention

        self.signal_handler  = SignalHandler()
        self.rate_mon        = SignalRateMonitor()
        self.risk_mgr        = RiskManager()
        self.last_stats_date: Optional[str] = None
        # BTC macro cache (EMA200 1W berubah sangat lambat)
        self._macro_cache:     Optional[tuple[str, float]] = None  # (regime, timestamp)
        self._macro_cache_ttl: int = 3600  # refresh tiap 1 jam

        # Suppress alert blast saat pertama launch
        self._warmup()

    # ── Startup warmup ────────────────────────────────────────

    def _warmup(self):
        """
        Pre-populate cooldown store untuk wick & FVG yang sudah ada.
        Mencegah alert blast di semua pair saat scanner pertama kali jalan.
        Hanya cooldown 'detected' yang di-suppress — 'entry' tetap fresh.
        """
        from strategy3_fvg import scan_fvg_setups
        log.info("[WARMUP] Pre-scanning existing setups (suppressing launch alerts)...")
        for pair in CRYPTO_PAIRS:
            try:
                # Wick — masukkan ke permanent seen set
                for setup in scan_wick_setups(pair):
                    direction = setup.get("direction", "LONG")
                    w   = setup["wick"]
                    ref = w.get("wick_low") or w.get("wick_high", 0)
                    wk  = f"{pair}_{direction}_{setup['tf']}_{ref:.4f}"
                    self._seen_wick.add(wk)
                # FVG — masukkan ke permanent seen set
                for setup in scan_fvg_setups(pair):
                    direction = setup["direction"]
                    fk = f"{pair}_{direction}_{setup['fvg']['fvg_low']:.4f}"
                    self._seen_fvg.add(fk)
                # OB — masukkan ke _seen_ob (S4 overlap prevention)
                try:
                    from strategy4_orderblock import scan_order_blocks
                    for setup in scan_order_blocks(pair):
                        self._seen_ob.add(setup["zone_key"])
                except Exception:
                    pass
            except Exception as e:
                log.error(f"[WARMUP] {pair}: {e}")
        log.info(f"[WARMUP] Done — {len(CRYPTO_PAIRS)} pairs seeded, only new setups will alert.")

    # ── Session filter ────────────────────────────────────────

    @staticmethod
    def _is_trading_session(pair: str) -> bool:
        """
        Return False jika pair sedang di luar jam trading.
        Khusus gold: tutup Jumat 21:00 UTC s/d Minggu 22:00 UTC (weekend gap).
        """
        if pair not in SESSION_FILTER_PAIRS:
            return True
        from datetime import timezone
        now = datetime.now(timezone.utc)
        wd  = now.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
        h   = now.hour
        if wd == 5:               # Sabtu — tutup seharian
            return False
        if wd == 4 and h >= 21:   # Jumat setelah 21:00 UTC
            return False
        if wd == 6 and h < 22:    # Minggu sebelum 22:00 UTC
            return False
        return True

    # ── Per-pair context ──────────────────────────────────────

    def _build_context(self, pair: str, btc_macro: str) -> PairContext:
        try:
            wick_setups = scan_wick_setups(pair)
        except Exception as e:
            log.error(f"[WICK INIT ERROR] {pair}: {e}")
            wick_setups = []
        try:
            fvg_imbal_setups = scan_fvg_imbalance(pair, wick_setups=wick_setups, engineered_setups=[])
        except Exception as e:
            log.error(f"[FVG_IMBAL INIT ERROR] {pair}: {e}")
            fvg_imbal_setups = []
        try:
            ob_setups = scan_order_blocks(pair)
        except Exception as e:
            log.error(f"[OB INIT ERROR] {pair}: {e}")
            ob_setups = []
        try:
            chart_setups = scan_chartpatterns(pair)
        except Exception as e:
            log.error(f"[CHART_PATTERN INIT ERROR] {pair}: {e}")
            chart_setups = []
        try:
            eng_setups = scan_engineered(pair)
        except Exception as e:
            log.error(f"[ENG INIT ERROR] {pair}: {e}")
            eng_setups = []
        return PairContext(
            pair=pair,
            btc_macro=btc_macro,
            wick_setups=wick_setups,
            fvg_imbal_setups=fvg_imbal_setups,
            ob_setups=ob_setups,
            chart_setups=chart_setups,
            eng_setups=eng_setups,
            params=get_pair_params(pair),
        )

    # ── Trade monitoring (dipanggil unconditional di scan_once) ──

    def _check_weight_gate(self, strategy_id: str, base_score: float) -> tuple[bool, float]:
        """Darwinian gate: return (approved, score_adjusted). Reject if score_final < 6.0."""
        approved, final = apply_weight_gate(strategy_id, base_score)
        if not approved:
            log.info(f"  ⛔ [WEIGHT GATE] {strategy_id} score={final:.2f} < 6.0 — REJECTED")
        return approved, final

    def _monitor_trades(self, pair: str, current_price: float):
        """Cek TP/SL hit untuk semua open trade pair ini (semua strategi)."""
        for ct in update_trades_for_pair(pair, current_price):
            res   = "✅ WIN" if ct["result"] == "WIN" else "❌ LOSS"
            strat = ct.get("strategy", "?")
            log.info(f"{res} [{strat}]: {pair} {ct['direction']} | Close={ct['close_price']}")

            # Darwinian Weighting — update bobot setelah trade close
            from weights import update_weight
            new_w = update_weight(strat, ct["result"])
            log.info(f"  Weight [{strat}]: {new_w:.3f}")

            alert_result(ct)
            self.risk_mgr.on_trade_closed()

    # ── Strategy 1: Fresh Liquidity Grab ─────────────────────

    def _scan_s1(self, ctx: PairContext, current_price: float):
        try:
            zones    = get_fresh_liquidity_zones(ctx.pair)
            htf_bias = zones["htf_bias"]

            # OWN_MACRO_PAIRS (mis. XAUUSDT) pakai htf_bias pair sendiri, bukan BTC macro
            if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS \
                    and htf_bias != ctx.btc_macro:
                return
            valid_dir = htf_bias

            all_zones = [z for z in zones["LONG"] + zones["SHORT"]
                         if z["type"] == valid_dir]
            if not all_zones:
                return

            for zone in all_zones:
                ckey = f"{ctx.pair}_{zone['type']}_{zone['pivot']:.4f}"

                if not is_touching_zone(current_price, zone,
                                        threshold_pct=ctx.params["TOUCH_THRESHOLD_PCT"]):
                    continue

                if not self.cd_touch.is_on_cooldown(ckey):
                    log.info(f"⚠️  [S1] TOUCH: {ctx.pair} @ {current_price:.4f} "
                             f"| {zone['type']} {zone['pivot']:.4f}")
                    alert_touch(ctx.pair, current_price,
                                zone["low"], zone["high"], zone["type"])
                    self.cd_touch.set(ckey)

                if self.cd_entry.is_on_cooldown(ckey):
                    continue

                candles_5m = get_candles(ctx.pair, "5m", limit=50)
                req_vol    = ctx.params["REQUIRE_VOLUME_SPIKE"]
                rejection  = (check_rejection_long(candles_5m, zone,
                                                   vol_spike_required=req_vol)
                              if valid_dir == "LONG"
                              else check_rejection_short(candles_5m, zone,
                                                         vol_spike_required=req_vol))

                if not (rejection and rejection["confirmed"]):
                    continue

                # TP = next liquidity di sisi berlawanan (swing high untuk LONG, low untuk SHORT)
                tp_zones = zones["SHORT"] if valid_dir == "LONG" else zones["LONG"]
                tp_target = _find_tp_target(rejection["entry_price"], tp_zones, valid_dir)
                trade     = calculate_trade(valid_dir, rejection["entry_price"],
                                            zone, tp_target)

                risk = self.risk_mgr.evaluate(TradeSetup(
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
                vol_str = " 🔥 Volume spike!" if vol else ""
                log.info(f"✅ [S1] ENTRY: {ctx.pair} {valid_dir} | "
                         f"E={trade['entry']} SL={trade['sl']} TP={trade['tp']} "
                         f"RR={risk.rr_ratio} Size=${risk.position_usdt}{vol_str}")

                s1_score = 6.0
                if vol:
                    s1_score += 1.5
                if risk.rr_ratio >= 2.5:
                    s1_score += 1.0
                elif risk.rr_ratio >= 2.0:
                    s1_score += 0.5

                vol_note = " dengan volume spike" if vol else ""
                reason   = (
                    f"Fresh {valid_dir} liquidity zone "
                    f"${zone['low']:.4f}–${zone['high']:.4f}. "
                    f"False breakout 5m{vol_note} confirmed, "
                    f"close kembali di dalam zona."
                )
                self.signal_handler.send_alert(Signal(
                    strategy_id        = "S1",
                    symbol             = ctx.pair,
                    direction          = valid_dir,
                    timeframe          = "30m",
                    entry_price        = trade["entry"],
                    sl_price           = trade["sl"],
                    tp1_price          = trade["tp"],
                    tp2_price          = None,
                    rr                 = risk.rr_ratio,
                    score              = s1_score,
                    reason             = reason,
                    risk_percent       = ctx.params["RISK_PCT"],
                    position_size      = risk.position_usdt,
                    invalidation_price = zone["low"] if valid_dir == "LONG" else zone["high"],
                ))
                log_signal(ctx.pair, valid_dir, trade["entry"],
                           trade["sl"], trade["tp"],
                           regime_state=ctx.btc_macro, strategy="S1",
                           position_usdt=risk.position_usdt,
                           rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_entry.set(ckey)

        except Exception as e:
            log.error(f"[S1 ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 1B: Chart Patterns ───────────────────────────────

    def _scan_s1_chart(self, ctx: PairContext, current_price: float):
        """Pure classic chart patterns — Rising/Falling Wedge, H&S, Inv H&S, Bull/Bear Flag."""
        try:
            if not ctx.chart_setups:
                return

            # Macro alignment (optional — same as other strategies)
            for setup in ctx.chart_setups:
                direction = setup["direction"]

                # Filter direction by macro
                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG" and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                # Cooldown per pattern+zone
                zone_mid = setup["zone_mid"]
                ckey = f"{ctx.pair}_{direction}_{setup['pattern']}_{zone_mid:.4f}"
                if self.cd_entry.is_on_cooldown(ckey):
                    continue

                # Wick rejection gate (MANDATORY — 30m close vs zone boundary)
                zone_high = setup["zone_high"]
                zone_low  = setup["zone_low"]
                ref_price = zone_high if direction == "LONG" else zone_low
                if not self._get_wick_rejection(ctx.pair, direction, ref_price):
                    continue

                # Entry price: midpoint of broken channel
                entry = zone_mid
                # SL: 1 ATR outside zone (same direction as pattern)
                atr = ctx.params.get("ATR_SL_MIN_MULT", 1.0) * ctx.params.get("atr_mult", 0.015) * entry
                sl  = (zone_low - 1.5 * atr) if direction == "LONG" else (zone_high + 1.5 * atr)
                # TP1: 1:2.0 / TP2: 1:3.0
                dist = abs(entry - sl)
                tp1  = entry + dist * 2.0 if direction == "LONG" else entry - dist * 2.0
                tp2  = entry + dist * 3.0 if direction == "LONG" else entry - dist * 3.0

                # Score: base 5.5, min 8.0 for signal
                score = 5.5
                if setup.get("vol_confirmed"):
                    score += 1.5
                score += min(setup.get("atr_pct", 0) / 10.0, 1.5)  # ATR width bonus
                if score < 8.0:
                    continue

                # Risk evaluation
                risk = self.risk_mgr.evaluate(TradeSetup(
                    pair=ctx.pair, direction=direction,
                    entry=entry, sl=sl, tp=tp1,
                    strategy="S1-CHART",
                    risk_pct=ctx.params["RISK_PCT"],
                    min_rr=ctx.params["MIN_RR_RATIO"],
                    atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
                ))
                if not risk.approved:
                    log.warning(f"⛔ [S1-CHART] RISK REJECTED: {ctx.pair} — {risk.reason}")
                    continue

                log.info(f"✅ [S1-CHART] ENTRY {direction}: {ctx.pair} {setup['pattern']} "
                         f"E={entry:.4f} SL={sl:.4f} TP1={tp1:.4f} TP2={tp2:.4f} "
                         f"RR={risk.rr_ratio:.1f} Score={score:.1f}")

                reason = (
                    f"{setup['pattern']} confirmed on 4H. "
                    f"Breakout on 1H with {setup['vol_ratio']:.1f}x volume. "
                    f"Entry at broken channel {direction}."
                )
                inv = zone_low if direction == "LONG" else zone_high
                self.signal_handler.send_alert(Signal(
                    strategy_id        = "S1-CHART",
                    symbol              = ctx.pair,
                    direction           = direction,
                    timeframe           = "30m",
                    entry_price         = entry,
                    sl_price            = sl,
                    tp1_price           = tp1,
                    tp2_price           = tp2,
                    rr                  = risk.rr_ratio,
                    score               = round(score, 1),
                    reason              = reason,
                    risk_percent        = ctx.params["RISK_PCT"],
                    position_size       = risk.position_usdt,
                    invalidation_price  = inv,
                ))
                log_signal(ctx.pair, direction, entry, sl, tp1,
                           regime_state=ctx.btc_macro, strategy="S1-CHART",
                           position_usdt=risk.position_usdt,
                           rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_entry.set(ckey)

        except Exception as e:
            log.error(f"[S1-CHART ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 2: Wick Fill ─────────────────────────────────

    def _scan_s2(self, ctx: PairContext):
        try:
            for setup in ctx.wick_setups:
                direction = setup.get("direction", "LONG")
                w   = setup["wick"]
                ref = w.get("wick_low") or w.get("wick_high", 0)
                wk  = f"{ctx.pair}_{direction}_{setup['tf']}_{ref:.4f}"

                if wk not in self._seen_wick:
                    log.info(f"🕯️  [S2] WICK {direction}: {ctx.pair} "
                             f"{setup['tf_label']} | Ref={ref} | {setup['confluence_label']}")
                    self._seen_wick.add(wk)

                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                if not setup["in_entry_zone"]:
                    continue
                if self.cd_wick_e.is_on_cooldown(wk):
                    continue

                candles_5m = get_candles(ctx.pair, "5m", limit=50)
                req_vol    = ctx.params["REQUIRE_VOLUME_SPIKE"]

                if direction == "LONG":
                    wick_zone = {"low": w["wick_low"], "high": w["wick_50pct"],
                                 "pivot": w["wick_low"]}
                    rejection = check_rejection_long(candles_5m, wick_zone,
                                                     vol_spike_required=req_vol)
                else:
                    wick_zone = {"low": w["wick_50pct"], "high": w["wick_high"],
                                 "pivot": w["wick_high"]}
                    rejection = check_rejection_short(candles_5m, wick_zone,
                                                      vol_spike_required=req_vol)

                if not (rejection and rejection["confirmed"]):
                    continue

                t    = setup["trade"]
                risk = self.risk_mgr.evaluate(TradeSetup(
                    pair=ctx.pair, direction=direction,
                    entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                    strategy="S2",
                    risk_pct=ctx.params["RISK_PCT"],
                    min_rr=ctx.params["MIN_RR_RATIO"],
                    atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
                ))
                if not risk.approved:
                    log.warning(f"⛔ [S2] RISK REJECTED: {ctx.pair} — {risk.reason}")
                    continue

                log.info(f"✅ [S2] WICK ENTRY {direction}: {ctx.pair} "
                         f"{setup['tf_label']} @ {setup['current_price']} "
                         f"Size=${risk.position_usdt}")

                ema_note  = " dekat EMA50" if setup["ema_info"]["has_confluence"] else ""
                wk_ratio  = setup["wick"]["wick_body_ratio"]
                reason_s2 = (
                    f"{setup['tf_label']} wick {wk_ratio}x body{ema_note}. "
                    f"Price di fill zone, rejection 5m confirmed."
                )
                inv_s2 = (
                    w["wick_low"]  if direction == "LONG"
                    else w["wick_high"]
                )
                self.signal_handler.send_alert(Signal(
                    strategy_id        = "S2",
                    symbol             = ctx.pair,
                    direction          = direction,
                    timeframe          = setup["tf_label"],
                    entry_price        = t["entry"],
                    sl_price           = t["sl"],
                    tp1_price          = t["tp1"],
                    tp2_price          = t["tp2"],
                    rr                 = risk.rr_ratio,
                    score              = setup["confluence_score"] * 2.0,
                    reason             = reason_s2,
                    risk_percent       = ctx.params["RISK_PCT"],
                    position_size      = risk.position_usdt,
                    invalidation_price = inv_s2,
                ))
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           regime_state=ctx.btc_macro, strategy="S2",
                           position_usdt=risk.position_usdt,
                           rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_wick_e.set(wk)

        except Exception as e:
            log.error(f"[S2 ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 3: FVG Reclaim ───────────────────────────────

    def _scan_s3(self, ctx: PairContext):
        try:
            setups = scan_fvg_setups(ctx.pair, wick_setups=ctx.wick_setups)

            for setup in setups:
                if setup["confluence_score"] < STRAT3_MIN_SCORE:
                    continue

                direction = setup["direction"]
                fk = f"{ctx.pair}_{direction}_{setup['fvg']['fvg_low']:.4f}"

                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                if fk not in self._seen_fvg:
                    log.info(f"🔷 [S3] FVG: {ctx.pair} {setup['tf_label']} "
                             f"{direction} | Zone {setup['fvg']['fvg_low']}-"
                             f"{setup['fvg']['fvg_high']} | Score={setup['confluence_score']}")
                    self._seen_fvg.add(fk)

                if not setup["in_fvg_zone"]:
                    continue
                if self.cd_fvg_e.is_on_cooldown(fk):
                    continue

                fvg_zone = {
                    "low":   setup["fvg"]["fvg_low"],
                    "high":  setup["fvg"]["fvg_high"],
                    "pivot": setup["fvg"]["fvg_mid"],
                }
                candles_5m = get_candles(ctx.pair, "5m", limit=50)
                req_vol    = ctx.params["REQUIRE_VOLUME_SPIKE"]
                rejection  = (check_rejection_long(candles_5m, fvg_zone,
                                                   vol_spike_required=req_vol)
                              if direction == "LONG"
                              else check_rejection_short(candles_5m, fvg_zone,
                                                         vol_spike_required=req_vol))

                if not (rejection and rejection["confirmed"]):
                    continue

                t    = setup["trade"]
                risk = self.risk_mgr.evaluate(TradeSetup(
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

                log.info(f"✅ [S3] FVG ENTRY: {ctx.pair} {direction} "
                         f"@ {t['entry']} | Score={setup['confluence_score']} "
                         f"RR={risk.rr_ratio} Size=${risk.position_usdt}")

                top_notes = [
                    n.replace("✅ ", "")
                    for n in setup["confluence_notes"] if n.startswith("✅")
                ][:2]
                sweep_ref = (setup["sweep"].get("sweep_low")
                             or setup["sweep"].get("sweep_high"))
                reason_s3 = (
                    f"Liquidity sweep → "
                    f"{'Bullish' if direction == 'LONG' else 'Bearish'} FVG "
                    f"${setup['fvg']['fvg_low']:.4f}–${setup['fvg']['fvg_high']:.4f}. "
                    + (" ".join(top_notes) if top_notes else "Rejection 5m confirmed.")
                )
                inv_s3 = (
                    setup["fvg"]["fvg_low"]  if direction == "LONG"
                    else setup["fvg"]["fvg_high"]
                )
                self.signal_handler.send_alert(Signal(
                    strategy_id        = "S3",
                    symbol             = ctx.pair,
                    direction          = direction,
                    timeframe          = setup["tf_label"],
                    entry_price        = t["entry"],
                    sl_price           = t["sl"],
                    tp1_price          = t["tp1"],
                    tp2_price          = t["tp2"],
                    rr                 = risk.rr_ratio,
                    score              = float(setup["confluence_score"]),
                    reason             = reason_s3,
                    risk_percent       = ctx.params["RISK_PCT"],
                    position_size      = risk.position_usdt,
                    invalidation_price = inv_s3,
                ))
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           confluence_score=setup["confluence_score"],
                           regime_state=ctx.btc_macro, strategy="S3",
                           position_usdt=risk.position_usdt,
                           rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_fvg_e.set(fk)

        except Exception as e:
            log.error(f"[S3 ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 3: FVG + Imbalance (UPGRADED) ─────────────────────

    def _scan_s3_imbal(self, ctx: PairContext):
        """S3 upgraded: FVG + Imbalance with tighter thresholds for more entries."""
        # Lesson injection
        strat_ctx = get_strategy_context("S3", ctx.pair)
        if strat_ctx:
            log.debug(f"[S3] {ctx.pair} context: {strat_ctx[:120]}...")

        try:
            for setup in ctx.fvg_imbal_setups:
                base_score = setup["confidence_score"]

                # Darwinian weight gate
                approved, final_score = self._check_weight_gate("S3", base_score)
                if not approved:
                    continue

                if base_score < 7.0:
                    continue
                direction = setup["direction"]
                fk = f"{ctx.pair}_{direction}_{setup['fvg']['fvg_low']:.4f}"

                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                if not setup["in_zone"]:
                    continue
                if self.cd_fvg_e.is_on_cooldown(fk):
                    continue

                # Wick rejection at 30m — MANDATORY
                wick_rej = setup.get("wick_rejection")
                if not wick_rej:
                    continue

                t    = setup["trade"]
                risk = self.risk_mgr.evaluate(TradeSetup(
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

                conf_parts = []
                if setup.get("has_s2_confluence"): conf_parts.append("S2")
                if setup.get("has_s5_confluence"): conf_parts.append("S5")
                conf_note = f" [{', '.join(conf_parts)}]" if conf_parts else ""

                log.info(f"✅ [S3] FVG/IMB ENTRY: {ctx.pair} {direction} "
                         f"@ {t['entry']} | Score={setup['confidence_score']} "
                         f"RR={risk.rr_ratio} Size=${risk.position_usdt}{conf_note}")

                reason = (
                    f"{setup['type']} {direction} "
                    f"${setup['fvg']['fvg_low']:.4f}–${setup['fvg']['fvg_high']:.4f}. "
                    f"Reclaim + wick rejection {wick_rej['tf']} confirmed{conf_note}."
                )
                inv = setup["fvg"]["fvg_low"] if direction == "LONG" else setup["fvg"]["fvg_high"]
                self.signal_handler.send_alert(Signal(
                    strategy_id="S3", symbol=ctx.pair, direction=direction,
                    timeframe=setup["tf_label"], entry_price=t["entry"],
                    sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                    rr=risk.rr_ratio, score=setup["confidence_score"],
                    reason=reason, risk_percent=ctx.params["RISK_PCT"],
                    position_size=risk.position_usdt, invalidation_price=inv,
                ))
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           confluence_score=setup["confidence_score"],
                           regime_state=ctx.btc_macro, strategy="S3",
                           position_usdt=risk.position_usdt, rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_fvg_e.set(fk)

        except Exception as e:
            log.error(f"[S3_IMBAL ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 4: Order Block + Breaker Block ─────────────────────

    def _scan_s4_ob(self, ctx: PairContext):
        """S4: Order Block + Breaker Block reactive retest setups.
        S4 fires FIRST on overlapping zones — writes to _seen_ob for S6/S5.
        """
        strat_ctx = get_strategy_context("S4", ctx.pair)
        if strat_ctx:
            log.debug(f"[S4] {ctx.pair} context: {strat_ctx[:120]}...")

        try:
            s1_zones = {}
            try:
                from strategy1_liquidity import get_fresh_liquidity_zones
                s1_zones = get_fresh_liquidity_zones(ctx.pair)
            except Exception:
                pass

            for setup in ctx.ob_setups:
                base_score = setup["confidence_score"]
                approved, final_score = self._check_weight_gate("S4", base_score)
                if not approved:
                    continue

                if base_score < 8.0:
                    continue
                direction = setup["direction"]

                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                if not setup["in_zone"]:
                    continue
                if self.cd_ob_e.is_on_cooldown(setup["zone_key"]):
                    continue

                # MANDATORY wick rejection at 30m
                wick_rej = self._get_wick_rejection(
                    ctx.pair, setup["direction"],
                    setup["ob"]["ob_mid"], setup.get("ob", {})
                )
                if not wick_rej:
                    continue

                t    = setup["trade"]
                risk = self.risk_mgr.evaluate(TradeSetup(
                    pair=ctx.pair, direction=direction,
                    entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                    strategy="S4-OB", atr=setup.get("atr", 0.0),
                    risk_pct=ctx.params["RISK_PCT"],
                    min_rr=ctx.params["MIN_RR_RATIO"],
                    atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
                ))
                if not risk.approved:
                    log.warning(f"⛔ [S4-OB] RISK REJECTED: {ctx.pair} — {risk.reason}")
                    continue

                # Write to _seen_ob so S6/S5 skip this zone
                self._seen_ob.add(setup["zone_key"])

                log.info(f"✅ [S4-OB] {setup['type']} {direction}: {ctx.pair} "
                         f"@ {t['entry']} | Score={setup['confidence_score']} "
                         f"RR={risk.rr_ratio} Size=${risk.position_usdt}")

                ob = setup["ob"]
                inv = ob["ob_low"] if direction == "LONG" else ob["ob_high"]
                reason = (
                    f"{setup['type']} {direction}: zone "
                    f"${ob['ob_low']:.4f}–${ob['ob_high']:.4f}. "
                    f"Reactive retest at broken structure."
                )
                self.signal_handler.send_alert(Signal(
                    strategy_id="S4-OB", symbol=ctx.pair, direction=direction,
                    timeframe=setup["tf_label"], entry_price=t["entry"],
                    sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                    rr=risk.rr_ratio, score=setup["confidence_score"],
                    reason=reason, risk_percent=ctx.params["RISK_PCT"],
                    position_size=risk.position_usdt, invalidation_price=inv,
                ))
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           confluence_score=setup["confidence_score"],
                           regime_state=ctx.btc_macro, strategy="S4-OB",
                           position_usdt=risk.position_usdt, rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_ob_e.set(setup["zone_key"])

        except Exception as e:
            log.error(f"[S4_OB ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 5: Engineered Liquidity Reversal ───────────────────

    def _scan_s5_eng(self, ctx: PairContext):
        """S5: Engineered Liquidity Reversal — compression + sweep setups.
        Reads _seen_ob to skip zones S4 already owns.
        """
        strat_ctx = get_strategy_context("S5", ctx.pair)
        if strat_ctx:
            log.debug(f"[S5] {ctx.pair} context: {strat_ctx[:120]}...")

        try:
            for setup in ctx.eng_setups:
                base_score = setup["confidence_score"]
                approved, final_score = self._check_weight_gate("S5", base_score)
                if not approved:
                    continue

                if base_score < 7.5:
                    continue
                direction = setup["direction"]

                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                # Skip if S4 already fired for this zone
                if setup["zone_key"] in self._seen_ob:
                    continue

                if not setup["in_zone"]:
                    continue
                if self.cd_eng_e.is_on_cooldown(setup["zone_key"]):
                    continue

                t    = setup["trade"]
                risk = self.risk_mgr.evaluate(TradeSetup(
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

                log.info(f"✅ [S5] {setup['type']} {direction}: {ctx.pair} "
                         f"@ {t['entry']} | Score={setup['confidence_score']} "
                         f"RR={risk.rr_ratio} Size=${risk.position_usdt}")

                zone = setup["zone"]
                inv = zone["zone_low"] if direction == "LONG" else zone["zone_high"]
                reason = (
                    f"{setup['type']} {direction} at "
                    f"${zone['zone_low']:.4f}–${zone['zone_high']:.4f}. "
                    f"Compression sweep + reclaim."
                )
                self.signal_handler.send_alert(Signal(
                    strategy_id="S5", symbol=ctx.pair, direction=direction,
                    timeframe=setup["tf_label"], entry_price=t["entry"],
                    sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                    rr=risk.rr_ratio, score=setup["confidence_score"],
                    reason=reason, risk_percent=ctx.params["RISK_PCT"],
                    position_size=risk.position_usdt, invalidation_price=inv,
                ))
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           confluence_score=setup["confidence_score"],
                           regime_state=ctx.btc_macro, strategy="S5",
                           position_usdt=risk.position_usdt, rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_eng_e.set(setup["zone_key"])

        except Exception as e:
            log.error(f"[S5_ENG ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Strategy 6: BOS + MSS / CHOCH ────────────────────────────────

    def _scan_s6_bos(self, ctx: PairContext):
        """S6: BOS + MSS / CHOCH — momentum break + hold setups.
        S6 fires AFTER S4 on overlapping zones (reads _seen_ob).
        Does NOT do reactive retests — those belong to S4.
        """
        strat_ctx = get_strategy_context("S6", ctx.pair)
        if strat_ctx:
            log.debug(f"[S6] {ctx.pair} context: {strat_ctx[:120]}...")

        try:
            s1_zones = {}
            try:
                from strategy1_liquidity import get_fresh_liquidity_zones
                s1_zones = get_fresh_liquidity_zones(ctx.pair)
            except Exception:
                pass

            for setup in scan_bos_mss(
                ctx.pair,
                s1_zones=s1_zones,
                fvg_setups=ctx.fvg_imbal_setups,
                seen_ob=self._seen_ob,
            ):
                base_score = setup["confidence_score"]
                approved, final_score = self._check_weight_gate("S6", base_score)
                if not approved:
                    continue

                if base_score < 8.0:
                    continue
                direction = setup["direction"]

                if ENABLE_MACRO_FILTER and ctx.pair not in OWN_MACRO_PAIRS:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                if not setup["in_zone"]:
                    continue
                if self.cd_bos_e.is_on_cooldown(setup["zone_key"]):
                    continue

                # MANDATORY wick rejection at 30m
                zone_mid = setup["zone"]["zone_mid"]
                wick_rej = self._get_wick_rejection(
                    ctx.pair, setup["direction"], zone_mid, setup.get("zone", {})
                )
                if not wick_rej:
                    continue

                t    = setup["trade"]
                risk = self.risk_mgr.evaluate(TradeSetup(
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

                log.info(f"✅ [S6] {setup['type']} {direction}: {ctx.pair} "
                         f"@ {t['entry']} | Score={setup['confidence_score']} "
                         f"RR={risk.rr_ratio} Size=${risk.position_usdt} "
                         f"{'[MSS]' if setup.get('mss') else ''}"
                         f"{'[CHOCH]' if setup.get('choch') else ''}"
                         f"{'[WICK OK]' if wick_rej else ''}")

                zone = setup["zone"]
                inv = zone["zone_low"] if direction == "LONG" else zone["zone_high"]
                mss_note = " MSS confirmed." if setup.get("mss") else ""
                choch_note = " CHOCH confirmed." if setup.get("choch") else ""
                reason = (
                    f"{setup['type']} {direction}: "
                    f"break of ${zone.get('break_level', zone['zone_mid']):.4f}. "
                    f"{mss_note}{choch_note}"
                )
                self.signal_handler.send_alert(Signal(
                    strategy_id="S6", symbol=ctx.pair, direction=direction,
                    timeframe=setup["tf_label"], entry_price=t["entry"],
                    sl_price=t["sl"], tp1_price=t["tp1"], tp2_price=t["tp2"],
                    rr=risk.rr_ratio, score=setup["confidence_score"],
                    reason=reason, risk_percent=ctx.params["RISK_PCT"],
                    position_size=risk.position_usdt, invalidation_price=inv,
                ))
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           confluence_score=setup["confidence_score"],
                           regime_state=ctx.btc_macro, strategy="S6",
                           position_usdt=risk.position_usdt, rr=risk.rr_ratio)
                self.risk_mgr.on_trade_opened(risk_pct=ctx.params["RISK_PCT"])
                self.rate_mon.track(ctx.pair)
                self.cd_bos_e.set(setup["zone_key"])

        except Exception as e:
            log.error(f"[S6_BOS ERROR] {ctx.pair}: {e}", exc_info=True)

    # ── Wick rejection helper (shared by S3, S4, S6) ──────────────

    def _get_wick_rejection(
        self, pair: str, direction: str, zone_price: float,
        zone_data: dict | None = None,
    ) -> dict | None:
        """
        Check for wick rejection at zone on 30m/15m/5m candles.
        Used by S3_imbal, S4, S6 as MANDATORY entry confirmation.
        """
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
                    if wick:
                        diff_pct = abs(wick["wick_low"] - zone_price) / zone_price
                        if diff_pct < 0.005:
                            return {"tf": tf, "wick": wick}
                else:
                    wick = is_long_upside_wick(c)
                    if wick:
                        diff_pct = abs(wick["wick_high"] - zone_price) / zone_price
                        if diff_pct < 0.005:
                            return {"tf": tf, "wick": wick}
        return None

    # ── Scan cycle ────────────────────────────────────────────

    def scan_once(self, btc_macro: str):
        # Sync open trade count dari trade_tracker sebelum scan
        self.risk_mgr.sync_open_count(get_stats()["open"])

        for pair in CRYPTO_PAIRS:
            if not self._is_trading_session(pair):
                log.info(f"⏸️  {pair} — di luar jam trading (weekend gap), skip")
                continue

            try:
                # Ambil harga saat ini sekali — dipakai monitor + S1
                candles_30m   = get_candles(pair, "30m", limit=5)
                current_price = candles_30m[-1]["close"]
            except Exception as e:
                log.error(f"[PRICE ERROR] {pair}: {e}")
                time.sleep(0.3)
                continue

            # Monitor TP/SL hits unconditional — tidak bergantung macro/strategy
            self._monitor_trades(pair, current_price)

            ctx = self._build_context(pair, btc_macro)
            self._scan_s1(ctx, current_price)    # S1: Liquidity Grab (existing)
            self._scan_s1_chart(ctx, current_price)  # S1: Chart Patterns (new)
            self._scan_s2(ctx)
            # NOTE: original S3 disabled — S3_imbal (FVG+Imbalance) replaces it
            # self._scan_s3(ctx)        # legacy — commented out
            self._scan_s3_imbal(ctx)  # upgraded S3 (FVG + Imbalance)
            self._scan_s4_ob(ctx)     # S4 (Order Block + Breaker Block)
            self._scan_s5_eng(ctx)    # S5 (Engineered Liquidity)
            self._scan_s6_bos(ctx)    # S6 (BOS + MSS / CHOCH)
            time.sleep(0.3)

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        log.info("=" * 50)
        log.info(f"🤖 VORTEX — 6 Strategies | {len(CRYPTO_PAIRS)} pairs")
        log.info(f"⏱️  Interval: {SCAN_INTERVAL_SECONDS}s")
        log.info(f"[1H AGGRESSIVE MODE] EXPERIMENT ACTIVE — TF_EXPERIMENT_MODE = 1H_AGGRESSIVE")
        log.info(f"    Detect/Confirm: 1H | Entry: 15m | Thresholds loosened for 2-week trial")
        log.info("=" * 50)

        alert_info(
            f"🤖 Vortex AKTIF — 1H_AGGRESSIVE MODE (eksperimen 2 minggu)\n"
            f"S1: Liquidity Grab + Chart Patterns\n"
            f"S2: Wick Fill\n"
            f"S3: FVG + Imbalance\n"
            f"S4: Order Block + Breaker Block\n"
            f"S5: Engineered Liquidity\n"
            f"S6: BOS + MSS / CHOCH\n"
            f"TF: Detect/Confirm=1H | Entry=15m\n"
            f"Pairs: {len(CRYPTO_PAIRS)} | Interval: {SCAN_INTERVAL_SECONDS}s"
        )

        while True:
            scan_start = time.time()
            log.info("[1H AGGRESSIVE MODE] Scanning...")

            # Fix 5: cache macro — EMA200 1W berubah sangat lambat
            try:
                now = time.time()
                if (not ENABLE_MACRO_FILTER):
                    btc_macro = "BULL"
                elif (self._macro_cache is None or
                      now - self._macro_cache[1] >= self._macro_cache_ttl):
                    btc_macro = get_btc_macro_regime()
                    self._macro_cache = (btc_macro, now)
                    log.info(f"🌍 Macro (refreshed): "
                             f"{'🟢 BULL' if btc_macro == 'BULL' else '🔴 BEAR'}")
                else:
                    btc_macro = self._macro_cache[0]
            except Exception as e:
                log.error(f"[MACRO ERROR] {e} — defaulting to BULL")
                btc_macro = "BULL"

            self.scan_once(btc_macro)

            # Daily: winrate report + cleanup (sekali per hari, saat hari berganti)
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self.last_stats_date:
                self.last_stats_date = today
                stats    = get_stats()
                risk_st  = self.risk_mgr.status()
                log.info(f"[STATS] WR={stats['winrate']}% "
                         f"W={stats['wins']} L={stats['losses']} "
                         f"Open={stats['open']} | "
                         f"DailyRisk={risk_st['daily_risk_used']}%")
                for s, d in stats.get("by_strategy", {}).items():
                    log.info(f"  [{s}] {d['wins']}W {d['losses']}L WR={d['winrate']}%")
                alert_stats(stats)
                self.rate_mon.check(CRYPTO_PAIRS)
                trim_old_trades(keep_closed=500)

            elapsed    = time.time() - scan_start
            sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
            log.info(f"Scan selesai ({elapsed:.1f}s). Next scan in {sleep_time:.0f}s")
            time.sleep(sleep_time)


# ── Helpers ───────────────────────────────────────────────────

def _find_tp_target(entry: float, opposite_zones: list, direction: str) -> float | None:
    """
    Cari TP target di next liquidity berdasarkan struktur pasar.

    LONG  → nearest swing HIGH di atas entry (dari zones["SHORT"])
    SHORT → nearest swing LOW  di bawah entry (dari zones["LONG"])

    Return None jika tidak ada kandidat — calculate_trade() akan fallback ke TRADE_RR.
    """
    if direction == "LONG":
        candidates = [z["pivot"] for z in opposite_zones if z["pivot"] > entry]
        return min(candidates) if candidates else None   # nearest resistance
    else:
        candidates = [z["pivot"] for z in opposite_zones if z["pivot"] < entry]
        return max(candidates) if candidates else None   # nearest support


if __name__ == "__main__":
    VortexScanner().run()
