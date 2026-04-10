# PM23FX Deriv Alert Bot 🤖

Custom price alert bot for Deriv — streams live ticks via WebSocket and sends Telegram notifications when your target levels are hit.

## Features
- 📊 **34 Forex pairs** (majors, crosses, metals)
- 📈 **30+ Deriv synthetic indices** (Volatility, Crash/Boom, Jump, DEX, etc.)
- ⚡ Real-time tick streaming via Deriv WebSocket API
- 🔔 Instant Telegram alerts when price levels are hit
- 💾 SQLite database for persistent alert storage
- 🔄 Auto-reconnect on connection drops

## Telegram Commands
| Command | Description |
|---------|-------------|
| `/alert SYMBOL above\|below PRICE` | Set a price alert |
| `/alerts` | List all active alerts |
| `/price SYMBOL` | Get current price |
| `/delete ID` | Delete an alert by ID |
| `/clearall` | Clear all active alerts |
| `/symbols` | List all available symbols |
| `/help` | Show help message |

## Examples
```
/alert GBPJPY above 195.50
/alert XAUUSD below 2300
/alert R_100 above 6500
/alert BOOM1000N above 9500
/price EURUSD
```

## Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add environment variables:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
4. Railway auto-deploys. Done!

## Environment Variables
| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | Your Telegram user/chat ID |
