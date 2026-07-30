import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN", "8222243632:AAFccOHOGAwKz9GxbpDWA2dov-ddb2C6KWg")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WHALE_THRESHOLD = 10_000  # USD

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory state
subscribed_chats: set[int] = set()
seen_tx_hashes: set[str] = set()
pair_address: str | None = None


# ── DexScreener helpers ──────────────────────────────────────────────────────

def get_pair_info() -> dict | None:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("pairs"):
            return data["pairs"][0]
    except Exception as e:
        logger.error(f"get_pair_info error: {e}")
    return None


def get_ansem_data() -> dict | None:
    pair = get_pair_info()
    if not pair:
        return None
    return {
        "price":      float(pair.get("priceUsd", 0)),
        "mcap":       pair.get("marketCap") or pair.get("fdv") or 0,
        "volume":     pair.get("volume", {}).get("h24", 0),
        "liquidity":  pair.get("liquidity", {}).get("usd", 0),
        "change_24h": pair.get("priceChange", {}).get("h24", 0),
    }


def get_recent_trades(pair_addr: str) -> list[dict]:
    """Return recent trades for a DexScreener pair address."""
    url = f"https://api.dexscreener.com/latest/dex/trades/{pair_addr}"
    try:
        r = requests.get(url, timeout=10)
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        logger.error(f"get_recent_trades error: {e}")
    return []


def resolve_pair_address() -> str | None:
    """Fetch and cache the pair address for the token."""
    global pair_address
    if pair_address:
        return pair_address
    pair = get_pair_info()
    if pair:
        pair_address = pair.get("pairAddress")
        logger.info(f"Pair address resolved: {pair_address}")
    return pair_address


# ── Whale monitor (background task) ─────────────────────────────────────────

async def whale_monitor(app: Application) -> None:
    """Poll for trades and alert subscribed chats on whale activity."""
    global seen_tx_hashes

    addr = resolve_pair_address()
    if not addr:
        logger.warning("whale_monitor: pair address not resolved yet")
        return

    trades = get_recent_trades(addr)
    if not trades:
        return

    new_whales = []
    for trade in trades:
        tx_hash = trade.get("txHash") or trade.get("id", "")
        if not tx_hash or tx_hash in seen_tx_hashes:
            continue
        seen_tx_hashes.add(tx_hash)

        amount_usd = float(trade.get("amountUsd", 0) or 0)
        if amount_usd < WHALE_THRESHOLD:
            continue

        side = trade.get("type", "").upper()  # "buy" / "sell"
        emoji = "🟢" if side == "BUY" else "🔴"
        price = float(trade.get("priceUsd", 0) or 0)
        token_amount = float(trade.get("amount", 0) or 0)

        new_whales.append({
            "emoji": emoji,
            "side": side,
            "amount_usd": amount_usd,
            "token_amount": token_amount,
            "price": price,
            "tx": tx_hash,
        })

    if not new_whales or not subscribed_chats:
        return

    for whale in new_whales:
        short_tx = whale["tx"][:12] + "…" if len(whale["tx"]) > 12 else whale["tx"]
        msg = (
            f"{whale['emoji']} *Whale Alert — ${whale['amount_usd']:,.0f}*\n\n"
            f"Tipo: `{whale['side']}`\n"
            f"Tokens: `{whale['token_amount']:,.2f} $ANSEM`\n"
            f"Precio: `${whale['price']:.6f}`\n"
            f"TX: `{short_tx}`"
        )
        for chat_id in list(subscribed_chats):
            try:
                await app.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Error sending whale alert to {chat_id}: {e}")


# ── Command handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐂 *BlackBullRadar online*\n\n"
        "Comandos:\n"
        "/price — precio + MC actual\n"
        "/stats — métricas completas\n"
        "/alerts on — activar alertas de ballenas 🐋\n"
        "/alerts off — desactivar alertas\n"
        "/help — ayuda",
        parse_mode="Markdown"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_ansem_data()
    if not data:
        await update.message.reply_text("Error obteniendo datos. Intenta de nuevo.")
        return
    msg = (
        f"🐂 *$ANSEM BlackBullRadar*\n\n"
        f"💰 Precio: `${data['price']:.6f}`\n"
        f"📊 Market Cap: `${data['mcap']:,.0f}`\n"
        f"📈 24h: `{data['change_24h']:+.2f}%`\n"
        f"💧 Liquidez: `${data['liquidity']:,.0f}`\n"
        f"📦 Volumen 24h: `${data['volume']:,.0f}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await price(update, context)


async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args

    if not args or args[0].lower() not in ("on", "off"):
        status = "✅ activas" if chat_id in subscribed_chats else "❌ inactivas"
        await update.message.reply_text(
            f"Alertas de ballenas (>{WHALE_THRESHOLD:,} USD): {status}\n\n"
            "Usa /alerts on para activar\n"
            "Usa /alerts off para desactivar"
        )
        return

    if args[0].lower() == "on":
        subscribed_chats.add(chat_id)
        await update.message.reply_text(
            f"🐋 Alertas de ballenas activadas.\n"
            f"Te avisaré cuando haya transacciones de más de ${WHALE_THRESHOLD:,}."
        )
    else:
        subscribed_chats.discard(chat_id)
        await update.message.reply_text("🔕 Alertas de ballenas desactivadas.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐂 *BlackBullRadar* — Tracker de $ANSEM\n\n"
        "/price o /stats — datos actuales\n"
        "/alerts on — recibir alertas de ballenas (>$10,000)\n"
        "/alerts off — dejar de recibir alertas",
        parse_mode="Markdown"
    )


# ── Periodic job wrapper ─────────────────────────────────────────────────────

async def whale_job(context: ContextTypes.DEFAULT_TYPE):
    await whale_monitor(context.application)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("price",  price))
    app.add_handler(CommandHandler("stats",  stats))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("help",   help_command))

    # Poll for whale trades every 60 seconds
    job_queue = app.job_queue
    job_queue.run_repeating(whale_job, interval=60, first=10)

    print("BlackBullRadar arrancando con monitor de ballenas...")
    app.run_polling()


if __name__ == "__main__":
    main()
