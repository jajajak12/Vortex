import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Optional

from vortex_logger import get_logger
from config import (
    CRYPTO_PAIRS, SCAN_INTERVAL_SECONDS,
    ENABLE_MACRO_FILTER, OWN_MACRO_PAIRS, SESSION_FILTER_PAIRS,
    VALIDATED_TRADING_PAIRS, RESEARCH_WATCHLIST_PAIRS,
    get_pair_params,
)
from scanner_utils import (
    PairContext,
    ScanState,
    SignalRateMonitor,
    ScanDiagnostics,
    StrategyDecision,
)
from strategy_registry import (
    STRATEGY_ORDER,
    get_auto_open_strategy_ids,
    get_strategy_definition,
    get_watch_only_strategy_ids,
)
from strategy_metadata import get_strategy_meta
from strategy_utils import get_candles, get_btc_macro_regime
from telegram_bot import alert_result, alert_stats, alert_info, alert_time_exit
from core.signal_handler import SignalHandler
from trade_tracker import log_signal, update_trades_for_pair, get_stats, trim_old_trades
from risk_manager import RiskManager
from weights import get_all_weights, update_weight
from lessons_injector import get_strategy_context, inject_lessons_to_context
from strategy_runner import run_all_strategies

log = get_logger(__name__)
LESSONS_FILE = Path("/home/prospera/vortex/lessons.json")
WEIGHTS_FILE = Path("/home/prospera/vortex/weights.json")
DAILY_ANALYSIS_FILE = Path("/home/prospera/vortex/daily_analysis.py")


def _fmt_pairs(pairs: list[str]) -> str:
    return ", ".join(pairs) if pairs else "-"

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
                for strategy_id in STRATEGY_ORDER:
                    definition = get_strategy_definition(strategy_id)
                    scanner = getattr(import_module(definition.scanner_module), definition.scanner_func)
                    for setup in scanner(pair):
                        self.state.ob_add(setup["zone_key"])
                        self.state.cd_ob_e.set(setup["zone_key"])
                        self.state.cd_ob_e.set(f"{pair}_{strategy_id}")
            except Exception as e:
                log.error(f"[WARMUP] {pair}: {e}")
        log.info(
            "[WARMUP] Done — "
            f"{len(CRYPTO_PAIRS)} validated trading pairs seeded "
            f"({_fmt_pairs(CRYPTO_PAIRS)})."
        )

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
        lesson_ctx = inject_lessons_to_context("", pair)
        return PairContext(
            pair             = pair,
            btc_macro        = btc_macro,
            lesson_ctx       = lesson_ctx,
            params           = get_pair_params(pair),
        )

    @staticmethod
    def _fmt_mtime(path: Path) -> str:
        if not path.exists():
            return "missing"
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")

    def _log_learning_state(self):
        weights = get_all_weights()
        lesson_update_mode = "batch_external" if DAILY_ANALYSIS_FILE.exists() else "unknown"
        log.info(
            "[LEARN][STATE] "
            f"weights={json.dumps(weights, separators=(',', ':'), sort_keys=True)} "
            f'weights_mtime="{self._fmt_mtime(WEIGHTS_FILE)}" '
            f"lessons_exists={LESSONS_FILE.exists()} "
            f'lessons_mtime="{self._fmt_mtime(LESSONS_FILE)}" '
            f'lesson_update_mode="{lesson_update_mode}"'
        )

    # ── Trade monitoring ──────────────────────────────────────

    def _monitor_trades(self, pair: str, current_price: float,
                        candle_high: float = 0.0, candle_low: float = 0.0):
        """Check TP/SL hits and max-hold timeout. Detect false signals (LOSS < 5 candles)."""
        for ct in update_trades_for_pair(pair, current_price, candle_high, candle_low):
            strat           = ct.get("strategy", "?")
            meta            = get_strategy_meta(strat)
            candles_held    = ct.get("candles_to_resolve") or 999
            is_time_exit    = ct.get("close_reason") == "TIME_EXIT"
            is_false_signal = (ct["result"] == "LOSS"
                               and candles_held < 5
                               and not is_time_exit)

            if is_time_exit:
                weight_result = ct["result"]  # WIN or LOSS based on price at timeout
                res_label     = f"⏱️ TIME_EXIT ({ct['result']})"
            elif ct["result"] == "WIN":
                weight_result = "WIN"
                res_label     = "✅ WIN"
            elif is_false_signal:
                weight_result = "FALSE_SIGNAL"
                res_label     = "❌ FALSE_SIGNAL"
            else:
                weight_result = "LOSS"
                res_label     = "❌ LOSS"

            log.info(f"{res_label} [{strat}]: {pair} {ct['direction']} "
                     f"| Close={ct['close_price']} | Candles={candles_held}"
                     + (f" | Held={ct.get('held_hours')}h" if is_time_exit else ""))
            old_w = get_all_weights().get(strat, 1.0)
            new_w = update_weight(strat, weight_result)
            delta = new_w - old_w
            log.info(
                "[LEARN][WEIGHT_UPDATE] "
                f"trade_id={ct.get('id')} "
                f"pair={pair} "
                f"strategy={strat} "
                f'name="{meta.strategy_name}" '
                f"outcome={weight_result} "
                f"old_weight={old_w:.2f} "
                f"new_weight={new_w:.2f} "
                f"delta={delta:+.2f} "
                "source=closed_trade_monitor "
                f"close_time={ct.get('close_time')}"
            )
            log.info(f"  Weight [{strat}]: {new_w:.3f}")
            if is_time_exit:
                alert_time_exit(ct)
            else:
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
        """Full scan for one pair — monitor + full strategy lineup. Thread-safe."""
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
        self.state.diagnostics = ScanDiagnostics()
        self.risk_mgr.sync_open_count(get_stats()["open"])
        # Reset seen_ob each cycle — cooldowns (cd_ob_e) already prevent re-signals
        with self.state._ob_lock:
            self.state.seen_ob.clear()
        active_pairs = [p for p in CRYPTO_PAIRS if self._is_trading_session(p)]
        inactive_pairs = [p for p in CRYPTO_PAIRS if p not in active_pairs]
        research_pairs = RESEARCH_WATCHLIST_PAIRS

        # Warm candle cache in parallel (I/O bound)
        self._prefetch_candles(active_pairs)

        log.info(
            "[UNIVERSE] Trading(validated): "
            f"{len(active_pairs)}/{len(CRYPTO_PAIRS)} active this cycle | "
            f"{_fmt_pairs(active_pairs)}"
        )
        log.info(
            "[UNIVERSE] Research/watchlist: "
            f"{len(research_pairs)} configured | {_fmt_pairs(research_pairs)}"
        )

        if inactive_pairs:
            for pair in inactive_pairs:
                for strategy_id in STRATEGY_ORDER:
                    meta = get_strategy_meta(strategy_id)
                    self.state.diagnostics.record(StrategyDecision(
                        pair=pair,
                        strategy_id=meta.strategy_id,
                        strategy_name=meta.strategy_name,
                        legacy_label=meta.legacy_label,
                        evaluated=False,
                        raw_signal=False,
                        blocked_reason="session_blocked",
                    ))
            log.info(
                "[UNIVERSE] Session-blocked pairs this cycle: "
                f"{_fmt_pairs(inactive_pairs)}"
            )

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
        self.state.diagnostics.log_summary()

    # ── Main loop ─────────────────────────────────────────────

    def run(self):
        auto_open_ids = get_auto_open_strategy_ids()
        watch_only_ids = get_watch_only_strategy_ids()
        auto_open_lines = []
        for strategy_id in auto_open_ids:
            definition = get_strategy_definition(strategy_id)
            auto_open_lines.append(
                f"{strategy_id}: {definition.display_name} (RR 1:{definition.planned_rr:g}, {definition.timeframe})"
            )
        watch_only_lines = []
        for strategy_id in watch_only_ids:
            definition = get_strategy_definition(strategy_id)
            watch_only_lines.append(f"{strategy_id}: {definition.display_name}")
        startup_message = (
            "🤖 Vortex AKTIF — final dry-run lineup\n"
            + "\n".join(auto_open_lines)
            + "\n"
            + f"Watch-only: {', '.join(watch_only_ids)}\n"
            + f"Validated pairs: {len(VALIDATED_TRADING_PAIRS)} | "
            + f"Watchlist: {len(RESEARCH_WATCHLIST_PAIRS)} | "
            + f"Interval: {SCAN_INTERVAL_SECONDS}s"
        )

        log.info("=" * 50)
        log.info(f"🤖 VORTEX — {len(STRATEGY_ORDER)} registered strategies | {len(CRYPTO_PAIRS)} validated trading pairs")
        log.info(f"⏱️  Interval: {SCAN_INTERVAL_SECONDS}s")
        log.info("[DRY-RUN LINEUP] Final active/watch-only runtime loaded")
        log.info(f"[UNIVERSE] Trading(validated): {_fmt_pairs(VALIDATED_TRADING_PAIRS)}")
        log.info(f"[UNIVERSE] Research/watchlist: {_fmt_pairs(RESEARCH_WATCHLIST_PAIRS)}")
        self._log_learning_state()
        log.info(f"[LINEUP] Auto-open: {' | '.join(auto_open_lines)}")
        log.info(f"[LINEUP] Watch-only: {' | '.join(watch_only_lines)}")
        log.info("=" * 50)

        alert_info(startup_message)

        while True:
            scan_start = time.time()
            log.info("[LINEUP] Scanning...")

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
                         f"DailyRisk=${risk_st['daily_risk_used_usd']:.2f}")
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
