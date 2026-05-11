"""
RiskManager — dipakai semua strategi via VortexScanner (singleton).

Fungsi utama:
- evaluate(): hitung position size + validasi RR, daily risk, max open trades, ATR SL
- on_trade_opened() / on_trade_closed(): update internal counters
- sync_open_count(): sync dengan trade_tracker setiap awal scan cycle
- Auto-reset daily risk saat hari berganti (midnight)

Dry-run position size formula:
  current_equity = DRY_RUN_STARTING_EQUITY_USD + realized_pnl (closed trades only)
  risk_usd       = min(current_equity * RISK_EQUITY_PCT, MAX_RISK_USD)
  risk_per_unit  = |entry - sl|          ← stop distance in price units
  position_qty   = risk_usd / risk_per_unit
  position_usdt  = position_qty * entry  ← notional (face value)

Daily risk cap (USD): current_equity * MAX_DAILY_RISK_PCT / 100
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import (
    ACCOUNT_BALANCE, RISK_PCT_DEFAULT,
    MAX_DAILY_RISK_PCT, MAX_OPEN_TRADES,
    MIN_RR_RATIO, ATR_SL_MIN_MULT,
    DRY_RUN_STARTING_EQUITY_USD, RISK_EQUITY_PCT, MAX_RISK_USD,
)


@dataclass
class TradeSetup:
    pair:         str
    direction:    str     # "LONG" | "SHORT"
    entry:        float
    sl:           float
    tp:           float   # TP utama untuk RR check (pakai TP2 jika ada)
    strategy:     str     # "S1" | "S2" | "S3"
    atr:          float = 0.0
    risk_pct:     float = RISK_PCT_DEFAULT   # kept for compatibility, not used for sizing
    min_rr:       float = MIN_RR_RATIO
    atr_sl_mult:  float = ATR_SL_MIN_MULT


@dataclass
class RiskCheck:
    approved:          bool
    reason:            str   = ""
    position_usdt:     float = 0.0   # notional position size
    margin_usdt:       float = 0.0   # margin if futures (notional / leverage)
    rr_ratio:          float = 0.0
    risk_amount:       float = 0.0   # USD at risk this trade (= risk_usd)
    current_equity:    float = 0.0   # dry-run equity at time of signal
    risk_usd:          float = 0.0   # min(equity * 2%, $500)
    stop_distance_pct: float = 0.0   # |entry - sl| / entry * 100


class RiskManager:
    """
    Singleton — diinstansiasi sekali di VortexScanner.
    Thread-safe: Lock melindungi evaluate+on_trade_opened agar atomic.
    """

    def __init__(self):
        self._daily_risk_used_usd: float = 0.0
        self._open_trade_count:    int   = 0
        self._last_reset_date:     str   = ""
        self._lock = threading.Lock()

    # ── Internal helpers ──────────────────────────────────────

    def _auto_reset(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._daily_risk_used_usd = 0.0
            self._last_reset_date     = today

    # ── Public API ────────────────────────────────────────────

    def sync_open_count(self, count: int):
        self._open_trade_count = count

    def get_current_equity(self) -> float:
        """Dry-run equity = starting equity + realized PnL from closed trades."""
        import db
        return round(DRY_RUN_STARTING_EQUITY_USD + db.compute_realized_pnl(), 4)

    def evaluate(
        self,
        setup: TradeSetup,
        balance: float = ACCOUNT_BALANCE,   # kept for API compatibility; unused
        leverage: int  = 1,
    ) -> "RiskCheck":
        with self._lock:
            return self._evaluate_locked(setup, leverage)

    def _evaluate_locked(self, setup: TradeSetup, leverage: int) -> "RiskCheck":
        self._auto_reset()

        sl_dist = abs(setup.entry - setup.sl)
        tp_dist = abs(setup.tp   - setup.entry)

        if sl_dist == 0:
            return RiskCheck(approved=False, reason="invalid_stop_distance: sl_dist=0")

        sl_pct = sl_dist / setup.entry
        rr     = round(tp_dist / sl_dist, 2)

        # ── Gate 1: Minimum RR ────────────────────────────────
        if rr < setup.min_rr:
            return RiskCheck(
                approved=False,
                reason=f"RR {rr:.2f} < minimum {setup.min_rr} ({setup.pair})",
                rr_ratio=rr,
            )

        # ── Gate 2: ATR SL validation ─────────────────────────
        if setup.atr > 0 and sl_dist < setup.atr * setup.atr_sl_mult:
            return RiskCheck(
                approved=False,
                reason=(f"SL terlalu sempit: {sl_dist:.4f} < "
                        f"{setup.atr_sl_mult}x ATR ({setup.atr:.4f})"),
                rr_ratio=rr,
            )

        # ── Gate 3: Max open trades ───────────────────────────
        if self._open_trade_count >= MAX_OPEN_TRADES:
            return RiskCheck(
                approved=False,
                reason=f"Max open trades ({MAX_OPEN_TRADES}) tercapai",
                rr_ratio=rr,
            )

        # ── Equity & risk calculation ─────────────────────────
        import db
        current_equity   = DRY_RUN_STARTING_EQUITY_USD + db.compute_realized_pnl()
        risk_usd         = min(current_equity * RISK_EQUITY_PCT, MAX_RISK_USD)
        daily_limit_usd  = current_equity * MAX_DAILY_RISK_PCT / 100.0

        # ── Gate 4: Daily risk limit (USD) ────────────────────
        if self._daily_risk_used_usd + risk_usd > daily_limit_usd:
            return RiskCheck(
                approved=False,
                reason=(f"Daily risk limit: "
                        f"${self._daily_risk_used_usd:.2f} + ${risk_usd:.2f} "
                        f"> ${daily_limit_usd:.2f}"),
                rr_ratio=rr,
            )

        # ── Position size: risk_usd / stop_distance_per_unit ─
        position_qty       = risk_usd / sl_dist
        position_usdt      = round(position_qty * setup.entry, 2)
        margin_usdt        = round(position_usdt / leverage, 2)
        stop_distance_pct  = round(sl_pct * 100, 4)

        return RiskCheck(
            approved=True,
            position_usdt=position_usdt,
            margin_usdt=margin_usdt,
            rr_ratio=rr,
            risk_amount=round(risk_usd, 2),
            current_equity=round(current_equity, 2),
            risk_usd=round(risk_usd, 2),
            stop_distance_pct=stop_distance_pct,
        )

    def on_trade_opened(self, risk_usd: float = 0.0, risk_pct: float = 0.0):
        """Thread-safe. Pass risk_usd (preferred) for USD-based daily tracking."""
        with self._lock:
            self._daily_risk_used_usd += risk_usd
            self._open_trade_count    += 1

    def on_trade_closed(self):
        with self._lock:
            self._open_trade_count = max(0, self._open_trade_count - 1)

    # ── Status ────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "daily_risk_used_usd": round(self._daily_risk_used_usd, 2),
            "daily_risk_limit":    MAX_DAILY_RISK_PCT,
            "open_trades":         self._open_trade_count,
            "max_open_trades":     MAX_OPEN_TRADES,
        }
