from telegram_bot import send_telegram

def alert_wick_detected(setup: dict):
    """Alert: Wick panjang terdeteksi di TF tinggi."""
    w = setup["wick"]
    e = setup["ema_info"]
    ema_str = f"${e['ema_value']}" if e["ema_value"] else "N/A"

    msg = (
        f"🕯️ <b>WICK DETECTED — {setup['tf_label']}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair      : <b>{setup['pair']}</b>\n"
        f"Priority  : {setup['priority']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Wick Low  : ${w['wick_low']}\n"
        f"50% Fill  : ${w['wick_50pct']}\n"
        f"100% Fill : ${w['wick_100pct']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Wick Size : {w['wick_body_ratio']}x body | {int(w['wick_range_ratio']*100)}% range\n"
        f"1W50 EMA  : {ema_str}\n"
        f"Confluence: {setup['confluence_label']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⏳ <i>Tunggu price revisit area wick low / 50% level</i>"
    )
    send_telegram(msg)

def alert_wick_entry(setup: dict):
    """Alert: Harga masuk entry zone wick fill."""
    w = setup["wick"]
    t = setup["trade"]
    notes = "\n".join(setup["confluence_notes"])

    msg = (
        f"✅ <b>[STRAT2] WICK FILL ENTRY — {setup['tf_label']}</b> 🟢\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair      : <b>{setup['pair']}</b>\n"
        f"Priority  : {setup['priority']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Entry     : <b>${t['entry']}</b>\n"
        f"SL        : ${t['sl']} ({t['sl_pct']}%)\n"
        f"TP1 (50%) : ${t['tp1']} (+{t['tp1_pct']}%) | RR {t['rr1']}\n"
        f"TP2 (100%): ${t['tp2']} (+{t['tp2_pct']}%) | RR {t['rr2']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Confluence: {setup['confluence_label']}\n"
        f"{notes}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Invalid jika close di bawah ${w['wick_low']}</i>"
    )
    send_telegram(msg)
