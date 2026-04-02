import time
import traceback
from datetime import datetime

from config import CRYPTO_PAIRS, SCAN_INTERVAL_SECONDS
from strategy1_liquidity import (
    get_fresh_liquidity_zones,
    get_candles,
    is_touching_zone,
    check_rejection_long,
    check_rejection_short,
    calculate_trade,
)
from telegram_bot import alert_touch, alert_entry, alert_result, alert_stats, alert_info
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
    return elapsed < ALERT_COOLDOWN

def scan_pair(pair: str):
    """Scan satu pair untuk setup Strategy 1."""
    try:
        # 1. Ambil zona liquidity fresh dari 4H
        zones = get_fresh_liquidity_zones(pair)
        all_zones = zones["LONG"] + zones["SHORT"]

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
                        log_signal(pair, direction, trade["entry"], trade["sl"], trade["tp"])
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
        f"Strategy: Liquidity Grab + Rejection\n"
        f"Pairs: {len(CRYPTO_PAIRS)} crypto pairs\n"
        f"Interval: {SCAN_INTERVAL_SECONDS}s"
    )

    scan_count = 0
    STATS_EVERY = 60  # kirim winrate setiap ~1 jam (60 x 60s)

    while True:
        scan_start = time.time()
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(CRYPTO_PAIRS)} pairs...")

        for pair in CRYPTO_PAIRS:
            scan_pair(pair)
            time.sleep(0.3)  # Rate limit Binance API

        scan_count += 1
        if scan_count % STATS_EVERY == 0:
            stats = get_stats()
            print(f"[STATS] Total={stats['total']} Win={stats['wins']} Loss={stats['losses']} WR={stats['winrate']}% Open={stats['open']}")
            alert_stats(stats)

        elapsed = time.time() - scan_start
        sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan selesai ({elapsed:.1f}s). Next scan in {sleep_time:.0f}s")
        time.sleep(sleep_time)

if __name__ == "__main__":
    run_scanner()
