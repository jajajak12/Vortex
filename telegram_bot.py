import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

SEP = "━━━━━━━━━━━━━━━━"


def _fmt_price(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "-"
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:.6f}"


def _fmt_num(value, decimals: int = 2) -> str:
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return "-"


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
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "unknown"
        print(f"[TELEGRAM ERROR] HTTP {status}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] {type(e).__name__}: {e}")

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

def alert_entry(pair: str, direction: str, entry: float, sl: float, tp: float,
                rr: str = "1:1", position_usdt: float = 0.0):
    """Alert 2: Rejection confirmed, entry signal."""
    emoji  = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    sl_pct = abs(entry - sl) / entry * 100
    tp_pct = abs(tp - entry) / entry * 100
    pos_line = (f"Size    : <b>${position_usdt:,.2f} USDT</b>\n"
                if position_usdt > 0 else "")
    msg = (
        f"✅ <b>[STRAT1] ENTRY SIGNAL — {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair    : <b>{pair}</b>\n"
        f"Entry   : <b>${entry:,.4f}</b>\n"
        f"SL      : ${sl:,.4f} ({sl_pct:.2f}%)\n"
        f"TP      : ${tp:,.4f} ({tp_pct:.2f}%)\n"
        f"RR      : {rr}\n"
        f"{pos_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"⚡ <i>Entry di open candle berikutnya</i>"
    )
    send_telegram(msg)

def alert_result(trade: dict):
    """Alert hasil trade: WIN atau LOSS."""
    result = trade.get("result", "?")
    direction = trade.get("direction", "?")
    status_emoji = "✅" if result == "WIN" else "❌"
    dir_em = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    strat = trade.get("strategy") or "?"
    entry = float(trade.get("entry") or 0)
    close_price = float(trade.get("close_price") or 0)
    sl = float(trade.get("sl") or 0)
    tp = float(trade.get("tp") or 0)
    pnl_pct = abs(close_price - entry) / entry * 100 if entry else 0
    rr = trade.get("rr")
    rr_line = f"RR plan  : 1:{_fmt_num(rr, 2)}\n" if rr else ""
    candles = trade.get("candles_to_resolve")
    held_line = f"Held     : {candles} candle\n" if candles else ""
    msg = (
        f"{status_emoji} <b>TRADE CLOSED - {result}</b>\n"
        f"{SEP}\n"
        f"{dir_em}  <b>{trade.get('pair', '?')}</b>  |  {strat}\n"
        f"Entry    : <b>${_fmt_price(entry)}</b>\n"
        f"Close    : <b>${_fmt_price(close_price)}</b> ({pnl_pct:.2f}%)\n"
        f"SL / TP  : ${_fmt_price(sl)} / ${_fmt_price(tp)}\n"
        f"{rr_line}"
        f"{held_line}"
        f"{SEP}\n"
        f"Open     : {trade.get('time', '-')}\n"
        f"Close    : {trade.get('close_time', '-')}"
    )
    send_telegram(msg)


def alert_stats(stats: dict):
    """Kirim ringkasan winrate dengan breakdown per strategi."""
    if stats["total"] == 0:
        send_telegram(
            f"📊 <b>VORTEX TRADE STATS</b>\n"
            f"{SEP}\n"
            f"Closed : 0\n"
            f"Open   : {stats.get('open', 0)}\n"
            f"Status : history trade baru dimulai"
        )
        return
    bar_filled = int(stats["winrate"] / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    # Breakdown per strategi
    strat_lines = ""
    for s, d in sorted(stats.get("by_strategy", {}).items()):
        if d["total"] > 0:
            strat_lines += (f"  [{s}] {d['wins']}W {d['losses']}L "
                            f"— WR {d['winrate']}%\n")
    if not strat_lines:
        strat_lines = "  belum ada closed trade\n"

    msg = (
        f"📊 <b>VORTEX TRADE STATS</b>\n"
        f"{SEP}\n"
        f"Closed   : {stats['total']}\n"
        f"Win/Loss : {stats['wins']}W / {stats['losses']}L\n"
        f"Winrate  : <b>{stats['winrate']}%</b>\n"
        f"[{bar}]\n"
        f"Open     : {stats['open']}\n"
        f"{SEP}\n"
        f"<b>Per Strategy</b>\n{strat_lines}"
    )
    send_telegram(msg)


def alert_info(message: str):
    """Alert informasi umum."""
    send_telegram(f"ℹ️ {message}")
