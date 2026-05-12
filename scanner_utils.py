"""
scanner_utils.py — P1.1: Shared scanner utilities.
PairContext, CooldownStore (thread-safe), SignalRateMonitor, ScanState.
"""

import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from vortex_logger import get_logger
from config import SIGNAL_RATE_MIN
from strategy_metadata import get_strategy_meta
from strategy_registry import STRATEGY_ORDER

if TYPE_CHECKING:
    from core.signal_handler import SignalHandler
    from risk_manager import RiskManager

log = get_logger(__name__)

STRATEGY_LABELS: dict[str, str] = {
    sid: get_strategy_meta(sid).strategy_name
    for sid in STRATEGY_ORDER
}


# ── Per-pair scan context ─────────────────────────────────────────────────────

@dataclass
class PairContext:
    """State yang dikompute sekali per pair, dipakai semua strategi."""
    pair:             str
    btc_macro:        str
    lesson_ctx:       str  = ""
    wick_setups:      list = field(default_factory=list)
    fvg_imbal_setups: list = field(default_factory=list)
    ob_setups:        list = field(default_factory=list)
    chart_setups:     list = field(default_factory=list)
    eng_setups:       list = field(default_factory=list)
    params:           dict = field(default_factory=dict)


# ── Alert cooldown store (thread-safe) ───────────────────────────────────────

class CooldownStore:
    """Cooldown per zona key. Auto-expire setelah COOLDOWN detik.
    Thread-safe: RLock melindungi check+set agar atomic. (P3.2)
    """
    COOLDOWN = 4 * 60 * 60  # 4 jam

    def __init__(self):
        self._store: dict[str, float] = {}
        self._lock  = threading.RLock()

    def is_on_cooldown(self, key: str) -> bool:
        with self._lock:
            if key not in self._store:
                return False
            if time.time() - self._store[key] >= self.COOLDOWN:
                del self._store[key]
                return False
            return True

    def set(self, key: str):
        with self._lock:
            self._store[key] = time.time()

    def check_and_set(self, key: str) -> bool:
        """Atomic: return True (on cooldown, skip) or False (set cooldown, proceed)."""
        with self._lock:
            now = time.time()
            ts  = self._store.get(key)
            if ts is not None and now - ts < self.COOLDOWN:
                return True   # on cooldown
            self._store[key] = now
            return False      # was not on cooldown, now set


# ── Signal rate monitor ───────────────────────────────────────────────────────

class SignalRateMonitor:
    """Tracking jumlah signal per pair per hari. Warning jika terlalu sedikit."""

    def __init__(self, min_per_day: int = SIGNAL_RATE_MIN):
        self.min_per_day = min_per_day
        self._counts: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def track(self, pair: str):
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            self._counts.setdefault(today, {})
            self._counts[today][pair] = self._counts[today].get(pair, 0) + 1

    def check(self, pairs: list[str]):
        today  = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            counts = self._counts.get(today, {})
        low = [p for p in pairs if counts.get(p, 0) < self.min_per_day]
        if low:
            log.warning(f"[SIGNAL RATE] <{self.min_per_day} signal hari ini: {low}")


@dataclass
class StrategyDecision:
    pair: str
    strategy_id: str
    strategy_name: str
    legacy_label: str
    evaluated: bool
    raw_signal: bool
    opened: bool = False
    blocked_reason: str = ""
    detail: str = ""


class ScanDiagnostics:
    """Thread-safe per-scan diagnostics collector."""

    def __init__(self):
        self._lock = threading.Lock()
        self.evaluated_by_strategy: Counter[str] = Counter()
        self.raw_signal_by_strategy: Counter[str] = Counter()
        self.blocked_by_reason: Counter[str] = Counter()
        self.opened_by_strategy: Counter[str] = Counter()
        self.decisions: list[StrategyDecision] = []

    def record(self, decision: StrategyDecision):
        with self._lock:
            self.decisions.append(decision)
            if decision.evaluated:
                self.evaluated_by_strategy[decision.strategy_id] += 1
            if decision.raw_signal:
                self.raw_signal_by_strategy[decision.strategy_id] += 1
            if decision.opened:
                self.opened_by_strategy[decision.strategy_id] += 1
            elif decision.blocked_reason:
                self.blocked_by_reason[decision.blocked_reason] += 1

        status = "opened" if decision.opened else (
            decision.blocked_reason or ("no_raw_signal" if not decision.raw_signal else "blocked")
        )
        detail = f" detail={decision.detail}" if decision.detail else ""
        log.info(
            "[DIAG] "
            f"{decision.pair} "
            f"strategy={decision.strategy_id} "
            f"name=\"{decision.strategy_name}\" "
            f"evaluated={'true' if decision.evaluated else 'false'} "
            f"raw_signal={'true' if decision.raw_signal else 'false'} "
            f"result={status}{detail}"
        )

    def log_summary(self):
        def _fmt(counter: Counter[str]) -> str:
            if not counter:
                return "-"
            return ", ".join(f"{k}={counter[k]}" for k in sorted(counter))

        log.info(
            "[DIAG SUMMARY] "
            f"evaluated_by_strategy: {_fmt(self.evaluated_by_strategy)} | "
            f"raw_signal_by_strategy: {_fmt(self.raw_signal_by_strategy)} | "
            f"blocked_by_reason: {_fmt(self.blocked_by_reason)} | "
            f"opened_by_strategy: {_fmt(self.opened_by_strategy)}"
        )


# ── Shared scan state (P1.1 + P3.2) ─────────────────────────────────────────

@dataclass
class ScanState:
    """
    Bundle semua shared mutable state untuk satu scan session.
    Dibuat sekali di VortexScanner.__init__, dipass ke strategy_runner functions.
    Thread-safe: masing-masing field sudah punya lock internal.
    """
    # Cooldown stores — satu per alert type
    # Shared entry cooldown store for setup-level signals.
    cd_touch:  CooldownStore = field(default_factory=CooldownStore)
    cd_entry:  CooldownStore = field(default_factory=CooldownStore)
    cd_wick_e: CooldownStore = field(default_factory=CooldownStore)
    cd_fvg_e:  CooldownStore = field(default_factory=CooldownStore)
    cd_ob_e:   CooldownStore = field(default_factory=CooldownStore)
    cd_eng_e:  CooldownStore = field(default_factory=CooldownStore)

    # Permanent seen sets (overlap prevention, keyed by pair+zone)
    seen_wick: set = field(default_factory=set)
    seen_fvg:  set = field(default_factory=set)
    seen_ob:   set = field(default_factory=set)

    # Shared services (thread-safe internally or stateless)
    signal_handler: object = None   # SignalHandler
    risk_mgr:       object = None   # RiskManager
    auto_demo_executor: object = None
    rate_mon: SignalRateMonitor = field(default_factory=SignalRateMonitor)
    diagnostics: ScanDiagnostics = field(default_factory=ScanDiagnostics)
    scan_cycle_started_at: object = None

    # Lock for seen_ob compatibility with warmup and older callers.
    _ob_lock: threading.Lock = field(default_factory=threading.Lock)

    def ob_seen(self, key: str) -> bool:
        with self._ob_lock:
            return key in self.seen_ob

    def ob_add(self, key: str):
        with self._ob_lock:
            self.seen_ob.add(key)
