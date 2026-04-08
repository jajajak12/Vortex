"""
Strategy 4: V Pattern (Sharp Reversal)
=======================================
V-Bottom (Bullish):
  1. Sharp decline >= 1.8x ATR dalam 3-8 candle
  2. Diikuti recovery >= 60% dari drop
  3. Rejection candle / long wick di titik terendah V
  4. Optional: liquidity sweep di bawah recent swing low sebelum reversal

V-Top (Bearish):
  1. Sharp rally >= 1.8x ATR dalam 3-8 candle
  2. Diikuti drop >= 60% dari rally
  3. Rejection candle / long wick di titik tertinggi V
  4. Optional: liquidity sweep di atas recent swing high

Timeframe: 4H (primary), 1D (secondary)
Entry zone: Harga masih dalam 80% zona recovery (ada upside/downside tersisa)
SL: 0.5% di luar titik V
TP1: Neckline (pre-drop high / pre-rally low)
TP2: Extension 1:1 (neckline + drop size)
"""

from strategy1_liquidity import get_candles, calculate_atr, _compute_htf_bias
from vortex_logger import get_logger

log = get_logger(__name__)

# ── Parameters ────────────────────────────────────────────────
VPATTERN_TIMEFRAMES    = ["4h", "1d"]
VPATTERN_ATR_MULT      = 1.8    # Minimum sharp move = 1.8x ATR
VPATTERN_ATR_STRONG    = 2.5    # "Strong" V jika >= 2.5x ATR
VPATTERN_DROP_CANDLES  = 8      # Max candle untuk fase drop/rally
VPATTERN_RECOV_CANDLES = 10     # Max candle untuk fase recovery
VPATTERN_RECOV_MIN     = 0.60   # Recovery minimal 60% dari drop
VPATTERN_RECOV_STRONG  = 0.75   # Recovery "kuat" >= 75%
VPATTERN_WICK_MIN      = 0.30   # Long wick jika >= 30% total range
VPATTERN_V_AGE_MAX     = 18     # V point harus dalam 18 candle terakhir
VPATTERN_SL_BUFFER     = 0.005  # 0.5% buffer SL di luar titik V

# Min score untuk kirim alert (scanner memakai ini)
STRAT4_MIN_SCORE = 7.8

TF_LABEL    = {"4h": "4H", "1d": "1D"}
TF_PRIORITY = {"4h": "🟡 MEDIUM", "1d": "🔴 HIGH"}

SCORE_HIGH   = 7.5
SCORE_MEDIUM = STRAT4_MIN_SCORE


# ── Helpers ───────────────────────────────────────────────────

def _rejection_wick(candle: dict) -> tuple[bool, float]:
    """
    Deteksi apakah candle punya long wick rejection.
    Return (has_wick, wick_ratio) — wick terbesar (lower atau upper).
    """
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    total_range = h - l
    if total_range == 0:
        return False, 0.0
    body_top    = max(o, c)
    body_bottom = min(o, c)
    lower_ratio = (body_bottom - l) / total_range
    upper_ratio = (h - body_top)  / total_range
    best = max(lower_ratio, upper_ratio)
    return best >= VPATTERN_WICK_MIN, round(best, 2)


def _1d_bias_aligned(direction: str, candles_1d: list[dict] | None) -> bool:
    """Cek apakah 1D EMA50 searah dengan V pattern."""
    if not candles_1d or len(candles_1d) < 55:
        return False
    bias = _compute_htf_bias(candles_1d)
    return bias == direction


def _score(
    move_size: float, atr: float,
    reversal_pct: float,
    has_rejection: bool, wick_ratio: float,
    has_sweep: bool, tf: str,
    bias_aligned: bool = False,
) -> tuple[float, list[str]]:
    """
    Hitung confidence score S4 (1–10) — bobot lebih tinggi untuk:
      recovery strength, wick rejection, HTF alignment, liquidity sweep.
    Base lebih rendah (3.0) → harus buktikan semua faktor untuk lolos 7.8.
    """
    score = 4.5
    notes = []

    # ── 1. Recovery strength (bobot terbesar: max +3.0) ──
    if reversal_pct >= 0.90:
        score += 3.0
        notes.append(f"✅ Recovery sangat kuat: {int(reversal_pct*100)}%  (+3.0)")
    elif reversal_pct >= VPATTERN_RECOV_STRONG:   # >= 0.75
        score += 2.0
        notes.append(f"✅ Recovery kuat: {int(reversal_pct*100)}%  (+2.0)")
    else:
        score += 0.5
        notes.append(f"⬜ Recovery lemah: {int(reversal_pct*100)}%  (+0.5)")

    # ── 2. Sharp move magnitude (max +1.5) ──
    atr_mult = move_size / atr if atr > 0 else 0
    if atr_mult >= 3.0:
        score += 1.5
        notes.append(f"✅ Sharp move sangat kuat: {atr_mult:.1f}x ATR  (+1.5)")
    elif atr_mult >= VPATTERN_ATR_STRONG:          # >= 2.5
        score += 1.0
        notes.append(f"✅ Sharp move kuat: {atr_mult:.1f}x ATR  (+1.0)")
    else:
        score += 0.5
        notes.append(f"⬜ Sharp move cukup: {atr_mult:.1f}x ATR  (+0.5)")

    # ── 3. Wick rejection (bobot tinggi: max +1.5) ──
    if has_rejection and wick_ratio >= 0.50:
        score += 1.5
        notes.append(f"✅ Wick rejection kuat: {int(wick_ratio*100)}% range  (+1.5)")
    elif has_rejection:
        score += 1.0
        notes.append(f"✅ Rejection candle: {int(wick_ratio*100)}% range  (+1.0)")
    else:
        notes.append("⛔ Tidak ada rejection candle  (+0)")

    # ── 4. Liquidity sweep (max +1.5) ──
    if has_sweep:
        score += 1.5
        notes.append("✅ Liquidity sweep sebelum reversal  (+1.5)")
    else:
        notes.append("⬜ Tidak ada sweep yang jelas  (+0)")

    # ── 5. HTF alignment (max +1.5) ──
    if bias_aligned:
        score += 1.5
        notes.append("✅ 1D EMA50 konfirmasi searah  (+1.5)")
    elif tf == "1d":
        score += 0.5
        notes.append("✅ Daily TF setup  (+0.5)")
    else:
        notes.append("⛔ HTF bias tidak konfirmasi  (+0)")

    return round(min(score, 10.0), 1), notes


# ── V-Bottom (Bullish) ────────────────────────────────────────

def _scan_v_bottom(candles: list[dict], atr: float, tf: str,
                   candles_1d: list[dict] | None = None) -> list[dict]:
    """Scan untuk V-Bottom (Bullish Reversal) setups."""
    results = []
    n = len(candles)

    for v_idx in range(VPATTERN_DROP_CANDLES, n - 1):
        # Hanya cek V yang cukup baru
        if v_idx < n - VPATTERN_V_AGE_MAX:
            continue

        v_low = candles[v_idx]["low"]

        # ── V point harus lokal minimum (2 candle kiri, 2 kanan) ──
        left  = [candles[j]["low"] for j in range(max(0, v_idx - 2), v_idx)]
        right = [candles[j]["low"] for j in range(v_idx + 1, min(n, v_idx + 3))]
        if left  and v_low >= min(left):
            continue
        if right and v_low >= min(right):
            continue

        # ── Sharp decline sebelum V ──
        pre_start = max(0, v_idx - VPATTERN_DROP_CANDLES)
        pre_high  = max(c["high"] for c in candles[pre_start:v_idx])
        drop_size = pre_high - v_low

        if drop_size < atr * VPATTERN_ATR_MULT:
            continue

        # ── Recovery setelah V ──
        post_end     = min(n, v_idx + VPATTERN_RECOV_CANDLES + 1)
        post_candles = candles[v_idx + 1:post_end]
        if not post_candles:
            continue

        post_high    = max(c["high"] for c in post_candles)
        recovery     = post_high - v_low
        reversal_pct = recovery / drop_size

        if reversal_pct < VPATTERN_RECOV_MIN:
            continue

        # ── Early filter: recovery < 75% → langsung reject (V lemah, high risk) ──
        if reversal_pct < VPATTERN_RECOV_STRONG:
            continue

        # ── Rejection candle di titik V ──
        has_rejection, wick_ratio = _rejection_wick(candles[v_idx])

        # ── Liquidity sweep: V low < previous swing lows ──
        prev_lows = [candles[j]["low"] for j in range(max(0, v_idx - 20), v_idx - 3)]
        has_sweep = bool(prev_lows) and v_low < min(prev_lows)

        # ── 1D bias alignment (hanya relevan untuk 4H setups) ──
        bias_aligned = _1d_bias_aligned("LONG", candles_1d) if tf == "4h" else False

        # ── Score ──
        score, notes = _score(
            drop_size, atr, reversal_pct,
            has_rejection, wick_ratio, has_sweep, tf,
            bias_aligned=bias_aligned,
        )

        # ── Entry zone: HARUS di bawah 50% recovery — V-bottom valid only near the low ──
        # Late entry (price sudah 80%+ recovered) = bukan V-bottom, sudah terlambat
        current_price = candles[-1]["close"]
        in_entry_zone = v_low < current_price <= (v_low + recovery * 0.50)

        # ── Trade calculation ──
        sl      = v_low * (1 - VPATTERN_SL_BUFFER)
        tp1     = pre_high                  # Neckline
        tp2     = pre_high + drop_size      # Extension 1:1
        sl_dist = current_price - sl

        rr1 = round((tp1 - current_price) / sl_dist, 2) if sl_dist > 0 else 0
        rr2 = round((tp2 - current_price) / sl_dist, 2) if sl_dist > 0 else 0

        trade = {
            "entry":   round(current_price, 4),
            "sl":      round(sl, 4),
            "tp1":     round(tp1, 4),
            "tp2":     round(tp2, 4),
            "rr1":     f"1:{rr1}",
            "rr2":     f"1:{rr2}",
            "sl_pct":  round((current_price - sl) / current_price * 100, 2),
            "tp1_pct": round((tp1 - current_price) / current_price * 100, 2),
            "tp2_pct": round((tp2 - current_price) / current_price * 100, 2),
        } if in_entry_zone else None

        label = ("⭐⭐⭐ HIGH"   if score >= SCORE_HIGH
                 else "⭐⭐ MEDIUM" if score >= SCORE_MEDIUM
                 else "⭐ LOW")

        results.append({
            "direction":        "LONG",
            "pattern":          "V-Bottom",
            "tf":               tf,
            "tf_label":         TF_LABEL[tf],
            "priority":         TF_PRIORITY[tf],
            "v_low":            round(v_low, 4),
            "pre_high":         round(pre_high, 4),
            "post_high":        round(post_high, 4),
            "drop_size":        round(drop_size, 4),
            "reversal_pct":     round(reversal_pct, 2),
            "has_rejection":    has_rejection,
            "wick_ratio":       wick_ratio,
            "has_sweep":        has_sweep,
            "atr":              round(atr, 4),
            "current_price":    current_price,
            "in_entry_zone":    in_entry_zone,
            "trade":            trade,
            "confidence_score": score,
            "confidence_label": label,
            "confluence_notes": notes,
            "bias_aligned":     bias_aligned,
        })

    return results


# ── V-Top (Bearish) ───────────────────────────────────────────

def _scan_v_top(candles: list[dict], atr: float, tf: str,
                candles_1d: list[dict] | None = None) -> list[dict]:
    """Scan untuk V-Top (Bearish Reversal) setups."""
    results = []
    n = len(candles)

    for v_idx in range(VPATTERN_DROP_CANDLES, n - 1):
        if v_idx < n - VPATTERN_V_AGE_MAX:
            continue

        v_high = candles[v_idx]["high"]

        # ── V point harus lokal maksimum ──
        left  = [candles[j]["high"] for j in range(max(0, v_idx - 2), v_idx)]
        right = [candles[j]["high"] for j in range(v_idx + 1, min(n, v_idx + 3))]
        if left  and v_high <= max(left):
            continue
        if right and v_high <= max(right):
            continue

        # ── Sharp rally sebelum V ──
        pre_start  = max(0, v_idx - VPATTERN_DROP_CANDLES)
        pre_low    = min(c["low"] for c in candles[pre_start:v_idx])
        rally_size = v_high - pre_low

        if rally_size < atr * VPATTERN_ATR_MULT:
            continue

        # ── Drop setelah V ──
        post_end     = min(n, v_idx + VPATTERN_RECOV_CANDLES + 1)
        post_candles = candles[v_idx + 1:post_end]
        if not post_candles:
            continue

        post_low     = min(c["low"] for c in post_candles)
        drop_size    = v_high - post_low
        reversal_pct = drop_size / rally_size

        if reversal_pct < VPATTERN_RECOV_MIN:
            continue

        # ── Early filter: recovery < 75% → langsung reject (V lemah, high risk) ──
        if reversal_pct < VPATTERN_RECOV_STRONG:
            continue

        # ── Rejection candle di titik V ──
        has_rejection, wick_ratio = _rejection_wick(candles[v_idx])

        # ── Liquidity sweep: V high > previous swing highs ──
        prev_highs = [candles[j]["high"] for j in range(max(0, v_idx - 20), v_idx - 3)]
        has_sweep  = bool(prev_highs) and v_high > max(prev_highs)

        # ── 1D bias alignment (hanya relevan untuk 4H setups) ──
        bias_aligned = _1d_bias_aligned("SHORT", candles_1d) if tf == "4h" else False

        # ── Score ──
        score, notes = _score(
            rally_size, atr, reversal_pct,
            has_rejection, wick_ratio, has_sweep, tf,
            bias_aligned=bias_aligned,
        )

        # ── Entry zone: HARUS di atas 50% drop (BELOW mid-point) — V-top valid only near the high ──
        # Late entry (price sudah drop 80%+) = bukan V-top reversal
        current_price = candles[-1]["close"]
        in_entry_zone = (v_high - drop_size * 0.50) <= current_price < v_high

        # ── Trade calculation ──
        sl      = v_high * (1 + VPATTERN_SL_BUFFER)
        tp1     = pre_low                   # Neckline
        tp2     = pre_low - rally_size      # Extension 1:1
        sl_dist = sl - current_price

        rr1 = round((current_price - tp1) / sl_dist, 2) if sl_dist > 0 else 0
        rr2 = round((current_price - tp2) / sl_dist, 2) if sl_dist > 0 else 0

        trade = {
            "entry":   round(current_price, 4),
            "sl":      round(sl, 4),
            "tp1":     round(tp1, 4),
            "tp2":     round(tp2, 4),
            "rr1":     f"1:{rr1}",
            "rr2":     f"1:{rr2}",
            "sl_pct":  round((sl - current_price) / current_price * 100, 2),
            "tp1_pct": round((current_price - tp1) / current_price * 100, 2),
            "tp2_pct": round((current_price - tp2) / current_price * 100, 2),
        } if in_entry_zone else None

        label = ("⭐⭐⭐ HIGH"   if score >= SCORE_HIGH
                 else "⭐⭐ MEDIUM" if score >= SCORE_MEDIUM
                 else "⭐ LOW")

        results.append({
            "direction":        "SHORT",
            "pattern":          "V-Top",
            "tf":               tf,
            "tf_label":         TF_LABEL[tf],
            "priority":         TF_PRIORITY[tf],
            "v_high":           round(v_high, 4),
            "pre_low":          round(pre_low, 4),
            "post_low":         round(post_low, 4),
            "rally_size":       round(rally_size, 4),
            "reversal_pct":     round(reversal_pct, 2),
            "has_rejection":    has_rejection,
            "wick_ratio":       wick_ratio,
            "has_sweep":        has_sweep,
            "atr":              round(atr, 4),
            "current_price":    current_price,
            "in_entry_zone":    in_entry_zone,
            "trade":            trade,
            "confidence_score": score,
            "confidence_label": label,
            "confluence_notes": notes,
            "bias_aligned":     bias_aligned,
        })

    return results


# ── Main scan ─────────────────────────────────────────────────

def scan_vpattern_setups(pair: str) -> list[dict]:
    """
    Scan 4H + 1D untuk V Pattern setups.
    MTF approach:
      - 4H : deteksi V pattern
      - 1D : higher bias (EMA50) → tambah confluence jika searah
      - 1D : scan V pattern sendiri (lebih reliable, lebih jarang)
    Return list setup diurutkan dari score tertinggi.
    """
    results = []

    # Fetch 1D candles sekali — dipakai sebagai bias untuk 4H setups
    try:
        candles_1d = get_candles(pair, "1d", limit=60)
    except Exception:
        candles_1d = None

    for tf in VPATTERN_TIMEFRAMES:
        try:
            limit   = 60 if tf == "4h" else 40
            candles = candles_1d if tf == "1d" else get_candles(pair, tf, limit=limit)
            if not candles or len(candles) < 20:
                continue

            atr = calculate_atr(candles)
            if atr == 0:
                continue

            # Untuk 4H: pass candles_1d sebagai HTF bias
            c1d = candles_1d if tf == "4h" else None

            for setup in _scan_v_bottom(candles, atr, tf, candles_1d=c1d):
                setup["pair"] = pair
                results.append(setup)

            for setup in _scan_v_top(candles, atr, tf, candles_1d=c1d):
                setup["pair"] = pair
                results.append(setup)

        except Exception as e:
            log.error(f"[VPATTERN ERROR] {pair} {tf}: {e}")

    results.sort(key=lambda x: x["confidence_score"], reverse=True)
    return results
