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
    "S1": "S4-MOMENTUM BOS+MSS",
    "S2": "S6 EMA Stack",
    "S3": "S7 P10 Swing Reversal",
    "S4": "S8 Volume Surge Bear SHORT",
    "S5": "volume_impulse_bull_close_high LONG",
    "S6": "donchian_breakout LONG 50-period",
}

SCORE_HIGH   = 7.5
SCORE_MEDIUM = 5.0

# S4 (V Pattern) — TP caps
S4_TP1_MAX_RR = 3.0   # TP1 max RR 1:3.0
S4_TP2_MAX_RR = 4.8   # TP2 max RR 1:4.8

# S4 — minimum score thresholds (tiered by original RR)
S4_MIN_SCORE     = 7.8   # normal
S4_MIN_SCORE_RR4 = 8.7   # original RR > 4.0
S4_MIN_SCORE_RR6 = 9.1   # original RR > 6.0


# ── Signal dataclass ──────────────────────────────────────────────

@dataclass
class Signal:
    """
    Representasi unified satu entry signal dari strategi manapun.
    Semua field wajib kecuali tp2_price, risk_percent, position_size,
    invalidation_price, original_rr.
    """
    strategy_id:        str          # "S1" | "S2" | "S3" | "S4"
    symbol:             str          # "BTCUSDT"
    direction:          str          # "LONG" | "SHORT"
    timeframe:          str          # label siap tampil: "4H", "1D (Daily)", dst.
    entry_price:        float
    sl_price:           float
    tp1_price:          float
    tp2_price:          Optional[float]
    rr:                 float        # RR ke TP terbaik; di-update setelah TP di-cap
    score:              float        # raw score; dinormalisasi ke 1–10 oleh handler
    reason:             str          # 1–2 kalimat penjelasan kenapa signal muncul
    risk_percent:       float = 0.0  # % risk dari balance (0 = tidak ditampilkan)
    position_size:      float = 0.0  # nominal USDT (0 = tidak ditampilkan)
    invalidation_price: Optional[float] = None
    original_rr:        float = 0.0  # RR sebelum TP cap; 0.0 = belum diproses


# ── S4 TP cap utility ─────────────────────────────────────────────

def adjust_tp_for_ob(
    entry: float,
    sl_price: float,
    tp1: float,
    tp2: Optional[float],
    direction: str,
) -> tuple[float, Optional[float]]:
    """
    Cap TP untuk S4-OB (Order Block) ke batas RR realistis.
      TP1: max RR 1:3.0
      TP2: max RR 1:4.8
    """
    sl_dist = abs(entry - sl_price)
    if sl_dist == 0:
        return tp1, tp2

    if direction == "LONG":
        tp1 = min(tp1, entry + sl_dist * S4_TP1_MAX_RR)
        tp2 = min(tp2, entry + sl_dist * S4_TP2_MAX_RR) if tp2 is not None else None
    else:  # SHORT
        tp1 = max(tp1, entry - sl_dist * S4_TP1_MAX_RR)
        tp2 = max(tp2, entry - sl_dist * S4_TP2_MAX_RR) if tp2 is not None else None

    return round(tp1, 6), (round(tp2, 6) if tp2 is not None else None)


# ── SignalHandler ─────────────────────────────────────────────────

class SignalHandler:
    """
    Pusat pemrosesan dan pengiriman alert untuk semua strategi.

    Method utama:
      calculate_score(signal)          → float (1–10, dengan RR penalty/bonus untuk S4)
      process_signals(signals, ...)    → list[Signal] (cap TP dulu, filter, sort)
      send_alert(signal)               → None (cap TP → score → gate → Telegram)
    """

    # ── S4 internal helpers ───────────────────────────────────────

    def _apply_s4_caps(self, signal: Signal) -> None:
        """
        Cap TP1/TP2 S4 ke batas realistis (in-place). Idempotent — aman dipanggil
        berkali-kali: original_rr hanya di-set pada pemanggilan pertama.
        """
        if not signal.strategy_id.startswith("S4"):
            return

        sl_dist = abs(signal.entry_price - signal.sl_price)
        if sl_dist == 0:
            return

        # Simpan original RR sebelum cap (hanya sekali)
        if signal.original_rr == 0.0:
            signal.original_rr = signal.rr

        tp1_adj, tp2_adj = adjust_tp_for_ob(
            signal.entry_price, signal.sl_price,
            signal.tp1_price, signal.tp2_price,
            signal.direction,
        )

        if tp1_adj != signal.tp1_price or tp2_adj != signal.tp2_price:
            log.info(
                f"[S4 TP CAP] {signal.symbol} {signal.direction}: "
                f"TP1 {signal.tp1_price}→{tp1_adj}, "
                f"TP2 {signal.tp2_price}→{tp2_adj}  "
                f"(original RR {signal.original_rr})"
            )

        signal.tp1_price = tp1_adj
        signal.tp2_price = tp2_adj
        signal.rr = round(abs(tp1_adj - signal.entry_price) / sl_dist, 2)

    def _s4_original_rr(self, signal: Signal) -> float:
        """Return original RR sebelum cap. Fallback ke signal.rr jika belum diproses."""
        return signal.original_rr if signal.original_rr > 0.0 else signal.rr

    # ── Core methods ──────────────────────────────────────────────

    def calculate_score(self, signal: Signal) -> float:
        """
        Clamp raw score ke 1.0–10.0.
        S4: terapkan RR realism penalty/bonus menggunakan original_rr.

          original RR > 6.0       → -2.0  (V terlalu besar, butuh semua faktor)
          original RR 4.0 – 6.0   → -1.2  (setup ekstrem)
          capped RR   2.5 – 3.5   → +0.8  (sweet spot, probabilitas optimal)
        """
        base = round(max(1.0, min(10.0, signal.score)), 1)

        if not signal.strategy_id.startswith("S4"):
            return base

        orig_rr = self._s4_original_rr(signal)

        if orig_rr > 6.0:
            adj = -2.0
        elif orig_rr > 4.0:
            adj = -1.2
        elif 2.5 <= signal.rr <= 3.5:   # gunakan capped RR untuk bonus
            adj = +0.8
        else:
            adj = 0.0

        return round(max(1.0, min(10.0, base + adj)), 1)

    def _min_score_for(self, signal: Signal) -> float:
        """
        Return minimum passing score per strategy.
        S4: flat 8.0 (strategy already gates at 8.0).
        """
        if signal.strategy_id.startswith("S4"):
            return 8.0
        return SCORE_MEDIUM

    def process_signals(
        self,
        signals: list[Signal],
        min_score: float | None = None,
    ) -> list[Signal]:
        """
        Cap TP S4 lebih awal, lalu filter di bawah threshold per strategi,
        kemudian urutkan score tertinggi dulu.
        """
        # Cap TP S4 sebelum scoring (idempotent)
        for s in signals:
            self._apply_s4_caps(s)

        def _passes(s: Signal) -> bool:
            score     = self.calculate_score(s)
            threshold = self._min_score_for(s)
            if min_score is not None:
                threshold = max(threshold, min_score)
            return score >= threshold

        filtered = [s for s in signals if _passes(s)]
        filtered.sort(key=lambda s: self.calculate_score(s), reverse=True)
        return filtered

    def send_alert(self, signal: Signal) -> None:
        """
        Format signal dan kirim ke Telegram.

        S4 pipeline:
          1. _apply_s4_caps()  — cap TP, simpan original_rr (idempotent)
          2. calculate_score() — RR penalty/bonus via original_rr
          3. _min_score_for()  — threshold tiered: 7.8 / 8.8 / 9.2
          4. Kirim jika lolos gate
        """
        self._apply_s4_caps(signal)

        score     = self.calculate_score(signal)
        threshold = self._min_score_for(signal)

        if score < threshold:
            orig_rr = self._s4_original_rr(signal)
            log.warning(
                f"⛔ [S4 GATE] {signal.symbol} {signal.direction} rejected: "
                f"score {score} < {threshold}  "
                f"(original RR {orig_rr}, capped RR {signal.rr})"
            )
            return

        msg = self._format(signal, score)
        orig_rr = self._s4_original_rr(signal)
        log.info(
            f"📤 [{signal.strategy_id}] {signal.symbol} {signal.direction} "
            f"@ {signal.entry_price} | TF={signal.timeframe} | "
            f"Score={score} | RR={signal.rr}"
            + (f" (original {orig_rr})" if signal.strategy_id == "S4" else "")
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

        tp2_line = ""
        if s.tp2_price is not None:
            tp2_dist = abs(s.tp2_price - s.entry_price)
            tp2_pct  = tp2_dist / s.entry_price * 100 if s.entry_price > 0 else 0
            tp2_rr   = round(tp2_dist / sl_dist, 2) if sl_dist > 0 else 0
            tp2_line = (
                f"TP2      : <b>${fmt(s.tp2_price)}</b>"
                f"  (+{tp2_pct:.2f}%)  RR 1:{tp2_rr}\n"
            )

        risk_line = ""
        if s.risk_percent > 0 or s.position_size > 0:
            parts: list[str] = []
            if s.risk_percent > 0:
                parts.append(f"Risk {s.risk_percent:.1f}%")
            if s.position_size > 0:
                parts.append(f"Size ${s.position_size:,.2f}")
            risk_line = "  │  ".join(parts) + "\n"

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
