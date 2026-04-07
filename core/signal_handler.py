"""
core/signal_handler.py — Unified Signal Alert System
=====================================================
Semua entry signal dari 4 strategi melewati SignalHandler.send_alert().
Format Telegram konsisten, score dinormalisasi ke skala 1–10.

Usage (di scanner.py):
    from core.signal_handler import Signal, SignalHandler
    handler = SignalHandler()
    handler.send_alert(Signal(...))
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from telegram_bot import send_telegram
from vortex_logger import get_logger

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────

STRATEGY_LABELS: dict[str, str] = {
    "S1": "Liquidity Grab",
    "S2": "Wick Fill",
    "S3": "FVG Reclaim",
    "S4": "V Pattern",
}

SCORE_HIGH   = 7.5
SCORE_MEDIUM = 5.0


# ── Signal dataclass ──────────────────────────────────────────────

@dataclass
class Signal:
    """
    Representasi unified satu entry signal dari strategi manapun.
    Semua field wajib kecuali tp2_price, risk_percent, position_size,
    invalidation_price.
    """
    strategy_id:        str          # "S1" | "S2" | "S3" | "S4"
    symbol:             str          # "BTCUSDT"
    direction:          str          # "LONG" | "SHORT"
    timeframe:          str          # label siap tampil: "4H", "1D (Daily)", dst.
    entry_price:        float
    sl_price:           float
    tp1_price:          float
    tp2_price:          Optional[float]
    rr:                 float        # RR ke TP terbaik (sudah dihitung caller)
    score:              float        # raw score; dinormalisasi ke 1–10 oleh handler
    reason:             str          # 1–2 kalimat penjelasan kenapa signal muncul
    risk_percent:       float = 0.0  # % risk dari balance (0 = tidak ditampilkan)
    position_size:      float = 0.0  # nominal USDT (0 = tidak ditampilkan)
    invalidation_price: Optional[float] = None  # harga yang membatalkan setup


# ── SignalHandler ─────────────────────────────────────────────────

class SignalHandler:
    """
    Pusat pemrosesan dan pengiriman alert untuk semua strategi.

    Tiga method utama:
      calculate_score(signal)          → float (1–10, diclamped)
      process_signals(signals, ...)    → list[Signal] (filtered + sorted)
      send_alert(signal)               → None (format + kirim Telegram)
    """

    def calculate_score(self, signal: Signal) -> float:
        """Clamp raw score ke range 1.0–10.0."""
        return round(max(1.0, min(10.0, signal.score)), 1)

    def process_signals(
        self,
        signals: list[Signal],
        min_score: float = SCORE_MEDIUM,
    ) -> list[Signal]:
        """
        Filter sinyal di bawah min_score, lalu urutkan score tertinggi dulu.
        Berguna jika di masa depan ada batch processing multi-pair.
        """
        filtered = [s for s in signals if self.calculate_score(s) >= min_score]
        filtered.sort(key=lambda s: self.calculate_score(s), reverse=True)
        return filtered

    def send_alert(self, signal: Signal) -> None:
        """Format signal dan kirim ke Telegram."""
        score = self.calculate_score(signal)
        msg   = self._format(signal, score)
        log.info(
            f"📤 [{signal.strategy_id}] {signal.symbol} {signal.direction} "
            f"@ {signal.entry_price} | TF={signal.timeframe} | "
            f"Score={score} | RR={signal.rr}"
        )
        send_telegram(msg)

    # ── Private: formatting ───────────────────────────────────────

    def _format(self, s: Signal, score: float) -> str:
        dir_emoji  = "🟢" if s.direction == "LONG" else "🔴"
        strat_name = STRATEGY_LABELS.get(s.strategy_id, s.strategy_id)

        if score >= SCORE_HIGH:
            score_label = "⭐⭐⭐ HIGH"
        elif score >= SCORE_MEDIUM:
            score_label = "⭐⭐ MEDIUM"
        else:
            score_label = "⭐ LOW"

        def fmt(p: float) -> str:
            """Format harga sesuai magnitude, tanpa trailing zeros berlebihan."""
            if p >= 1000:
                return f"{p:,.2f}"
            if p >= 1:
                return f"{p:,.4f}"
            return f"{p:.6f}"

        sl_dist  = abs(s.entry_price - s.sl_price)
        sl_pct   = sl_dist / s.entry_price * 100 if s.entry_price > 0 else 0

        tp1_dist = abs(s.tp1_price - s.entry_price)
        tp1_pct  = tp1_dist / s.entry_price * 100 if s.entry_price > 0 else 0
        tp1_rr   = round(tp1_dist / sl_dist, 2) if sl_dist > 0 else 0

        # TP2 (opsional)
        tp2_line = ""
        if s.tp2_price is not None:
            tp2_dist = abs(s.tp2_price - s.entry_price)
            tp2_pct  = tp2_dist / s.entry_price * 100 if s.entry_price > 0 else 0
            tp2_rr   = round(tp2_dist / sl_dist, 2) if sl_dist > 0 else 0
            tp2_line = (
                f"TP2      : <b>${fmt(s.tp2_price)}</b>"
                f"  (+{tp2_pct:.2f}%)  RR 1:{tp2_rr}\n"
            )

        # Risk / size (opsional)
        risk_line = ""
        if s.risk_percent > 0 or s.position_size > 0:
            parts: list[str] = []
            if s.risk_percent > 0:
                parts.append(f"Risk {s.risk_percent:.1f}%")
            if s.position_size > 0:
                parts.append(f"Size ${s.position_size:,.2f}")
            risk_line = "  │  ".join(parts) + "\n"

        # Invalidation
        inv_line = ""
        if s.invalidation_price is not None:
            side     = "bawah" if s.direction == "LONG" else "atas"
            inv_line = (
                f"\n⚠️ <i>Invalid jika close di {side} "
                f"${fmt(s.invalidation_price)}</i>"
            )

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        return (
            f"🌀 <b>VORTEX SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"{dir_emoji} <b>{s.direction}</b>  │  <b>{s.symbol}</b>  │  {s.timeframe}\n"
            f"Strategy : {s.strategy_id} — {strat_name}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Entry    : <b>${fmt(s.entry_price)}</b>\n"
            f"SL       : ${fmt(s.sl_price)}  (-{sl_pct:.2f}%)\n"
            f"TP1      : <b>${fmt(s.tp1_price)}</b>"
            f"  (+{tp1_pct:.2f}%)  RR 1:{tp1_rr}\n"
            f"{tp2_line}"
            f"━━━━━━━━━━━━━━━\n"
            f"Score    : {score}/10  {score_label}\n"
            f"{risk_line}"
            f"━━━━━━━━━━━━━━━\n"
            f"📝 {s.reason}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🕐 {ts}"
            f"{inv_line}"
        )
