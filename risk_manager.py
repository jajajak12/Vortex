"""
RiskManager — dipakai semua strategi via VortexScanner (singleton).

Fungsi utama:
- evaluate(): hitung position size + validasi RR, daily risk, max open trades, ATR SL
- on_trade_opened() / on_trade_closed(): update internal counters
- sync_open_count(): sync dengan trade_tracker setiap awal scan cycle
- Auto-reset daily risk saat hari berganti (midnight)

Position size formula (spot & futures):
  risk_amount   = balance × risk_pct / 100
  sl_pct        = |entry - sl| / entry
  position_usdt = risk_amount / sl_pct   ← notional (face value)
  margin_usdt   = position_usdt / leverage  ← hanya relevan untuk futures
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config import (
    ACCOUNT_BALANCE, RISK_PCT_DEFAULT,
    MAX_DAILY_RISK_PCT, MAX_OPEN_TRADES,
    MIN_RR_RATIO, ATR_SL_MIN_MULT,
)


@dataclass
class TradeSetup:
    pair:      str
    direction: str     # "LONG" | "SHORT"
    entry:     float
    sl:        float
    tp:        float   # TP utama untuk RR check (pakai TP2 jika ada)
    strategy:  str     # "S1" | "S2" | "S3"
    atr:       float = 0.0
    risk_pct:  float = RISK_PCT_DEFAULT


@dataclass
class RiskCheck:
    approved:      bool
    reason:        str   = ""
    position_usdt: float = 0.0   # notional position size (berapa USDT di-trade)
    margin_usdt:   float = 0.0   # margin required jika futures (notional / leverage)
    rr_ratio:      float = 0.0
    risk_amount:   float = 0.0   # USDT yang di-risk


class RiskManager:
    """
    Singleton — diinstansiasi sekali di VortexScanner.
    Thread-safety tidak diperlukan karena scanner berjalan single-thread.
    """

    def __init__(self):
        self._daily_risk_used:  float = 0.0
        self._open_trade_count: int   = 0
        self._last_reset_date:  str   = ""

    # ── Internal helpers ──────────────────────────────────────

    def _auto_reset(self):
        """Reset daily risk counter saat hari berganti."""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_reset_date:
            self._daily_risk_used  = 0.0
            self._last_reset_date  = today

    # ── Public API ────────────────────────────────────────────

    def sync_open_count(self, count: int):
        """
        Sync jumlah open trades dari trade_tracker.
        Panggil di awal setiap scan cycle.
        """
        self._open_trade_count = count

    def evaluate(
        self,
        setup: TradeSetup,
        balance: float = ACCOUNT_BALANCE,
        leverage: int  = 1,
    ) -> RiskCheck:
        """
        Validasi setup dan hitung position size.

        Args:
            setup:    parameter trade yang akan dievaluasi
            balance:  account balance USDT (default dari config)
            leverage: 1 = spot; >1 = futures (hanya mengubah margin_usdt)

        Returns:
            RiskCheck dengan approved=True jika semua gate terpenuhi.
        """
        self._auto_reset()

        sl_dist = abs(setup.entry - setup.sl)
        tp_dist = abs(setup.tp  - setup.entry)

        if sl_dist == 0:
            return RiskCheck(approved=False, reason="SL distance = 0")

        sl_pct  = sl_dist / setup.entry
        rr      = round(tp_dist / sl_dist, 2)

        # ── Gate 1: Minimum RR ────────────────────────────────
        if rr < MIN_RR_RATIO:
            return RiskCheck(
                approved=False,
                reason=f"RR {rr:.2f} < minimum {MIN_RR_RATIO}",
                rr_ratio=rr,
            )

        # ── Gate 2: ATR SL validation ─────────────────────────
        if setup.atr > 0 and sl_dist < setup.atr * ATR_SL_MIN_MULT:
            return RiskCheck(
                approved=False,
                reason=(f"SL terlalu sempit: {sl_dist:.4f} < "
                        f"{ATR_SL_MIN_MULT}x ATR ({setup.atr:.4f})"),
                rr_ratio=rr,
            )

        # ── Gate 3: Max open trades ───────────────────────────
        if self._open_trade_count >= MAX_OPEN_TRADES:
            return RiskCheck(
                approved=False,
                reason=f"Max open trades ({MAX_OPEN_TRADES}) tercapai",
                rr_ratio=rr,
            )

        # ── Gate 4: Daily risk limit ──────────────────────────
        if self._daily_risk_used + setup.risk_pct > MAX_DAILY_RISK_PCT:
            return RiskCheck(
                approved=False,
                reason=(f"Daily risk limit: "
                        f"{self._daily_risk_used:.1f}% + {setup.risk_pct}% "
                        f"> {MAX_DAILY_RISK_PCT}%"),
                rr_ratio=rr,
            )

        # ── Position size calculation ─────────────────────────
        risk_amount   = balance * setup.risk_pct / 100
        position_usdt = round(risk_amount / sl_pct, 2)
        margin_usdt   = round(position_usdt / leverage, 2)

        return RiskCheck(
            approved=True,
            position_usdt=position_usdt,
            margin_usdt=margin_usdt,
            rr_ratio=rr,
            risk_amount=round(risk_amount, 2),
        )

    def on_trade_opened(self, risk_pct: float = RISK_PCT_DEFAULT):
        """Panggil setelah trade berhasil di-log."""
        self._daily_risk_used  += risk_pct
        self._open_trade_count += 1

    def on_trade_closed(self):
        """Panggil setiap kali trade closed (WIN/LOSS)."""
        self._open_trade_count = max(0, self._open_trade_count - 1)

    # ── Status ────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "daily_risk_used":  round(self._daily_risk_used, 2),
            "daily_risk_limit": MAX_DAILY_RISK_PCT,
            "open_trades":      self._open_trade_count,
            "max_open_trades":  MAX_OPEN_TRADES,
        }
