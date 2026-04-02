import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def send_telegram(message: str):
    """Kirim pesan ke Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")

def alert_touch(pair: str, price: float, zone_low: float, zone_high: float, direction: str):
    """Alert 1: Harga menyentuh zona liquidity."""
    emoji = "📉" if direction == "SHORT" else "📈"
    msg = (
        f"⚠️ <b>LIQUIDITY TOUCH</b> {emoji}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair    : <b>{pair}</b>\n"
        f"Harga   : <b>${price:,.4f}</b>\n"
        f"Zona    : ${zone_low:,.4f} – ${zone_high:,.4f}\n"
        f"Setup   : {direction}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⏳ <i>Pantau rejection di TF 5m...</i>"
    )
    send_telegram(msg)

def alert_entry(pair: str, direction: str, entry: float, sl: float, tp: float, rr: str = "1:1"):
    """Alert 2: Rejection confirmed, entry signal."""
    emoji = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    sl_pct  = abs(entry - sl) / entry * 100
    tp_pct  = abs(tp - entry) / entry * 100
    msg = (
        f"✅ <b>ENTRY SIGNAL — {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair    : <b>{pair}</b>\n"
        f"Entry   : <b>${entry:,.4f}</b>\n"
        f"SL      : ${sl:,.4f} ({sl_pct:.2f}%)\n"
        f"TP      : ${tp:,.4f} ({tp_pct:.2f}%)\n"
        f"RR      : {rr}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚡ <i>Entry di open candle berikutnya</i>"
    )
    send_telegram(msg)

def alert_result(trade: dict):
    """Alert hasil trade: WIN atau LOSS."""
    emoji  = "✅ WIN" if trade["result"] == "WIN" else "❌ LOSS"
    dir_em = "🟢 LONG" if trade["direction"] == "LONG" else "🔴 SHORT"
    pnl_pct = abs(trade["close_price"] - trade["entry"]) / trade["entry"] * 100
    msg = (
        f"{emoji} — <b>{trade['pair']}</b> {dir_em}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Entry      : ${trade['entry']:,.4f}\n"
        f"Close      : ${trade['close_price']:,.4f} ({pnl_pct:.2f}%)\n"
        f"SL / TP    : ${trade['sl']:,.4f} / ${trade['tp']:,.4f}\n"
        f"Waktu open : {trade['time']}\n"
        f"Waktu close: {trade['close_time']}"
    )
    send_telegram(msg)


def alert_stats(stats: dict):
    """Kirim ringkasan winrate."""
    if stats["total"] == 0:
        send_telegram("📊 <b>Winrate Tracker</b>\nBelum ada trade selesai.")
        return
    bar_filled = int(stats["winrate"] / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    msg = (
        f"📊 <b>WINRATE TRACKER</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Total closed : {stats['total']}\n"
        f"Win          : {stats['wins']} ✅\n"
        f"Loss         : {stats['losses']} ❌\n"
        f"Winrate      : <b>{stats['winrate']}%</b>\n"
        f"[{bar}]\n"
        f"Open trades  : {stats['open']}\n"
        f"━━━━━━━━━━━━━━━"
    )
    send_telegram(msg)


def alert_info(message: str):
    """Alert informasi umum."""
    send_telegram(f"ℹ️ {message}")
