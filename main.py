import os
import requests
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN", "8222243632:AAFccOHOGAwKz9GxbpDWA2dov-ddb2C6KWg")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

def get_ansem_data():
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("pairs"):
            pair = data["pairs"][0]
            price = float(pair.get("priceUsd", 0))
            mcap = pair.get("marketCap") or pair.get("fdv") or 0
            volume = pair.get("volume", {}).get("h24", 0)
            liquidity = pair.get("liquidity", {}).get("usd", 0)
            change_24h = pair.get("priceChange", {}).get("h24", 0)
            return {
                "price": price,
                "mcap": mcap,
                "volume": volume,
                "liquidity": liquidity,
                "change_24h": change_24h
            }
    except Exception as e:
        logging.error(f"Error: {e}")
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐂 BlackBullRadar online\n\n"
        "Comandos:\n"
        "/price → precio + MC actual\n"
        "/stats → métricas\n"
        "/help → ayuda"
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_ansem_data()
    if not data:
        await update.message.reply_text("Error obteniendo datos. Intenta de nuevo.")
        return

    msg = (
        f"🐂 **$ANSEM BlackBullRadar**\n\n"
        f"💰 Precio: ${data['price']:.6f}\n"
        f"📊 Market Cap: ${data['mcap']:,.0f}\n"
        f"📈 24h: {data['change_24h']:+.2f}%\n"
        f"💧 Liquidez: ${data['liquidity']:,.0f}\n"
        f"📦 Volumen 24h: ${data['volume']:,.0f}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await price(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("BlackBullRadar - Tracker de $ANSEM\n\n/price o /stats para datos actuales")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help", help_command))
    print("BlackBullRadar arrancando...")
    app.run_polling()

if __name__ == "__main__":
    main()
