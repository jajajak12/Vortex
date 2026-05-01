import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from vortex_logger import get_logger
from config import (
    CRYPTO_PAIRS, SCAN_INTERVAL_SECONDS,
    ENABLE_MACRO_FILTER, OWN_MACRO_PAIRS, SESSION_FILTER_PAIRS,
    get_pair_params,
)
from scanner_utils import PairContext, ScanState, SignalRateMonitor
from strategy1_liquidity import (
    get_fresh_liquidity_zones, get_candles, get_btc_macro_regime,
    is_touching_zone, check_rejection_long, check_rejection_short,
    calculate_trade,
)
from strategy1_chartpattern import scan_chart_patterns as scan_chartpatterns
from strategy2_wick import scan_wick_setups
from strategy3_fvg_imbalance import scan_fvg_imbalance
from strategy4_ob_bos import scan_ob_bos       # merged S4+S6
from strategy5_engineered import scan_engineered
from telegram_bot import alert_result, alert_stats, alert_info
from core.signal_handler import SignalHandler
from trade_tracker import log_signal, update_trades_for_pair, get_stats, trim_old_trades
from risk_manager import RiskManager
from weights import apply_weight_gate, get_all_weights, update_weight
from lessons_injector import get_strategy_context, inject_lessons_to_context
from strategy_runner import run_all_strategies

log = get_logger(__name__)

# ── Main scanner ──────────────────────────────────────────────

class VortexScanner:

    def __init__(self):
        self.risk_mgr       = RiskManager()
        self.signal_handler = SignalHandler()
        self.rate_mon       = SignalRateMonitor()

        # Single shared ScanState — all cooldown stores, seen sets, shared services
        self.state = ScanState(
            signal_handler = self.signal_handler,
            risk_mgr       = self.risk_mgr,
            rate_mon       = self.rate_mon,
        )

        self.last_stats_date: Optional[str] = None
        self._macro_cache:     Optional[tuple[str, float]] = None
        self._macro_cache_ttl: int = 1200  # refresh every 20 min

        self._warmup()
        # Reset open count to trades.json reality (warmup may seed incorrect count)
        self.risk_mgr.sync_open_count(get_stats()["open"])

    # ── Startup warmup ────────────────────────────────────────

    def _warmup(self):
        """
        Pre-populate seen sets for wick, FVG, OB setups already present.
        Prevents alert blast when scanner first starts.
        """
        log.info("[WARMUP] Pre-scanning existing setups (suppressing launch alerts)...")
        for pair in CRYPTO_PAIRS:
            try:
                for setup in scan_wick_setups(pair):
                    direction = setup.get("direction", "LONG")
                    w   = setup["wick"]
                    ref = w.get("wick_low") or w.get("wick_high", 0)
                    wk  = f"{pair}_{direction}_{setup['tf']}_{ref:.4f}"
                    self.state.seen_wick.add(wk)
                for setup in scan_fvg_imbalance(pair, wick_setups=[], engineered_setups=[]):
                    direction = setup["direction"]
                    fk = f"{pair}_{direction}_{setup['fvg']['fvg_low']:.4f}"
                    self.state.seen_fvg.add(fk)
                try:
                    for setup in scan_ob_bos(pair):
                        self.state.ob_add(setup["zone_key"])
                except Exception:
                    pass
            except Exception as e:
                log.error(f"[WARMUP] {pair}: {e}")
        log.info(f"[WARMUP] Done — {len(CRYPTO_PAIRS)} pairs seeded.")

    # ── Session filter ────────────────────────────────────────

    @staticmethod
    def _is_trading_session(pair: str) -> bool:
        if pair not in SESSION_FILTER_PAIRS:
            return True
        from datetime import timezone
        now = datetime.now(timezone.utc)
        wd  = now.weekday()
        h   = now.hour
        if wd == 5:
            return False
        if wd == 4 and h >= 21:
            return False
        if wd == 6 and h < 22:
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
            chart_setups = scan_chartpatterns(pair)
        except Exception as e:
            log.error(f"[CHART_PATTERN INIT ERROR] {pair}: {e}")
            chart_setups = []
        try:
            eng_setups = scan_engineered(pair)
        except Exception as e:
            log.error(f"[ENG INIT ERROR] {pair}: {e}")
            eng_setups = []
        lesson_ctx = inject_lessons_to_context("", pair)
        return PairContext(
            pair             = pair,
            btc_macro        = btc_macro,
            lesson_ctx       = lesson_ctx,
            wick_setups      = wick_setups,
            fvg_imbal_setups = fvg_imbal_setups,
            chart_setups     = chart_setups,
            eng_setups       = eng_setups,
            params           = get_pair_params(pair),
        )

    # ── Trade monitoring ──────────────────────────────────────

    def _monitor_trades(self, pair: str, current_price: float,
                        candle_high: float = 0.0, candle_low: float = 0.0):
        """Check TP/SL hits. Detect false signals (LOSS < 5 candles)."""
        for ct in update_trades_for_pair(pair, current_price, candle_high, candle_low):
            strat           = ct.get("strategy", "?")
            candles_held    = ct.get("candles_to_resolve") or 999
            is_false_signal = ct["result"] == "LOSS" and candles_held < 5

            if ct["result"] == "WIN":
                weight_result = "WIN"
                res_label     = "✅ WIN"
            elif is_false_signal:
                weight_result = "FALSE_SIGNAL"
                res_label     = "❌ FALSE_SIGNAL"
            else:
                weight_result = "LOSS"
                res_label     = "❌ LOSS"

            log.info(f"{res_label} [{strat}]: {pair} {ct['direction']} "
                     f"| Close={ct['close_price']} | Candles={candles_held}")
            new_w = update_weight(strat, weight_result)
            log.info(f"  Weight [{strat}]: {new_w:.3f}")
            alert_result(ct)
            self.risk_mgr.on_trade_closed()

    # ── Parallel candle prefetch ──────────────────────────────

    def _prefetch_candles(self, pairs: list[str]):
        """Parallel I/O prefetch — warms 55s TTL cache before scan."""
        TFS = ["5m", "15m", "30m", "1h", "4h", "1d", "1w"]

        def _fetch(args: tuple):
            pair, tf = args
            try:
                get_candles(pair, tf, limit=100)
            except Exception:
                pass

        tasks = [(p, tf) for p in pairs for tf in TFS]
        with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
            list(ex.map(_fetch, tasks))

    # ── Per-pair scan (runs in thread) ────────────────────────

    def _scan_pair(self, pair: str, btc_macro: str):
        """Full scan for one pair — monitor + all 6 strategies. Thread-safe."""
        try:
            candles_30m    = get_candles(pair, "30m", limit=100)
            last           = candles_30m[-1]
            current_price  = last["close"]
            candle_high    = last["high"]
            candle_low     = last["low"]
        except Exception as e:
            log.error(f"[PRICE ERROR] {pair}: {e}")
            return

        self._monitor_trades(pair, current_price, candle_high, candle_low)
        ctx = self._build_context(pair, btc_macro)
        run_all_strategies(ctx, self.state, current_price)

    # ── Scan cycle ────────────────────────────────────────────

    def scan_once(self, btc_macro: str):
        self.risk_mgr.sync_open_count(get_stats()["open"])
        # Reset seen_ob each cycle — cooldowns (cd_ob_e) already prevent re-signals
        with self.state._ob_lock:
            self.state.seen_ob.clear()
        active_pairs = [p for p in CRYPTO_PAIRS if self._is_trading_session(p)]

        # Warm candle cache in parallel (I/O bound)
        self._prefetch_candles(active_pairs)

        # Scan all pairs in parallel (P3.2 — full parallel per-pair)
        with ThreadPoolExecutor(max_workers=min(len(active_pairs), 6)) as ex:
            futures = {ex.submit(self._scan_pair, pair, btc_macro): pair
                       for pair in active_pairs}
            for fut in as_completed(futures):
                pair = futures[fut]
                try:
                    fut.result()
                except Exception as e:
                    log.error(f"[SCAN ERROR] {pair}: {e}", exc_info=True)

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

            try:
                now = time.time()
                if not ENABLE_MACRO_FILTER:
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

            today = datetime.now().strftime("%Y-%m-%d")
            if today != self.last_stats_date:
                self.last_stats_date = today
                stats   = get_stats()
                risk_st = self.risk_mgr.status()
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


if __name__ == "__main__":
    VortexScanner().run()
