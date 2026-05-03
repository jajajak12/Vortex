"""
scanner_utils.py — P1.1: Shared scanner utilities.
PairContext, CooldownStore (thread-safe), SignalRateMonitor, ScanState.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from vortex_logger import get_logger
from config import SIGNAL_RATE_MIN

if TYPE_CHECKING:
    from core.signal_handler import SignalHandler
    from risk_manager import RiskManager

log = get_logger(__name__)


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


# ── Shared scan state (P1.1 + P3.2) ─────────────────────────────────────────

@dataclass
class ScanState:
    """
    Bundle semua shared mutable state untuk satu scan session.
    Dibuat sekali di VortexScanner.__init__, dipass ke strategy_runner functions.
    Thread-safe: masing-masing field sudah punya lock internal.
    """
    # Cooldown stores — satu per alert type
    # Shared S1-S6 entry cooldown store for setup-level signals.
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
    rate_mon: SignalRateMonitor = field(default_factory=SignalRateMonitor)

    # Lock for seen_ob compatibility with warmup and older callers.
    _ob_lock: threading.Lock = field(default_factory=threading.Lock)

    def ob_seen(self, key: str) -> bool:
        with self._ob_lock:
            return key in self.seen_ob

    def ob_add(self, key: str):
        with self._ob_lock:
            self.seen_ob.add(key)
