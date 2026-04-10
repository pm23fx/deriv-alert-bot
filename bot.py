"""
PM23FX Deriv Alert Bot v2
- Interactive inline keyboard for pair selection
- Streams live ticks from Deriv WebSocket API
- Custom price alerts via Telegram
- All forex pairs + all Deriv synthetic indices
- Runs 24/7 on Railway free tier
"""

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import aiohttp
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ── Config ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("DerivBot")

# ── Conversation states ─────────────────────────────────────────────────
PICK_CATEGORY, PICK_PAIR, PICK_DIRECTION, ENTER_PRICE = range(4)
PRICE_PICK_PAIR = 10

# ── Symbol Registry ─────────────────────────────────────────────────────
SYMBOL_CATEGORIES = {
    "💱 Forex Majors": [
        "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxUSDCHF",
        "frxAUDUSD", "frxNZDUSD", "frxUSDCAD",
    ],
    "💱 EUR Crosses": [
        "frxEURAUD", "frxEURCAD", "frxEURCHF", "frxEURGBP",
        "frxEURJPY", "frxEURNZD",
    ],
    "💱 GBP Crosses": [
        "frxGBPAUD", "frxGBPCAD", "frxGBPCHF", "frxGBPJPY", "frxGBPNZD",
    ],
    "💱 AUD/NZD Crosses": [
        "frxAUDCAD", "frxAUDCHF", "frxAUDJPY", "frxAUDNZD",
        "frxNZDCAD", "frxNZDCHF", "frxNZDJPY",
    ],
    "💱 USD Exotics": [
        "frxUSDMXN", "frxUSDNOK", "frxUSDPLN", "frxUSDSEK", "frxUSDZAR",
    ],
    "🥇 Metals": [
        "frxXAUUSD", "frxXAGUSD", "frxXPDUSD", "frxXPTUSD",
    ],
    "📈 Vol 10-50": [
        "R_10", "R_25", "R_50",
    ],
    "📈 Vol 75-250": [
        "R_75", "R_100", "R_150", "R_200", "R_250",
    ],
    "📈 Vol 1s (10-50)": [
        "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V", "1HZ50V",
    ],
    "📈 Vol 1s (75-300)": [
        "1HZ75V", "1HZ90V", "1HZ100V", "1HZ150V", "1HZ200V", "1HZ250V", "1HZ300V",
    ],
    "💥 Crash / Boom": [
        "CRASH300N", "CRASH500N", "CRASH1000N",
        "BOOM300N", "BOOM500N", "BOOM1000N",
    ],
    "🦘 Jump Indices": [
        "JD10", "JD25", "JD50", "JD75", "JD100",
    ],
    "🪜 Step Indices": [
        "stpRNG", "stpRNG2", "stpRNG3", "stpRNG4", "stpRNG5",
    ],
    "🔀 Drift Switch": [
        "DSI10", "DSI20", "DSI30",
    ],
}

ALL_SYMBOLS = []
for syms in SYMBOL_CATEGORIES.values():
    ALL_SYMBOLS.extend(syms)
ALL_SYMBOLS = list(dict.fromkeys(ALL_SYMBOLS))

SYMBOL_NAMES = {}
# Friendly names for all symbols
FRIENDLY = {
    # Standard Volatility
    "R_10": "V10", "R_25": "V25", "R_50": "V50",
    "R_75": "V75", "R_100": "V100", "R_150": "V150", "R_200": "V200", "R_250": "V250",
    # 1-second Volatility
    "1HZ10V": "V10 (1s)", "1HZ15V": "V15 (1s)", "1HZ25V": "V25 (1s)",
    "1HZ30V": "V30 (1s)", "1HZ50V": "V50 (1s)", "1HZ75V": "V75 (1s)",
    "1HZ90V": "V90 (1s)", "1HZ100V": "V100 (1s)",
    "1HZ150V": "V150 (1s)", "1HZ200V": "V200 (1s)", "1HZ250V": "V250 (1s)", "1HZ300V": "V300 (1s)",
    # Crash / Boom
    "CRASH300N": "Crash 300", "CRASH500N": "Crash 500", "CRASH1000N": "Crash 1000",
    "BOOM300N": "Boom 300", "BOOM500N": "Boom 500", "BOOM1000N": "Boom 1000",
    # Jump
    "JD10": "Jump 10", "JD25": "Jump 25", "JD50": "Jump 50", "JD75": "Jump 75", "JD100": "Jump 100",
    # Step
    "stpRNG": "Step Index", "stpRNG2": "Step 200", "stpRNG3": "Step 300",
    "stpRNG4": "Step 400", "stpRNG5": "Step 500",
    # Drift Switch
    "DSI10": "Drift 10", "DSI20": "Drift 20", "DSI30": "Drift 30",
}
for s in ALL_SYMBOLS:
    if s in FRIENDLY:
        SYMBOL_NAMES[s] = FRIENDLY[s]
    elif s.startswith("frx"):
        pair = s.replace("frx", "")
        if pair.startswith("X"):
            SYMBOL_NAMES[s] = pair
        else:
            SYMBOL_NAMES[s] = f"{pair[:3]}/{pair[3:]}"
    else:
        SYMBOL_NAMES[s] = s

REVERSE_LOOKUP = {}
for sym, name in SYMBOL_NAMES.items():
    REVERSE_LOOKUP[name.upper()] = sym
    REVERSE_LOOKUP[name.upper().replace("/", "")] = sym
    REVERSE_LOOKUP[sym.upper()] = sym
    clean = sym.replace("frx", "").upper()
    REVERSE_LOOKUP[clean] = sym


def resolve_symbol(user_input: str) -> str | None:
    key = user_input.strip().upper().replace("/", "")
    if key in REVERSE_LOOKUP:
        return REVERSE_LOOKUP[key]
    for name, sym in REVERSE_LOOKUP.items():
        if key in name:
            return sym
    return None


# ── Database ────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "alerts.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            target_price REAL NOT NULL,
            created_at TEXT NOT NULL,
            triggered INTEGER DEFAULT 0,
            triggered_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    log.info("Database initialized")


def add_alert(symbol: str, direction: str, price: float) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO alerts (symbol, direction, target_price, created_at) VALUES (?, ?, ?, ?)",
        (symbol, direction, price, datetime.now(timezone.utc).isoformat()),
    )
    alert_id = cur.lastrowid
    conn.commit()
    conn.close()
    return alert_id


def get_active_alerts():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, symbol, direction, target_price FROM alerts WHERE triggered = 0"
    ).fetchall()
    conn.close()
    return rows


def mark_triggered(alert_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alerts SET triggered = 1, triggered_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), alert_id),
    )
    conn.commit()
    conn.close()


def delete_alert(alert_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def delete_all_alerts() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM alerts WHERE triggered = 0")
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


# ── Global state ────────────────────────────────────────────────────────
latest_prices: dict[str, float] = {}
telegram_app: Application = None


# ══════════════════════════════════════════════════════════════════════════
#  INTERACTIVE ALERT FLOW:  /setalert → Category → Pair → Direction → Price
# ══════════════════════════════════════════════════════════════════════════

async def cmd_setalert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END

    buttons = []
    cats = list(SYMBOL_CATEGORIES.keys())
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"cat:{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"cat:{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await update.message.reply_text(
        "📊 *Set Price Alert*\nPick a category:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PICK_CATEGORY


async def pick_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    category = query.data.replace("cat:", "")
    symbols = SYMBOL_CATEGORIES.get(category, [])

    buttons = []
    for i in range(0, len(symbols), 2):
        row = []
        for sym in symbols[i:i + 2]:
            name = SYMBOL_NAMES.get(sym, sym)
            price = latest_prices.get(sym)
            label = f"{name} ({price})" if price else name
            row.append(InlineKeyboardButton(label, callback_data=f"pair:{sym}"))
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton("⬅️ Back", callback_data="back_to_cats"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    ])

    await query.edit_message_text(
        f"📊 *{category}*\nSelect a pair:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PICK_PAIR


async def back_to_cats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    buttons = []
    cats = list(SYMBOL_CATEGORIES.keys())
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"cat:{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"cat:{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await query.edit_message_text(
        "📊 *Set Price Alert*\nPick a category:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PICK_CATEGORY


async def pick_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END
    if query.data == "back_to_cats":
        return await back_to_cats(update, ctx)

    symbol = query.data.replace("pair:", "")
    ctx.user_data["alert_symbol"] = symbol
    name = SYMBOL_NAMES.get(symbol, symbol)
    price = latest_prices.get(symbol)
    price_str = f"\nCurrent price: *{price}*" if price else ""

    buttons = [
        [
            InlineKeyboardButton("🔺 Above", callback_data="dir:above"),
            InlineKeyboardButton("🔻 Below", callback_data="dir:below"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ]

    await query.edit_message_text(
        f"📊 *{name}*{price_str}\n\nAlert when price goes:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PICK_DIRECTION


async def pick_direction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    direction = query.data.replace("dir:", "")
    ctx.user_data["alert_direction"] = direction
    symbol = ctx.user_data["alert_symbol"]
    name = SYMBOL_NAMES.get(symbol, symbol)
    price = latest_prices.get(symbol)
    price_str = f" (current: {price})" if price else ""
    arrow = "🔺" if direction == "above" else "🔻"

    await query.edit_message_text(
        f"{arrow} *{name}* — alert {direction}{price_str}\n\n"
        f"Now type the target price:",
        parse_mode="Markdown",
    )
    return ENTER_PRICE


async def enter_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END

    text = update.message.text.strip()
    try:
        price = float(text)
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Type a number like `195.50`", parse_mode="Markdown")
        return ENTER_PRICE

    symbol = ctx.user_data.get("alert_symbol")
    direction = ctx.user_data.get("alert_direction")
    if not symbol or not direction:
        await update.message.reply_text("❌ Something went wrong. Use /setalert again.")
        return ConversationHandler.END

    alert_id = add_alert(symbol, direction, price)
    name = SYMBOL_NAMES.get(symbol, symbol)
    current = latest_prices.get(symbol)
    cur_str = f"\n📍 Current: {current}" if current else ""
    arrow = "🔺" if direction == "above" else "🔻"

    await update.message.reply_text(
        f"✅ *Alert #{alert_id} Set!*\n\n"
        f"{arrow} {name} {direction} *{price}*{cur_str}",
        parse_mode="Markdown",
    )
    log.info(f"Alert #{alert_id}: {name} {direction} {price}")
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel_conv(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Cancelled.")
    else:
        await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
#  INTERACTIVE PRICE CHECK:  /price → Category → Prices list
# ══════════════════════════════════════════════════════════════════════════

async def cmd_price_interactive(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return ConversationHandler.END

    if ctx.args:
        symbol = resolve_symbol(ctx.args[0])
        if not symbol:
            await update.message.reply_text(f"❌ Unknown symbol: `{ctx.args[0]}`", parse_mode="Markdown")
            return ConversationHandler.END
        name = SYMBOL_NAMES.get(symbol, symbol)
        current = latest_prices.get(symbol)
        if current:
            await update.message.reply_text(f"📊 {name}: *{current}*", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⏳ {name}: waiting for first tick...")
        return ConversationHandler.END

    buttons = []
    cats = list(SYMBOL_CATEGORIES.keys())
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"pcat:{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"pcat:{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await update.message.reply_text(
        "📊 *Check Price*\nPick a category:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PRICE_PICK_PAIR


async def price_pick_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    category = query.data.replace("pcat:", "")
    symbols = SYMBOL_CATEGORIES.get(category, [])

    lines = [f"📊 *{category}*\n"]
    for sym in symbols:
        name = SYMBOL_NAMES.get(sym, sym)
        price = latest_prices.get(sym)
        if price:
            lines.append(f"  {name}: *{price}*")
        else:
            lines.append(f"  {name}: ⏳")

    buttons = [[
        InlineKeyboardButton("⬅️ Back", callback_data="pback"),
        InlineKeyboardButton("❌ Close", callback_data="cancel"),
    ]]

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PRICE_PICK_PAIR


async def price_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    buttons = []
    cats = list(SYMBOL_CATEGORIES.keys())
    for i in range(0, len(cats), 2):
        row = [InlineKeyboardButton(cats[i], callback_data=f"pcat:{cats[i]}")]
        if i + 1 < len(cats):
            row.append(InlineKeyboardButton(cats[i + 1], callback_data=f"pcat:{cats[i + 1]}"))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    await query.edit_message_text(
        "📊 *Check Price*\nPick a category:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return PRICE_PICK_PAIR


# ══════════════════════════════════════════════════════════════════════════
#  SIMPLE COMMANDS
# ══════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    text = (
        "🤖 *PM23FX Deriv Alert Bot*\n\n"
        "*Interactive (tap to select):*\n"
        "  /setalert → Pick pair & set alert\n"
        "  /price → Check live prices\n\n"
        "*Quick commands:*\n"
        "  `/alert GBPJPY above 195.50`\n"
        "  `/price EURUSD`\n\n"
        "*Manage:*\n"
        "  /alerts → List active alerts\n"
        "  /delete ID → Delete an alert\n"
        "  /clearall → Clear all alerts\n"
        "  /symbols → List all symbols\n"
        "  /help → This message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_alert_quick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    args = ctx.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "❌ Usage: `/alert SYMBOL above|below PRICE`\n"
            "Or use /setalert for interactive mode!",
            parse_mode="Markdown",
        )
        return

    symbol_input, direction, price_str = args[0], args[1].lower(), args[2]
    try:
        price = float(price_str)
    except ValueError:
        await update.message.reply_text("❌ Invalid price.", parse_mode="Markdown")
        return

    if direction not in ("above", "below"):
        await update.message.reply_text("❌ Direction must be `above` or `below`", parse_mode="Markdown")
        return

    symbol = resolve_symbol(symbol_input)
    if not symbol:
        await update.message.reply_text(f"❌ Unknown: `{symbol_input}`\nUse /setalert for interactive mode.", parse_mode="Markdown")
        return

    alert_id = add_alert(symbol, direction, price)
    name = SYMBOL_NAMES.get(symbol, symbol)
    current = latest_prices.get(symbol)
    cur_str = f"  (current: {current})" if current else ""
    await update.message.reply_text(f"✅ Alert #{alert_id}: {name} {direction} {price}{cur_str}")


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    alerts = get_active_alerts()
    if not alerts:
        await update.message.reply_text("📭 No active alerts.\nUse /setalert to set one!")
        return

    lines = ["📋 *Active Alerts:*\n"]
    for aid, sym, direction, price in alerts:
        name = SYMBOL_NAMES.get(sym, sym)
        current = latest_prices.get(sym)
        cur_str = f" (now: {current})" if current else ""
        arrow = "🔺" if direction == "above" else "🔻"
        lines.append(f"  {arrow} `#{aid}` {name} {direction} {price}{cur_str}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/delete ID`", parse_mode="Markdown")
        return
    try:
        aid = int(ctx.args[0].replace("#", ""))
    except ValueError:
        await update.message.reply_text("❌ Invalid ID")
        return
    if delete_alert(aid):
        await update.message.reply_text(f"🗑 Alert #{aid} deleted.")
    else:
        await update.message.reply_text(f"❌ Alert #{aid} not found.")


async def cmd_clearall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    count = delete_all_alerts()
    await update.message.reply_text(f"🗑 Cleared {count} alert(s).")


async def cmd_symbols(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    lines = ["📊 *Available Symbols*\n"]
    for cat, syms in SYMBOL_CATEGORIES.items():
        names = [SYMBOL_NAMES.get(s, s) for s in syms]
        lines.append(f"*{cat}:*")
        lines.append(f"  `{', '.join(names)}`\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Deriv WebSocket ─────────────────────────────────────────────────────
async def send_telegram_alert(alert_id: int, symbol: str, direction: str, target: float, current: float):
    name = SYMBOL_NAMES.get(symbol, symbol)
    arrow = "🔺" if direction == "above" else "🔻"
    msg = (
        f"{arrow} *ALERT TRIGGERED!*\n\n"
        f"📊 *{name}*\n"
        f"Direction: {direction.upper()} {target}\n"
        f"Current: *{current}*\n"
        f"Alert ID: #{alert_id}\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
    )
    try:
        bot = telegram_app.bot
        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        log.info(f"🔔 Alert #{alert_id} triggered: {name} {direction} {target} (current: {current})")
    except Exception as e:
        log.error(f"Failed to send Telegram alert: {e}")


def check_alerts(symbol: str, price: float):
    alerts = get_active_alerts()
    for aid, sym, direction, target in alerts:
        if sym != symbol:
            continue
        triggered = False
        if direction == "above" and price >= target:
            triggered = True
        elif direction == "below" and price <= target:
            triggered = True
        if triggered:
            mark_triggered(aid)
            asyncio.get_event_loop().create_task(
                send_telegram_alert(aid, symbol, direction, target, price)
            )


async def stream_deriv_ticks():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(DERIV_WS_URL) as ws:
                    log.info("✅ Connected to Deriv WebSocket")

                    for i, symbol in enumerate(ALL_SYMBOLS):
                        await ws.send_json({"ticks": symbol, "subscribe": 1})
                        if i % 10 == 9:
                            await asyncio.sleep(0.5)

                    log.info(f"📡 Subscribed to {len(ALL_SYMBOLS)} symbols")

                    try:
                        bot = telegram_app.bot
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=(
                                f"🟢 *PM23FX Deriv Alert Bot v2 Online*\n"
                                f"Monitoring {len(ALL_SYMBOLS)} symbols\n"
                                f"Use /setalert to set alerts (interactive)\n"
                                f"Use /help for all commands"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        log.error(f"Startup notification failed: {e}")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if "tick" in data:
                                tick = data["tick"]
                                symbol = tick.get("symbol", "")
                                price = tick.get("quote")
                                if symbol and price is not None:
                                    latest_prices[symbol] = price
                                    check_alerts(symbol, price)
                            elif "error" in data:
                                err = data["error"]
                                log.warning(f"Deriv error: {err.get('message', err)}")
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

        except Exception as e:
            log.error(f"WebSocket error: {e}")

        log.info("🔄 Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


# ── Main ────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    commands = [
        BotCommand("setalert", "🎯 Set alert (interactive)"),
        BotCommand("price", "📊 Check price (interactive)"),
        BotCommand("alert", "Quick: /alert SYMBOL above|below PRICE"),
        BotCommand("alerts", "📋 List active alerts"),
        BotCommand("delete", "🗑 Delete alert by ID"),
        BotCommand("clearall", "🗑 Clear all alerts"),
        BotCommand("symbols", "📊 List all symbols"),
        BotCommand("help", "❓ Show help"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("Bot commands menu set")


async def main():
    global telegram_app

    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set!")
        return
    if CHAT_ID == 0:
        log.error("TELEGRAM_CHAT_ID not set!")
        return

    init_db()

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Interactive /setalert conversation ──
    setalert_conv = ConversationHandler(
        entry_points=[CommandHandler("setalert", cmd_setalert)],
        states={
            PICK_CATEGORY: [
                CallbackQueryHandler(cancel_conv, pattern="^cancel$"),
                CallbackQueryHandler(pick_category, pattern="^cat:"),
            ],
            PICK_PAIR: [
                CallbackQueryHandler(cancel_conv, pattern="^cancel$"),
                CallbackQueryHandler(back_to_cats, pattern="^back_to_cats$"),
                CallbackQueryHandler(pick_pair, pattern="^pair:"),
            ],
            PICK_DIRECTION: [
                CallbackQueryHandler(cancel_conv, pattern="^cancel$"),
                CallbackQueryHandler(pick_direction, pattern="^dir:"),
            ],
            ENTER_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_price),
                CommandHandler("cancel", cancel_conv),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_user=True,
        per_chat=True,
    )

    # ── Interactive /price conversation ──
    price_conv = ConversationHandler(
        entry_points=[CommandHandler("price", cmd_price_interactive)],
        states={
            PRICE_PICK_PAIR: [
                CallbackQueryHandler(cancel_conv, pattern="^cancel$"),
                CallbackQueryHandler(price_back, pattern="^pback$"),
                CallbackQueryHandler(price_pick_category, pattern="^pcat:"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_user=True,
        per_chat=True,
    )

    telegram_app.add_handler(setalert_conv)
    telegram_app.add_handler(price_conv)
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("help", cmd_start))
    telegram_app.add_handler(CommandHandler("alert", cmd_alert_quick))
    telegram_app.add_handler(CommandHandler("alerts", cmd_alerts))
    telegram_app.add_handler(CommandHandler("delete", cmd_delete))
    telegram_app.add_handler(CommandHandler("clearall", cmd_clearall))
    telegram_app.add_handler(CommandHandler("symbols", cmd_symbols))

    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        log.info("🤖 Telegram bot started")

        try:
            await stream_deriv_ticks()
        except asyncio.CancelledError:
            pass
        finally:
            await telegram_app.updater.stop()
            await telegram_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
