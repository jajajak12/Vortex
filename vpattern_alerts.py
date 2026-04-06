from telegram_bot import send_telegram


def alert_vpattern_detected(setup: dict):
    """Alert: V Pattern terdeteksi — belum masuk entry zone."""
    direction = setup["direction"]
    emoji     = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    pattern   = setup["pattern"]

    if direction == "LONG":
        ref_label  = "V Low"
        ref_price  = setup["v_low"]
        neckline   = setup["pre_high"]
        move_info  = f"Recovery : {int(setup['reversal_pct']*100)}% dari drop"
    else:
        ref_label  = "V High"
        ref_price  = setup["v_high"]
        neckline   = setup["pre_low"]
        move_info  = f"Drop     : {int(setup['reversal_pct']*100)}% dari rally"

    sweep_str = "✅ Ya" if setup["has_sweep"]     else "❌ Tidak"
    rej_str   = "✅ Ya" if setup["has_rejection"] else "❌ Tidak"
    bias_str  = "✅ Aligned" if setup.get("bias_aligned") else "⬜ Tidak konfirmasi"
    notes_str = "\n".join(setup["confluence_notes"])

    msg = (
        f"📐 <b>[S4] V PATTERN — {setup['tf_label']} {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair      : <b>{setup['pair']}</b>\n"
        f"Pattern   : {pattern}\n"
        f"Priority  : {setup['priority']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{ref_label:<9}: ${ref_price}\n"
        f"Neckline  : ${neckline}\n"
        f"ATR       : ${setup['atr']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Sweep     : {sweep_str}\n"
        f"Rejection : {rej_str}\n"
        f"1D Bias   : {bias_str}\n"
        f"{move_info}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Score     : {setup['confidence_score']}/10 {setup['confidence_label']}\n"
        f"{notes_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⏳ <i>Tunggu harga masuk entry zone + konfirmasi 30m</i>"
    )
    send_telegram(msg)


def alert_vpattern_entry(setup: dict, position_usdt: float = 0.0):
    """Alert: V Pattern entry signal confirmed (rejection 30m)."""
    t         = setup["trade"]
    direction = setup["direction"]
    emoji     = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
    pattern   = setup["pattern"]

    if direction == "LONG":
        invalid_level = setup["v_low"]
        invalid_msg   = f"⚠️ <i>Invalid jika close di bawah ${invalid_level}</i>"
    else:
        invalid_level = setup["v_high"]
        invalid_msg   = f"⚠️ <i>Invalid jika close di atas ${invalid_level}</i>"

    pos_line  = f"Size      : <b>${position_usdt:,.2f} USDT</b>\n" if position_usdt > 0 else ""
    notes_str = "\n".join(setup["confluence_notes"])
    bias_str  = "✅ Aligned" if setup.get("bias_aligned") else "⬜ Tidak konfirmasi"

    msg = (
        f"✅ <b>[S4] V PATTERN ENTRY — {setup['tf_label']} {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair      : <b>{setup['pair']}</b>\n"
        f"Pattern   : {pattern}\n"
        f"Priority  : {setup['priority']}\n"
        f"1D Bias   : {bias_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Entry     : <b>${t['entry']}</b>\n"
        f"SL        : ${t['sl']} ({t['sl_pct']}%)\n"
        f"TP1 (neck): ${t['tp1']} ({t['tp1_pct']}%) | RR {t['rr1']}\n"
        f"TP2 (ext) : ${t['tp2']} ({t['tp2_pct']}%) | RR {t['rr2']}\n"
        f"{pos_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"Score     : {setup['confidence_score']}/10 {setup['confidence_label']}\n"
        f"{notes_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{invalid_msg}"
    )
    send_telegram(msg)
