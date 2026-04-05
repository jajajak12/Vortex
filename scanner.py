import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import (
    CRYPTO_PAIRS, SCAN_INTERVAL_SECONDS,
    ENABLE_MACRO_FILTER, SIGNAL_RATE_MIN, STRAT3_MIN_SCORE,
    get_pair_params,
)
from strategy1_liquidity import (
    get_fresh_liquidity_zones, get_candles, get_btc_macro_regime,
    is_touching_zone, check_rejection_long, check_rejection_short,
    calculate_trade,
)
from strategy2_wick import scan_wick_setups
from strategy3_fvg import scan_fvg_setups
from telegram_bot import alert_touch, alert_entry, alert_result, alert_stats, alert_info
from wick_alerts import alert_wick_detected, alert_wick_entry
from fvg_alerts import alert_fvg_detected, alert_fvg_entry
from trade_tracker import log_signal, update_trades_for_pair, get_stats, trim_old_trades
from risk_manager import RiskManager, TradeSetup


# ── Per-pair scan context ─────────────────────────────────────

@dataclass
class PairContext:
    """State yang dikompute sekali per pair, dipakai semua strategi."""
    pair:        str
    btc_macro:   str
    wick_setups: list = field(default_factory=list)
    params:      dict = field(default_factory=dict)  # pair-specific overrides


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
            print(f"[⚠️  SIGNAL RATE] <{self.min_per_day} signal hari ini: {low}")


# ── Main scanner ──────────────────────────────────────────────

class VortexScanner:

    def __init__(self):
        # Cooldown store per alert type
        self.cd_touch  = CooldownStore()
        self.cd_entry  = CooldownStore()
        self.cd_wick_d = CooldownStore()
        self.cd_wick_e = CooldownStore()
        self.cd_fvg_d  = CooldownStore()
        self.cd_fvg_e  = CooldownStore()

        self.rate_mon        = SignalRateMonitor()
        self.risk_mgr        = RiskManager()
        self.last_stats_date: Optional[str] = None
        # Fix 5: BTC macro cache (EMA200 1W berubah sangat lambat)
        self._macro_cache:     Optional[tuple[str, float]] = None  # (regime, timestamp)
        self._macro_cache_ttl: int = 3600  # refresh tiap 1 jam

    # ── Per-pair context ──────────────────────────────────────

    def _build_context(self, pair: str, btc_macro: str) -> PairContext:
        try:
            wick_setups = scan_wick_setups(pair)
        except Exception as e:
            print(f"[WICK INIT ERROR] {pair}: {e}")
            wick_setups = []
        return PairContext(
            pair=pair,
            btc_macro=btc_macro,
            wick_setups=wick_setups,
            params=get_pair_params(pair),
        )

    # ── Trade monitoring (dipanggil unconditional di scan_once) ──

    def _monitor_trades(self, pair: str, current_price: float):
        """Cek TP/SL hit untuk semua open trade pair ini (semua strategi)."""
        for ct in update_trades_for_pair(pair, current_price):
            res = "✅ WIN" if ct["result"] == "WIN" else "❌ LOSS"
            strat = ct.get("strategy", "?")
            print(f"[{_ts()}] {res} [{strat}]: {pair} {ct['direction']} | "
                  f"Close={ct['close_price']}")
            alert_result(ct)
            self.risk_mgr.on_trade_closed()

    # ── Strategy 1: Fresh Liquidity Grab ─────────────────────

    def _scan_s1(self, ctx: PairContext, current_price: float):
        try:
            zones    = get_fresh_liquidity_zones(ctx.pair)
            htf_bias = zones["htf_bias"]

            if ENABLE_MACRO_FILTER and htf_bias != ctx.btc_macro:
                return
            valid_dir = htf_bias

            all_zones = [z for z in zones["LONG"] + zones["SHORT"]
                         if z["type"] == valid_dir]
            if not all_zones:
                return

            for zone in all_zones:
                ckey = f"{ctx.pair}_{zone['type']}_{zone['pivot']:.4f}"

                if not is_touching_zone(current_price, zone):
                    continue

                if not self.cd_touch.is_on_cooldown(ckey):
                    print(f"[{_ts()}] ⚠️  [S1] TOUCH: {ctx.pair} @ {current_price:.4f} "
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
                    print(f"[{_ts()}] ⛔ [S1] RISK REJECTED: {ctx.pair} — {risk.reason}")
                    continue

                vol = " 🔥 Volume spike!" if rejection.get("volume_spike") else ""
                print(f"[{_ts()}] ✅ [S1] ENTRY: {ctx.pair} {valid_dir} | "
                      f"E={trade['entry']} SL={trade['sl']} TP={trade['tp']} "
                      f"RR={risk.rr_ratio} Size=${risk.position_usdt}{vol}")
                alert_entry(ctx.pair, valid_dir,
                            trade["entry"], trade["sl"], trade["tp"],
                            trade["rr"], position_usdt=risk.position_usdt)
                log_signal(ctx.pair, valid_dir, trade["entry"],
                           trade["sl"], trade["tp"],
                           regime_state=ctx.btc_macro, strategy="S1",
                           position_usdt=risk.position_usdt)
                self.risk_mgr.on_trade_opened()
                self.rate_mon.track(ctx.pair)
                self.cd_entry.set(ckey)

        except Exception as e:
            print(f"[S1 ERROR] {ctx.pair}: {e}")
            traceback.print_exc()

    # ── Strategy 2: Wick Fill ─────────────────────────────────

    def _scan_s2(self, ctx: PairContext):
        try:
            for setup in ctx.wick_setups:
                wk = f"{ctx.pair}_{setup['tf']}_{setup['wick']['wick_low']:.4f}"

                if not self.cd_wick_d.is_on_cooldown(wk):
                    print(f"[{_ts()}] 🕯️  [S2] WICK: {ctx.pair} {setup['tf_label']} "
                          f"| Low={setup['wick']['wick_low']} | {setup['confluence_label']}")
                    alert_wick_detected(setup)
                    self.cd_wick_d.set(wk)

                if ENABLE_MACRO_FILTER and ctx.btc_macro == "BEAR":
                    continue

                if not setup["in_entry_zone"]:
                    continue
                if self.cd_wick_e.is_on_cooldown(wk):
                    continue

                candles_5m = get_candles(ctx.pair, "5m", limit=50)
                wick_zone  = {
                    "low":   setup["wick"]["wick_low"],
                    "high":  setup["wick"]["wick_50pct"],
                    "pivot": setup["wick"]["wick_low"],
                }
                rejection = check_rejection_long(
                    candles_5m, wick_zone,
                    vol_spike_required=ctx.params["REQUIRE_VOLUME_SPIKE"]
                )

                if rejection and rejection["confirmed"]:
                    t = setup["trade"]
                    risk = self.risk_mgr.evaluate(TradeSetup(
                        pair=ctx.pair, direction="LONG",
                        entry=t["entry"], sl=t["sl"], tp=t["tp2"],
                        strategy="S2",
                        risk_pct=ctx.params["RISK_PCT"],
                        min_rr=ctx.params["MIN_RR_RATIO"],
                        atr_sl_mult=ctx.params["ATR_SL_MIN_MULT"],
                    ))
                    if not risk.approved:
                        print(f"[{_ts()}] ⛔ [S2] RISK REJECTED: {ctx.pair} — {risk.reason}")
                        continue

                    print(f"[{_ts()}] ✅ [S2] WICK ENTRY: {ctx.pair} {setup['tf_label']} "
                          f"@ {setup['current_price']} Size=${risk.position_usdt}")
                    alert_wick_entry(setup, position_usdt=risk.position_usdt)
                    log_signal(ctx.pair, "LONG", t["entry"], t["sl"], t["tp2"],
                               regime_state=ctx.btc_macro, strategy="S2",
                               position_usdt=risk.position_usdt)
                    self.risk_mgr.on_trade_opened()
                    self.rate_mon.track(ctx.pair)
                    self.cd_wick_e.set(wk)

        except Exception as e:
            print(f"[S2 ERROR] {ctx.pair}: {e}")
            traceback.print_exc()

    # ── Strategy 3: FVG Reclaim ───────────────────────────────

    def _scan_s3(self, ctx: PairContext):
        try:
            setups = scan_fvg_setups(ctx.pair, wick_setups=ctx.wick_setups)

            for setup in setups:
                if setup["confluence_score"] < STRAT3_MIN_SCORE:
                    continue

                direction = setup["direction"]
                fk = f"{ctx.pair}_{direction}_{setup['fvg']['fvg_low']:.4f}"

                if ENABLE_MACRO_FILTER:
                    if direction == "LONG"  and ctx.btc_macro == "BEAR":
                        continue
                    if direction == "SHORT" and ctx.btc_macro == "BULL":
                        continue

                if not self.cd_fvg_d.is_on_cooldown(fk):
                    print(f"[{_ts()}] 🔷 [S3] FVG: {ctx.pair} {setup['tf_label']} "
                          f"{direction} | Zone {setup['fvg']['fvg_low']}-"
                          f"{setup['fvg']['fvg_high']} | Score={setup['confluence_score']}")
                    alert_fvg_detected(setup)
                    self.cd_fvg_d.set(fk)

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
                    print(f"[{_ts()}] ⛔ [S3] RISK REJECTED: {ctx.pair} — {risk.reason}")
                    continue

                print(f"[{_ts()}] ✅ [S3] FVG ENTRY: {ctx.pair} {direction} "
                      f"@ {t['entry']} | Score={setup['confluence_score']} "
                      f"RR={risk.rr_ratio} Size=${risk.position_usdt}")
                alert_fvg_entry(setup, position_usdt=risk.position_usdt)
                log_signal(ctx.pair, direction, t["entry"], t["sl"], t["tp2"],
                           confluence_score=setup["confluence_score"],
                           regime_state=ctx.btc_macro, strategy="S3",
                           position_usdt=risk.position_usdt)
                self.risk_mgr.on_trade_opened()
                self.rate_mon.track(ctx.pair)
                self.cd_fvg_e.set(fk)

        except Exception as e:
            print(f"[S3 ERROR] {ctx.pair}: {e}")
            traceback.print_exc()

    # ── Scan cycle ────────────────────────────────────────────

    def scan_once(self, btc_macro: str):
        # Sync open trade count dari trade_tracker sebelum scan
        self.risk_mgr.sync_open_count(get_stats()["open"])

        for pair in CRYPTO_PAIRS:
            try:
                # Ambil harga saat ini sekali — dipakai monitor + S1
                candles_30m   = get_candles(pair, "30m", limit=5)
                current_price = candles_30m[-1]["close"]
            except Exception as e:
                print(f"[PRICE ERROR] {pair}: {e}")
                time.sleep(0.3)
                continue

            # Monitor TP/SL hits unconditional — tidak bergantung macro/strategy
            self._monitor_trades(pair, current_price)

            ctx = self._build_context(pair, btc_macro)
            self._scan_s1(ctx, current_price)
            self._scan_s2(ctx)
            self._scan_s3(ctx)
            time.sleep(0.3)

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        print("=" * 50)
        print(f"🤖 VORTEX — 3 Strategies | {len(CRYPTO_PAIRS)} pairs")
        print(f"⏱️  Interval: {SCAN_INTERVAL_SECONDS}s")
        print("=" * 50)

        alert_info(
            f"🤖 Vortex aktif — 3 strategi\n"
            f"Strat 1: Liquidity Grab\n"
            f"Strat 2: Wick Fill\n"
            f"Strat 3: FVG Reclaim\n"
            f"Pairs: {len(CRYPTO_PAIRS)} | Interval: {SCAN_INTERVAL_SECONDS}s"
        )

        while True:
            scan_start = time.time()
            print(f"\n[{_ts()}] Scanning {len(CRYPTO_PAIRS)} pairs...")

            # Fix 5: cache macro — EMA200 1W berubah sangat lambat
            try:
                now = time.time()
                if (not ENABLE_MACRO_FILTER):
                    btc_macro = "BULL"
                elif (self._macro_cache is None or
                      now - self._macro_cache[1] >= self._macro_cache_ttl):
                    btc_macro = get_btc_macro_regime()
                    self._macro_cache = (btc_macro, now)
                    print(f"[{_ts()}] 🌍 Macro (refreshed): "
                          f"{'🟢 BULL' if btc_macro == 'BULL' else '🔴 BEAR'}")
                else:
                    btc_macro = self._macro_cache[0]
            except Exception as e:
                print(f"[MACRO ERROR] {e} — defaulting to BULL")
                btc_macro = "BULL"

            self.scan_once(btc_macro)

            # Daily: winrate report + cleanup (sekali per hari, saat hari berganti)
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self.last_stats_date:
                self.last_stats_date = today
                stats    = get_stats()
                risk_st  = self.risk_mgr.status()
                print(f"[STATS] WR={stats['winrate']}% "
                      f"W={stats['wins']} L={stats['losses']} "
                      f"Open={stats['open']} | "
                      f"DailyRisk={risk_st['daily_risk_used']}%")
                for s, d in stats.get("by_strategy", {}).items():
                    print(f"  [{s}] {d['wins']}W {d['losses']}L WR={d['winrate']}%")
                alert_stats(stats)
                self.rate_mon.check(CRYPTO_PAIRS)
                trim_old_trades(keep_closed=500)  # Fix 6

            elapsed    = time.time() - scan_start
            sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
            print(f"[{_ts()}] Scan selesai ({elapsed:.1f}s). "
                  f"Next scan in {sleep_time:.0f}s")
            time.sleep(sleep_time)


# ── Helpers ───────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

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
