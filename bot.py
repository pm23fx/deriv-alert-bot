"""
PM23FX Deriv Alert Bot
- Streams live ticks from Deriv WebSocket API
- Custom price alerts via Telegram commands
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
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ── Config ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
DERIV_WS_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"  # Public app_id

# ── Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("DerivBot")

# ── Symbol Registry ─────────────────────────────────────────────────────
FOREX_PAIRS = [
    # Majors
    "frxAUDCAD", "frxAUDCHF", "frxAUDJPY", "frxAUDNZD", "frxAUDUSD",
    "frxEURAUD", "frxEURCAD", "frxEURCHF", "frxEURGBP", "frxEURJPY",
    "frxEURNZD", "frxEURUSD",
    "frxGBPAUD", "frxGBPCAD", "frxGBPCHF", "frxGBPJPY", "frxGBPNZD",
    "frxGBPUSD",
    "frxNZDCAD", "frxNZDCHF", "frxNZDJPY", "frxNZDUSD",
    "frxUSDCAD", "frxUSDCHF", "frxUSDJPY", "frxUSDMXN", "frxUSDNOK",
    "frxUSDPLN", "frxUSDSEK", "frxUSDZAR",
    # Metals
    "frxXAUUSD", "frxXAGUSD", "frxXPDUSD", "frxXPTUSD",
]

DERIV_INDICES = [
    # Volatility Indices
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    # Crash / Boom
    "BOOM300N", "BOOM500N", "BOOM1000N",
    "CRASH300N", "CRASH500N", "CRASH1000N",
    # Step Index
    "stpRNG",
    # Jump Indices
    "JD10", "JD25", "JD50", "JD75", "JD100",
    # Range Break
    "RDBEAR", "RDBULL",
    # Drift Switch
    "DSI10", "DSI20", "DSI30",
    # DEX Indices
    "DEX600DN", "DEX600UP", "DEX900DN", "DEX900UP",
    "DEX1500DN", "DEX1500UP",
]

ALL_SYMBOLS = FOREX_PAIRS + DERIV_INDICES

# Friendly name mapping
SYMBOL_NAMES = {}
for s in FOREX_PAIRS:
    pair = s.replace("frx", "")
    SYMBOL_NAMES[s] = f"{pair[:3]}/{pair[3:]}" if "X" not in pair[:3] else pair
for s in DERIV_INDICES:
    SYMBOL_NAMES[s] = s

# Reverse lookup: user-friendly name -> deriv symbol
REVERSE_LOOKUP = {}
for sym, name in SYMBOL_NAMES.items():
    REVERSE_LOOKUP[name.upper()] = sym
    REVERSE_LOOKUP[name.upper().replace("/", "")] = sym
    REVERSE_LOOKUP[sym.upper()] = sym
    # Also allow lowercase without prefix
    clean = sym.replace("frx", "").upper()
    REVERSE_LOOKUP[clean] = sym

# ── Database ────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", "alerts.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,  -- 'above' or 'below'
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


# ── Resolve user input to Deriv symbol ──────────────────────────────────
def resolve_symbol(user_input: str) -> str | None:
    key = user_input.strip().upper().replace("/", "")
    if key in REVERSE_LOOKUP:
        return REVERSE_LOOKUP[key]
    # Fuzzy: check if input is substring
    for name, sym in REVERSE_LOOKUP.items():
        if key in name:
            return sym
    return None


# ── Global state ────────────────────────────────────────────────────────
latest_prices: dict[str, float] = {}
telegram_app: Application = None
ws_connection = None


# ── Telegram Commands ───────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    text = (
        "🤖 *PM23FX Deriv Alert Bot*\n\n"
        "Commands:\n"
        "`/alert SYMBOL above|below PRICE`\n"
        "  → Set a price alert\n\n"
        "`/alerts` → List active alerts\n"
        "`/price SYMBOL` → Get current price\n"
        "`/delete ID` → Delete an alert\n"
        "`/clearall` → Delete all alerts\n"
        "`/symbols` → List available symbols\n"
        "`/help` → Show this message\n\n"
        "Examples:\n"
        "`/alert GBPJPY above 195.50`\n"
        "`/alert XAUUSD below 2300`\n"
        "`/alert R_100 above 6500`\n"
        "`/alert BOOM1000N above 9500`\n"
        "`/price EURUSD`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_alert(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    args = ctx.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "❌ Usage: `/alert SYMBOL above|below PRICE`\n"
            "Example: `/alert GBPJPY above 195.50`",
            parse_mode="Markdown",
        )
        return

    symbol_input = args[0]
    direction = args[1].lower()
    try:
        price = float(args[2])
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Use a number like `195.50`", parse_mode="Markdown")
        return

    if direction not in ("above", "below"):
        await update.message.reply_text("❌ Direction must be `above` or `below`", parse_mode="Markdown")
        return

    symbol = resolve_symbol(symbol_input)
    if not symbol:
        await update.message.reply_text(
            f"❌ Unknown symbol: `{symbol_input}`\nUse `/symbols` to see available symbols.",
            parse_mode="Markdown",
        )
        return

    alert_id = add_alert(symbol, direction, price)
    name = SYMBOL_NAMES.get(symbol, symbol)
    current = latest_prices.get(symbol)
    current_str = f"  (current: {current})" if current else ""

    await update.message.reply_text(
        f"✅ Alert #{alert_id} set!\n"
        f"📊 {name} {direction} {price}{current_str}",
    )
    log.info(f"Alert #{alert_id}: {name} {direction} {price}")


async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    alerts = get_active_alerts()
    if not alerts:
        await update.message.reply_text("📭 No active alerts.")
        return

    lines = ["📋 *Active Alerts:*\n"]
    for aid, sym, direction, price in alerts:
        name = SYMBOL_NAMES.get(sym, sym)
        current = latest_prices.get(sym)
        cur_str = f" (now: {current})" if current else ""
        lines.append(f"  `#{aid}` {name} {direction} {price}{cur_str}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != CHAT_ID:
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/price SYMBOL`", parse_mode="Markdown")
        return

    symbol = resolve_symbol(ctx.args[0])
    if not symbol:
        await update.message.reply_text(f"❌ Unknown symbol: `{ctx.args[0]}`", parse_mode="Markdown")
        return

    name = SYMBOL_NAMES.get(symbol, symbol)
    current = latest_prices.get(symbol)
    if current:
        await update.message.reply_text(f"📊 {name}: *{current}*", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⏳ {name}: waiting for first tick...")


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

    forex_names = [SYMBOL_NAMES[s] for s in FOREX_PAIRS]
    index_names = [SYMBOL_NAMES[s] for s in DERIV_INDICES]

    text = (
        "📊 *Available Symbols*\n\n"
        f"*Forex ({len(FOREX_PAIRS)}):*\n"
        f"`{', '.join(forex_names)}`\n\n"
        f"*Deriv Indices ({len(DERIV_INDICES)}):*\n"
        f"`{', '.join(index_names)}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Deriv WebSocket Price Streaming ─────────────────────────────────────
async def send_telegram_alert(alert_id: int, symbol: str, direction: str, target: float, current: float):
    """Send alert notification via Telegram."""
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
    """Check if any alerts are triggered by the current price."""
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
            # Schedule the Telegram notification
            asyncio.get_event_loop().create_task(
                send_telegram_alert(aid, symbol, direction, target, price)
            )


async def stream_deriv_ticks():
    """Connect to Deriv WS and stream ticks for all symbols."""
    global ws_connection

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(DERIV_WS_URL) as ws:
                    ws_connection = ws
                    log.info(f"✅ Connected to Deriv WebSocket")

                    # Subscribe to all symbols
                    for i, symbol in enumerate(ALL_SYMBOLS):
                        sub_msg = {
                            "ticks": symbol,
                            "subscribe": 1,
                        }
                        await ws.send_json(sub_msg)
                        # Small delay to avoid rate limiting
                        if i % 10 == 9:
                            await asyncio.sleep(0.5)

                    log.info(f"📡 Subscribed to {len(ALL_SYMBOLS)} symbols")

                    # Send startup notification
                    try:
                        bot = telegram_app.bot
                        await bot.send_message(
                            chat_id=CHAT_ID,
                            text=(
                                f"🟢 *PM23FX Deriv Alert Bot Online*\n"
                                f"Monitoring {len(FOREX_PAIRS)} forex pairs + "
                                f"{len(DERIV_INDICES)} Deriv indices\n"
                                f"Use /help for commands"
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        log.error(f"Startup notification failed: {e}")

                    # Process incoming ticks
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
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.error(f"WebSocket error: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            log.warning("WebSocket closed by server")
                            break

        except Exception as e:
            log.error(f"WebSocket connection error: {e}")

        # Reconnect after 5 seconds
        log.info("🔄 Reconnecting in 5 seconds...")
        await asyncio.sleep(5)


# ── Main ────────────────────────────────────────────────────────────────
async def post_init(application: Application):
    """Set bot commands menu after initialization."""
    commands = [
        BotCommand("alert", "Set price alert: /alert SYMBOL above|below PRICE"),
        BotCommand("alerts", "List active alerts"),
        BotCommand("price", "Get current price: /price SYMBOL"),
        BotCommand("delete", "Delete alert: /delete ID"),
        BotCommand("clearall", "Clear all alerts"),
        BotCommand("symbols", "List available symbols"),
        BotCommand("help", "Show help"),
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

    # Init database
    init_db()

    # Build Telegram bot
    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("help", cmd_start))
    telegram_app.add_handler(CommandHandler("alert", cmd_alert))
    telegram_app.add_handler(CommandHandler("alerts", cmd_alerts))
    telegram_app.add_handler(CommandHandler("price", cmd_price))
    telegram_app.add_handler(CommandHandler("delete", cmd_delete))
    telegram_app.add_handler(CommandHandler("clearall", cmd_clearall))
    telegram_app.add_handler(CommandHandler("symbols", cmd_symbols))

    # Start Telegram polling + Deriv streaming concurrently
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        log.info("🤖 Telegram bot started")

        # Run Deriv WebSocket streaming
        try:
            await stream_deriv_ticks()
        except asyncio.CancelledError:
            pass
        finally:
            await telegram_app.updater.stop()
            await telegram_app.stop()


if __name__ == "__main__":
    asyncio.run(main())
