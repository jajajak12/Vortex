from telegram_bot import send_telegram


def alert_wick_detected(setup: dict):
    """Alert: Wick panjang terdeteksi di TF tinggi — LONG atau SHORT."""
    w         = setup["wick"]
    direction = setup.get("direction", "LONG")
    emoji     = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    e         = setup["ema_info"]
    ema_str   = f"${e['ema_value']}" if e["ema_value"] else "N/A"

    if direction == "LONG":
        wick_ref  = f"Wick Low  : ${w['wick_low']}"
        invalid_msg = f"⏳ <i>Tunggu price revisit area wick low / 50% level</i>"
    else:
        wick_ref  = f"Wick High : ${w['wick_high']}"
        invalid_msg = f"⏳ <i>Tunggu price revisit area wick high / 50% level</i>"

    msg = (
        f"🕯️ <b>[S2] WICK DETECTED — {setup['tf_label']} {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair      : <b>{setup['pair']}</b>\n"
        f"Priority  : {setup['priority']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{wick_ref}\n"
        f"50% Fill  : ${w['wick_50pct']}\n"
        f"100% Fill : ${w['wick_100pct']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Wick Size : {w['wick_body_ratio']}x body | {int(w['wick_range_ratio']*100)}% range\n"
        f"EMA50     : {ema_str}\n"
        f"Confluence: {setup['confluence_label']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{invalid_msg}"
    )
    send_telegram(msg)


def alert_wick_entry(setup: dict, position_usdt: float = 0.0):
    """Alert: Harga masuk entry zone wick fill — LONG atau SHORT."""
    w         = setup["wick"]
    t         = setup["trade"]
    direction = setup.get("direction", "LONG")
    emoji     = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    notes     = "\n".join(setup["confluence_notes"])

    if direction == "LONG":
        invalid_level = w["wick_low"]
        invalid_msg   = f"⚠️ <i>Invalid jika close di bawah ${invalid_level}</i>"
    else:
        invalid_level = w["wick_high"]
        invalid_msg   = f"⚠️ <i>Invalid jika close di atas ${invalid_level}</i>"

    pos_line = f"Size      : <b>${position_usdt:,.2f} USDT</b>\n" if position_usdt > 0 else ""
    msg = (
        f"✅ <b>[STRAT2] WICK FILL ENTRY — {setup['tf_label']} {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair      : <b>{setup['pair']}</b>\n"
        f"Priority  : {setup['priority']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Entry     : <b>${t['entry']}</b>\n"
        f"SL        : ${t['sl']} ({t['sl_pct']}%)\n"
        f"TP1 (50%) : ${t['tp1']} ({t['tp1_pct']}%) | RR {t['rr1']}\n"
        f"TP2 (100%): ${t['tp2']} ({t['tp2_pct']}%) | RR {t['rr2']}\n"
        f"{pos_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"Confluence: {setup['confluence_label']}\n"
        f"{notes}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{invalid_msg}"
    )
    send_telegram(msg)
