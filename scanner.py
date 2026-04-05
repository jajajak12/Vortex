import time
import traceback
from datetime import datetime

from config import CRYPTO_PAIRS, SCAN_INTERVAL_SECONDS, ENABLE_MACRO_FILTER, SIGNAL_RATE_MIN, STRAT3_MIN_SCORE
from strategy1_liquidity import (
    get_fresh_liquidity_zones,
    get_candles,
    get_btc_macro_regime,
    is_touching_zone,
    check_rejection_long,
    check_rejection_short,
    calculate_trade,
)
from strategy2_wick import scan_wick_setups
from strategy3_fvg import scan_fvg_setups
from telegram_bot import alert_touch, alert_entry, alert_result, alert_stats, alert_info
from wick_alerts import alert_wick_detected, alert_wick_entry
from fvg_alerts import alert_fvg_detected, alert_fvg_entry
from trade_tracker import log_signal, update_trades_for_pair, get_stats

# ── State tracker ────────────────────────────────────────────
# Mencegah alert duplikat untuk zona yang sama
alerted_touch  = {}   # key: f"{pair}_{zone_type}_{zone_pivot}" → timestamp
alerted_entry  = {}   # key: f"{pair}_{zone_type}_{zone_pivot}" → timestamp
ALERT_COOLDOWN = 4 * 60 * 60  # 4 jam cooldown per zona

def cooldown_key(pair: str, zone: dict) -> str:
    return f"{pair}_{zone['type']}_{zone['pivot']:.4f}"

def is_on_cooldown(store: dict, key: str) -> bool:
    if key not in store:
        return False
    elapsed = time.time() - store[key]
    if elapsed >= ALERT_COOLDOWN:
        del store[key]
        return False
    return True

# ── Signal rate monitoring (overfitting detection) ────────────
# key: "YYYY-MM-DD" → {pair: count}
daily_signal_counts: dict[str, dict[str, int]] = {}

def _track_signal(pair: str):
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in daily_signal_counts:
        daily_signal_counts[today] = {}
    daily_signal_counts[today][pair] = daily_signal_counts[today].get(pair, 0) + 1

def _check_signal_rate():
    """Log warning jika signal rate hari ini terlalu rendah (indikasi overfitting)."""
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in daily_signal_counts:
        return
    low_pairs = [p for p in CRYPTO_PAIRS
                 if daily_signal_counts[today].get(p, 0) < SIGNAL_RATE_MIN]
    if low_pairs:
        print(f"[⚠️  SIGNAL RATE] Pairs with <{SIGNAL_RATE_MIN} signal hari ini: {low_pairs}")

alerted_wick_detected = {}  # key: f"{pair}_{tf}_{wick_low}" → timestamp
alerted_wick_entry    = {}

def wick_key(pair: str, tf: str, wick_low: float) -> str:
    return f"{pair}_{tf}_{wick_low:.4f}"

def scan_pair_wick(pair: str, btc_macro: str = "BULL", wick_setups: list | None = None):
    """Scan Strategy 2: Wick Fill setups. Terima pre-computed setups untuk efisiensi."""
    try:
        setups = wick_setups if wick_setups is not None else scan_wick_setups(pair)

        for setup in setups:
            wk = wick_key(pair, setup["tf"], setup["wick"]["wick_low"])

            # Alert 1: Wick terdeteksi (kirim sekali per wick)
            if not is_on_cooldown(alerted_wick_detected, wk):
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🕯️  WICK: {pair} {setup['tf_label']} | Low={setup['wick']['wick_low']} | {setup['confluence_label']}")
                alert_wick_detected(setup)
                alerted_wick_detected[wk] = time.time()

            # Alert 2: Harga masuk entry zone + konfirmasi rejection 5m
            # Skip wick LONG jika macro BTC sedang BEAR
            if ENABLE_MACRO_FILTER and btc_macro == "BEAR":
                continue

            if setup["in_entry_zone"] and not is_on_cooldown(alerted_wick_entry, wk):
                candles_5m = get_candles(pair, "5m", limit=50)
                wick_zone  = {
                    "low":   setup["wick"]["wick_low"],
                    "high":  setup["wick"]["wick_50pct"],
                    "pivot": setup["wick"]["wick_low"],
                }
                rejection = check_rejection_long(candles_5m, wick_zone)
                if rejection and rejection["confirmed"]:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ WICK ENTRY: {pair} {setup['tf_label']} @ {setup['current_price']}")
                    alert_wick_entry(setup)
                    alerted_wick_entry[wk] = time.time()

    except Exception as e:
        print(f"[WICK SCAN ERROR] {pair}: {e}")
        traceback.print_exc()

alerted_fvg_detected = {}  # key: f"{pair}_{direction}_{fvg_low}" → timestamp
alerted_fvg_entry    = {}

def fvg_key(pair: str, direction: str, fvg_low: float) -> str:
    return f"{pair}_{direction}_{fvg_low:.4f}"

def scan_pair_fvg(pair: str, btc_macro: str = "BULL", wick_setups: list | None = None):
    """Scan Strategy 3: FVG Reclaim after Liquidity Sweep."""
    try:
        setups = scan_fvg_setups(pair, wick_setups=wick_setups)

        for setup in setups:
            # Filter score minimum
            if setup["confluence_score"] < STRAT3_MIN_SCORE:
                continue

            fk = fvg_key(pair, setup["direction"], setup["fvg"]["fvg_low"])

            # Skip setup yang berlawanan dengan macro BTC (khusus LONG saat BEAR)
            if ENABLE_MACRO_FILTER and setup["direction"] == "LONG" and btc_macro == "BEAR":
                continue
            if ENABLE_MACRO_FILTER and setup["direction"] == "SHORT" and btc_macro == "BULL":
                continue

            # Alert 1: FVG zone terdeteksi (kirim sekali per FVG)
            if not is_on_cooldown(alerted_fvg_detected, fk):
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔷 FVG: {pair} {setup['tf_label']} {setup['direction']} | Zone {setup['fvg']['fvg_low']}-{setup['fvg']['fvg_high']} | Score={setup['confluence_score']}")
                alert_fvg_detected(setup)
                alerted_fvg_detected[fk] = time.time()

            # Alert 2: Harga masuk FVG zone + konfirmasi rejection 5m
            if setup["in_fvg_zone"] and not is_on_cooldown(alerted_fvg_entry, fk):
                candles_5m = get_candles(pair, "5m", limit=50)
                fvg_zone = {
                    "low":   setup["fvg"]["fvg_low"],
                    "high":  setup["fvg"]["fvg_high"],
                    "pivot": setup["fvg"]["fvg_mid"],
                }
                if setup["direction"] == "LONG":
                    rejection = check_rejection_long(candles_5m, fvg_zone)
                else:
                    rejection = check_rejection_short(candles_5m, fvg_zone)

                if rejection and rejection["confirmed"]:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ FVG ENTRY: {pair} {setup['direction']} @ {setup['current_price']} | Score={setup['confluence_score']}")
                    alert_fvg_entry(setup)
                    log_signal(
                        pair, setup["direction"],
                        setup["trade"]["entry"], setup["trade"]["sl"], setup["trade"]["tp2"],
                        confluence_score=setup["confluence_score"],
                        regime_state=btc_macro,
                        strategy="S3"
                    )
                    _track_signal(pair)
                    alerted_fvg_entry[fk] = time.time()

    except Exception as e:
        print(f"[FVG SCAN ERROR] {pair}: {e}")
        traceback.print_exc()

def scan_pair(pair: str, btc_macro: str = "BULL"):
    """Scan satu pair untuk setup Strategy 1."""
    try:
        # 1. Ambil zona liquidity fresh dari 4H + HTF bias
        zones     = get_fresh_liquidity_zones(pair)
        htf_bias  = zones["htf_bias"]

        # Filter zona: harus searah HTF bias pair DAN macro regime BTC
        if ENABLE_MACRO_FILTER:
            valid_dir = htf_bias if htf_bias == btc_macro else None
        else:
            valid_dir = htf_bias

        if valid_dir is None:
            return  # HTF pair berlawanan dengan macro BTC, skip

        all_zones = [z for z in zones["LONG"] + zones["SHORT"] if z["type"] == valid_dir]

        if not all_zones:
            return

        # 2. Ambil harga terkini dari candle 30m terakhir
        candles_30m = get_candles(pair, "30m", limit=5)
        current_price = candles_30m[-1]["close"]

        # Cek apakah ada open trade yang sudah hit TP/SL
        closed_trades = update_trades_for_pair(pair, current_price)
        for ct in closed_trades:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {'✅ WIN' if ct['result'] == 'WIN' else '❌ LOSS'}: {pair} {ct['direction']} | Close={ct['close_price']}")
            alert_result(ct)

        for zone in all_zones:
            ckey = cooldown_key(pair, zone)
            direction = zone["type"]

            # ── ALERT 1: Touch check ─────────────────────────
            if is_touching_zone(current_price, zone):
                if not is_on_cooldown(alerted_touch, ckey):
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️  TOUCH: {pair} @ {current_price:.4f} | Zone: {zone['type']} {zone['pivot']:.4f}")
                    alert_touch(
                        pair=pair,
                        price=current_price,
                        zone_low=zone["low"],
                        zone_high=zone["high"],
                        direction=direction
                    )
                    alerted_touch[ckey] = time.time()

                # ── ALERT 2: Rejection check ─────────────────
                if not is_on_cooldown(alerted_entry, ckey):
                    candles_5m = get_candles(pair, "5m", limit=50)

                    rejection = None
                    if direction == "LONG":
                        rejection = check_rejection_long(candles_5m, zone)
                    elif direction == "SHORT":
                        rejection = check_rejection_short(candles_5m, zone)

                    if rejection and rejection["confirmed"]:
                        other_zones = zones["LONG"] if direction == "LONG" else zones["SHORT"]
                        prev_liquidity = _find_prev_liquidity(zone, other_zones, direction)

                        trade = calculate_trade(
                            direction=direction,
                            entry=rejection["entry_price"],
                            zone=zone,
                            prev_liquidity_price=prev_liquidity
                        )

                        vol_note = " 🔥 Volume spike!" if rejection.get("volume_spike") else ""
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ ENTRY: {pair} {direction} | Entry={trade['entry']} SL={trade['sl']} TP={trade['tp']}{vol_note}")

                        alert_entry(
                            pair=pair,
                            direction=direction,
                            entry=trade["entry"],
                            sl=trade["sl"],
                            tp=trade["tp"],
                            rr=trade["rr"]
                        )
                        log_signal(pair, direction, trade["entry"], trade["sl"], trade["tp"],
                                   regime_state=btc_macro, strategy="S1")
                        _track_signal(pair)
                        alerted_entry[ckey] = time.time()

    except Exception as e:
        print(f"[ERROR] {pair}: {e}")
        traceback.print_exc()

def _find_prev_liquidity(current_zone: dict, all_zones_same_type: list, direction: str) -> float:
    """
    Cari liquidity sebelumnya untuk penentuan SL.
    Untuk LONG: cari swing low yang lebih rendah dari zona saat ini.
    Untuk SHORT: cari swing high yang lebih tinggi dari zona saat ini.
    """
    pivot = current_zone["pivot"]

    if direction == "LONG":
        candidates = [z["pivot"] for z in all_zones_same_type if z["pivot"] < pivot]
        return min(candidates) if candidates else pivot * 0.97  # fallback -3%
    else:
        candidates = [z["pivot"] for z in all_zones_same_type if z["pivot"] > pivot]
        return max(candidates) if candidates else pivot * 1.03  # fallback +3%

def run_scanner():
    """Main loop — scan semua pair setiap SCAN_INTERVAL_SECONDS."""
    print("=" * 50)
    print("🤖 TRADING AGENT — Strategy 1: Liquidity Grab")
    print(f"📊 Scanning {len(CRYPTO_PAIRS)} pairs")
    print(f"⏱️  Interval: {SCAN_INTERVAL_SECONDS}s")
    print("=" * 50)

    alert_info(
        f"🤖 Trading Agent aktif!\n"
        f"Strategy 1: Liquidity Grab + Rejection\n"
        f"Strategy 2: Wick Fill (1W/1D/4H)\n"
        f"Pairs: {len(CRYPTO_PAIRS)} crypto pairs\n"
        f"Interval: {SCAN_INTERVAL_SECONDS}s"
    )

    scan_count = 0
    STATS_EVERY = 60  # kirim winrate setiap ~1 jam (60 x 60s)

    while True:
        scan_start = time.time()
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(CRYPTO_PAIRS)} pairs...")

        # Compute macro regime sekali per siklus scan
        try:
            btc_macro = get_btc_macro_regime() if ENABLE_MACRO_FILTER else "BULL"
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 🌍 Macro: BTC {'🟢 BULL' if btc_macro == 'BULL' else '🔴 BEAR'}")
        except Exception as e:
            print(f"[MACRO ERROR] {e} — defaulting to BULL")
            btc_macro = "BULL"

        for pair in CRYPTO_PAIRS:
            # Compute wick setups sekali — dipakai Strat 2 dan Strat 3 (confluence)
            try:
                wick_setups = scan_wick_setups(pair)
            except Exception as e:
                print(f"[WICK INIT ERROR] {pair}: {e}")
                wick_setups = []

            scan_pair(pair, btc_macro)                              # Strat 1
            scan_pair_wick(pair, btc_macro, wick_setups=wick_setups)  # Strat 2
            scan_pair_fvg(pair, btc_macro, wick_setups=wick_setups)   # Strat 3
            time.sleep(0.3)  # Rate limit Binance API

        scan_count += 1
        if scan_count % STATS_EVERY == 0:
            stats = get_stats()
            print(f"[STATS] Total={stats['total']} Win={stats['wins']} Loss={stats['losses']} WR={stats['winrate']}% Open={stats['open']}")
            alert_stats(stats)
            _check_signal_rate()

        elapsed = time.time() - scan_start
        sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan selesai ({elapsed:.1f}s). Next scan in {sleep_time:.0f}s")
        time.sleep(sleep_time)

if __name__ == "__main__":
    run_scanner()
