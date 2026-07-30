import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN", "REPLACE_ME")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WHALE_THRESHOLD = 5500
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

subscribed_chats = set()
seen_signatures = set()
pair_address = None


def get_pair_info():
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("pairs"):
            return max(data["pairs"], key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
    except Exception as e:
        logger.error(f"get_pair_info error: {e}")
    return None


def get_ansem_data():
    pair = get_pair_info()
    if not pair:
        return None
    price = float(pair.get("priceUsd", 0))
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    volume = pair.get("volume", {}).get("h24", 0) or 0
    liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
    change_24h = pair.get("priceChange", {}).get("h24", 0) or 0
    return {
        "price": price,
        "mcap": mcap,
        "volume": volume,
        "liquidity": liquidity,
        "change_24h": change_24h
    }


def resolve_pair_address():
    global pair_address
    if pair_address:
        return pair_address
    pair = get_pair_info()
    if pair:
        pair_address = pair.get("pairAddress")
        logger.info(f"Pair address resolved: {pair_address}")
    return pair_address


def get_holder_count():
    if not HELIUS_RPC:
        return None
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": "holders",
            "method": "getTokenAccounts",
            "params": {
                "mint": TOKEN_ADDRESS,
                "limit": 1,
                "page": 1
            }
        }
        r = requests.post(HELIUS_RPC, json=payload, timeout=12)
        data = r.json()
        result = data.get("result")
        if result and "total" in result:
            return int(result["total"])
    except Exception as e:
        logger.error(f"Error holders: {e}")
    return None


def solana_post(method, params):
    try:
        r = requests.post(SOLANA_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
        return r.json().get("result")
    except Exception as e:
        logger.error(f"Solana RPC error: {e}")
    return None


def get_recent_signatures(address, limit=25):
    result = solana_post("getSignaturesForAddress", [address, {"limit": limit}])
    return result or []


def get_transaction(sig):
    return solana_post("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])


def parse_whale_trade(tx, current_price, pool_owner):
    if not tx:
        return None
    meta = tx.get("meta", {})
    if meta.get("err"):
        return None
    pre_balances = {e["accountIndex"]: e for e in meta.get("preTokenBalances", []) if e.get("mint") == TOKEN_ADDRESS}
    post_balances = {e["accountIndex"]: e for e in meta.get("postTokenBalances", []) if e.get("mint") == TOKEN_ADDRESS}
    all_indexes = set(pre_balances) | set(post_balances)
    best = None
    for idx in all_indexes:
        pre_entry = pre_balances.get(idx, {})
        post_entry = post_balances.get(idx, {})
        owner = (pre_entry or post_entry).get("owner", "")
        if owner == pool_owner:
            continue
        pre_amt = float((pre_entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post_amt = float((post_entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        delta = post_amt - pre_amt
        usd_value = abs(delta) * current_price
        if usd_value < WHALE_THRESHOLD:
            continue
        if best is None or usd_value > best["usd_value"]:
            best = {
                "side": "BUY" if delta > 0 else "SELL",
                "tokens": abs(delta),
                "usd_value": usd_value,
                "price": current_price,
                "wallet": owner
            }
    return best


async def whale_job(context):
    addr = resolve_pair_address()
    if not addr:
        return
    data = get_ansem_data()
    if not data or data["price"] == 0:
        return
    current_price = data["price"]
    sigs = get_recent_signatures(addr, limit=25)
    new_whales = []
    for entry in sigs:
        sig = entry.get("signature", "")
        if not sig or sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        if entry.get("err"):
            continue
        tx = get_transaction(sig)
        whale = parse_whale_trade(tx, current_price, pool_owner=addr)
        if whale:
            whale["sig"] = sig
            new_whales.append(whale)
    if not new_whales or not subscribed_chats:
        return
    for whale in new_whales:
        emoji = "🟢 COMPRA" if whale["side"] == "BUY" else "🔴 VENTA"
        short_wallet = whale["wallet"][:6] + "..." + whale["wallet"][-4:]
        solscan_tx = f"https://solscan.io/tx/{whale['sig']}"
        solscan_wallet = f"https://solscan.io/account/{whale['wallet']}"
        msg = (
            f"🐋 *Whale Alert*\n\n"
            f"{emoji}\n"
            f"💵 Valor: *${whale['usd_value']:,.0f}*\n"
            f"🪙 Tokens: {whale['tokens']:,.2f} $ANSEM\n"
            f"💰 Precio: ${whale['price']:.6f}\n"
            f"👛 Wallet: {short_wallet}\n"
        )
        keyboard = [
            [InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack")],
            [InlineKeyboardButton("🔍 Ver TX", url=solscan_tx), InlineKeyboardButton("👛 Wallet", url=solscan_wallet)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        for chat_id in list(subscribed_chats):
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Error sending whale alert: {e}")


async def start(update, context):
    keyboard = [
        [InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack")],
        [
            InlineKeyboardButton("📈 DexScreener", url=f"https://dexscreener.com/solana/{TOKEN_ADDRESS}"),
            InlineKeyboardButton("🦅 Birdeye", url=f"https://birdeye.so/token/{TOKEN_ADDRESS}?chain=solana")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🐂 *BlackBullRadar*\n"
        "Tracker en tiempo real de *$ANSEM*\n\n"
        "Comandos:\n"
        "• /price\n"
        "• /stats\n"
        "• /alerts on\n"
        "• /alerts off\n"
        "• /help\n\n"
        "💡 Usa los botones para operar rápido.",
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def price(update, context):
    data = get_ansem_data()
    if not data:
        await update.message.reply_text("❌ Error obteniendo datos.")
        return

    change = data["change_24h"]
    change_emoji = "🟢" if change >= 0 else "🔴"

    def fmt(n):
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.1f}K"
        return f"${n:,.0f}"

    msg = (
        f"🐂 *$ANSEM* — The Black Bull\n\n"
        f"💰 Precio: ${data['price']:.6f}\n"
        f"📊 Market Cap: {fmt(data['mcap'])}\n"
        f"{change_emoji} 24h: {change:+.2f}%\n"
        f"💧 Liquidez: {fmt(data['liquidity'])}\n"
        f"📦 Volumen 24h: {fmt(data['volume'])}\n\n"
        f"🕐 Actualizado ahora"
    )

    keyboard = [
        [InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack")],
        [
            InlineKeyboardButton("📈 DexScreener", url=f"https://dexscreener.com/solana/{TOKEN_ADDRESS}"),
            InlineKeyboardButton("🦅 Birdeye", url=f"https://birdeye.so/token/{TOKEN_ADDRESS}?chain=solana")
        ]
    ]
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )


async def stats(update, context):
    data = get_ansem_data()
    if not data:
        await update.message.reply_text("❌ Error obteniendo datos.")
        return

    holders = get_holder_count()
    change = data["change_24h"]
    change_emoji = "🟢" if change >= 0 else "🔴"

    def fmt(n):
        if n >= 1_000_000:
            return f"${n/1_000_000:.2f}M"
        if n >= 1_000:
            return f"${n/1_000:.1f}K"
        return f"${n:,.0f}"

    holders_text = f"👥 Holders: {holders:,}" if holders else "👥 Holders: No disponible"

    msg = (
        f"🐂 *$ANSEM* — Stats\n\n"
        f"💰 Precio: ${data['price']:.6f}\n"
        f"📊 Market Cap: {fmt(data['mcap'])}\n"
        f"{change_emoji} 24h: {change:+.2f}%\n"
        f"💧 Liquidez: {fmt(data['liquidity'])}\n"
        f"📦 Volumen 24h: {fmt(data['volume'])}\n"
        f"{holders_text}\n\n"
        f"🕐 Actualizado ahora"
    )

    keyboard = [
        [InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack")],
        [
            InlineKeyboardButton("📈 DexScreener", url=f"https://dexscreener.com/solana/{TOKEN_ADDRESS}"),
            InlineKeyboardButton("🦅 Birdeye", url=f"https://birdeye.so/token/{TOKEN_ADDRESS}?chain=solana")
        ]
    ]
    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )


async def alerts(update, context):
    chat_id = update.effective_chat.id
    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        status = "✅ activas" if chat_id in subscribed_chats else "❌ inactivas"
        await update.message.reply_text(f"🐋 Alertas (>${WHALE_THRESHOLD:,}): {status}\n\n/alerts on\n/alerts off")
        return
    if args[0].lower() == "on":
        subscribed_chats.add(chat_id)
        await update.message.reply_text(f"🐋 Alertas activadas ( > ${WHALE_THRESHOLD:,} )")
    else:
        subscribed_chats.discard(chat_id)
        await update.message.reply_text("🔕 Alertas desactivadas.")


async def help_command(update, context):
    await update.message.reply_text(
        "🐂 *BlackBullRadar*\n\n"
        "/price → Precio\n"
        "/stats → Stats + holders\n"
        f"/alerts on → Ballenas > ${WHALE_THRESHOLD:,}\n"
        "/alerts off → Desactivar\n"
        "/help → Ayuda",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("help", help_command))
    app.job_queue.run_repeating(whale_job, interval=60, first=10)
    print("🐂 BlackBullRadar arrancando...")
    app.run_polling()


if __name__ == "__main__":
    main()
