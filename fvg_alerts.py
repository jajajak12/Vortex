from telegram_bot import send_telegram


def alert_fvg_detected(setup: dict):
    """Alert: FVG zone terdeteksi setelah liquidity sweep."""
    fvg   = setup["fvg"]
    sweep = setup["sweep"]
    dir   = setup["direction"]
    emoji = "🟢" if dir == "LONG" else "🔴"
    notes = "\n".join(setup["confluence_notes"])

    sweep_ref = sweep.get("sweep_low") or sweep.get("sweep_high")

    msg = (
        f"🔷 <b>[STRAT3] FVG RECLAIM SETUP — {emoji} {dir}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair     : <b>{setup['pair']}</b> ({setup['tf_label']})\n"
        f"Score    : <b>{setup['confluence_score']}/10</b> {setup['confluence_label']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Sweep {'Low' if dir == 'LONG' else 'High'} : ${sweep_ref:,.4f}\n"
        f"FVG Zone : ${fvg['fvg_low']:,.4f} – ${fvg['fvg_high']:,.4f}\n"
        f"FVG Mid  : <b>${fvg['fvg_mid']:,.4f}</b> (target entry)\n"
        f"ATR      : ${setup['atr']:,.4f}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{notes}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⏳ <i>Tunggu retrace ke FVG zone + konfirmasi 5m rejection</i>"
    )
    send_telegram(msg)


def alert_fvg_entry(setup: dict, position_usdt: float = 0.0):
    """Alert: Harga masuk FVG zone, rejection 5m confirmed."""
    fvg   = setup["fvg"]
    t     = setup["trade"]
    dir   = setup["direction"]
    emoji = "🟢 LONG" if dir == "LONG" else "🔴 SHORT"
    notes = "\n".join(setup["confluence_notes"])

    pos_line = f"Size     : <b>${position_usdt:,.2f} USDT</b>\n" if position_usdt > 0 else ""
    msg = (
        f"✅ <b>[STRAT3] FVG ENTRY — {emoji}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Pair     : <b>{setup['pair']}</b> ({setup['tf_label']})\n"
        f"Score    : <b>{setup['confluence_score']}/10</b> {setup['confluence_label']}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Entry    : <b>${t['entry']:,.4f}</b>\n"
        f"SL       : ${t['sl']:,.4f} ({t['sl_pct']}%)\n"
        f"TP1 (FVG): ${t['tp1']:,.4f} (+{t['tp1_pct']}%) | RR {t['rr1']}\n"
        f"TP2 (1:2): ${t['tp2']:,.4f} (+{t['tp2_pct']}%) | RR {t['rr2']}\n"
        f"FVG Zone : ${fvg['fvg_low']:,.4f} – ${fvg['fvg_high']:,.4f}\n"
        f"{pos_line}"
        f"━━━━━━━━━━━━━━━\n"
        f"{notes}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Invalid jika close di bawah ${fvg['fvg_low']:,.4f}</i>"
    )
    send_telegram(msg)
