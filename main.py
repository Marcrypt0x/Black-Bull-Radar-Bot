import os
import logging
import time
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN", "REPLACE_ME")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WHALE_THRESHOLD = 5500
VOLUME_MULTIPLIER = 2.5
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

subscribed_chats = set()
seen_signatures = set()
pair_address = None
last_volume_alert = 0


def fmt(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:,.0f}"


def get_pair_info():
    """Devuelve todos los pairs del token"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        pairs = data.get("pairs")
        if pairs:
            return pairs
    except Exception as e:
        logger.error(f"get_pair_info error: {e}")
    return None


def get_ansem_data():
    pairs = get_pair_info()
    if not pairs:
        return None

    # Pair principal (mayor liquidez) para precio, cambios y volumen
    main_pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)

    price_change = main_pair.get("priceChange", {}) or {}
    volume = main_pair.get("volume", {}) or {}
    txns = main_pair.get("txns", {}) or {}

    # Liquidez total = suma de todos los pairs
    total_liquidity = sum(
        (p.get("liquidity", {}) or {}).get("usd", 0) or 0
        for p in pairs
    )

    return {
        "price": float(main_pair.get("priceUsd", 0)),
        "mcap": main_pair.get("marketCap") or main_pair.get("fdv") or 0,
        "liquidity": total_liquidity,
        "change_m5": price_change.get("m5", 0) or 0,
        "change_h1": price_change.get("h1", 0) or 0,
        "change_h6": price_change.get("h6", 0) or 0,
        "change_h24": price_change.get("h24", 0) or 0,
        "volume_h1": volume.get("h1", 0) or 0,
        "volume_h6": volume.get("h6", 0) or 0,
        "volume_h24": volume.get("h24", 0) or 0,
        "buys_h1": (txns.get("h1") or {}).get("buys", 0) or 0,
        "sells_h1": (txns.get("h1") or {}).get("sells", 0) or 0,
        "buys_h6": (txns.get("h6") or {}).get("buys", 0) or 0,
        "sells_h6": (txns.get("h6") or {}).get("sells", 0) or 0,
        "buys_h24": (txns.get("h24") or {}).get("buys", 0) or 0,
        "sells_h24": (txns.get("h24") or {}).get("sells", 0) or 0,
    }


def resolve_pair_address():
    global pair_address
    if pair_address:
        return pair_address
    pairs = get_pair_info()
    if pairs:
        main_pair = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
        pair_address = main_pair.get("pairAddress")
        logger.info(f"Pair address resolved: {pair_address}")
    return pair_address


def solana_post(method, params):
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=15,
        )
        return r.json().get("result")
    except Exception as e:
        logger.error(f"Solana RPC error: {e}")
    return None


def get_recent_signatures(address, limit=25):
    result = solana_post("getSignaturesForAddress", [address, {"limit": limit}])
    return result or []


def get_transaction(sig):
    return solana_post(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )


def parse_whale_trade(tx, current_price, pool_owner):
    if not tx:
        return None
    meta = tx.get("meta", {})
    if meta.get("err"):
        return None

    pre_balances = {
        e["accountIndex"]: e
        for e in meta.get("preTokenBalances", [])
        if e.get("mint") == TOKEN_ADDRESS
    }
    post_balances = {
        e["accountIndex"]: e
        for e in meta.get("postTokenBalances", [])
        if e.get("mint") == TOKEN_ADDRESS
    }

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
                "wallet": owner,
            }
    return best


async def whale_job(context):
    global last_volume_alert

    addr = resolve_pair_address()
    if not addr:
        return

    data = get_ansem_data()
    if not data or data["price"] == 0:
        return

    current_price = data["price"]

    # ——— Alerta de Volumen Inusual ———
    volume_h1 = data["volume_h1"]
    volume_h24 = data["volume_h24"]
    avg_hourly = volume_h24 / 24 if volume_h24 > 0 else 0

    if avg_hourly > 0 and volume_h1 >= (avg_hourly * VOLUME_MULTIPLIER):
        now = time.time()
        if now - last_volume_alert > 1800:  # máximo 1 alerta cada 30 min
            last_volume_alert = now
            multiplier = volume_h1 / avg_hourly

            msg = (
                f"🚨 *Volumen Inusual Detectado*\n\n"
                f"📦 Volumen 1h: {fmt(volume_h1)}\n"
                f"📊 Promedio horario 24h: {fmt(avg_hourly)}\n"
                f"📈 Multiplicador: *{multiplier:.1f}x*\n\n"
                f"💰 Precio actual: ${current_price:.6f}"
            )

            keyboard = [
                [InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack")],
                [
                    InlineKeyboardButton("📈 DexScreener", url=f"https://dexscreener.com/solana/{TOKEN_ADDRESS}"),
                    InlineKeyboardButton("🦅 Birdeye", url=f"https://birdeye.so/token/{TOKEN_ADDRESS}?chain=solana")
                ]
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
                    logger.error(f"Error sending volume alert: {e}")

    # ——— Alertas de Ballenas ———
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
            [
                InlineKeyboardButton("🔍 Ver TX", url=solscan_tx),
                InlineKeyboardButton("👛 Wallet", url=solscan_wallet)
            ]
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
        "• /price — Vista rápida\n"
        "• /stats — Stats detalladas\n"
        "• /alerts on — Activar alertas (ballenas + volumen)\n"
        "• /alerts off — Desactivar alertas\n"
        "• /help — Ayuda",
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def price(update, context):
    data = get_ansem_data()
    if not data:
        await update.message.reply_text("❌ Error obteniendo datos.")
        return

    change = data["change_h24"]
    change_emoji = "🟢" if change >= 0 else "🔴"

    msg = (
        f"🐂 *$ANSEM* — The Black Bull\n\n"
        f"💰 Precio: ${data['price']:.6f}\n"
        f"📊 Market Cap: {fmt(data['mcap'])}\n"
        f"{change_emoji} 24h: {change:+.2f}%\n"
        f"💧 Liquidez total: {fmt(data['liquidity'])}\n"
        f"📦 Volumen 24h: {fmt(data['volume_h24'])}\n\n"
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

    def change_emoji(val):
        return "🟢" if val >= 0 else "🔴"

    def pct(val):
        return f"{val:+.2f}%"

    def buy_ratio(buys, sells):
        total = buys + sells
        if total == 0:
            return "—"
        ratio = (buys / total) * 100
        return f"{ratio:.0f}% compras"

    msg = (
        f"🐂 *$ANSEM* — Stats detalladas\n\n"
        f"💰 Precio: ${data['price']:.6f}\n"
        f"📊 Market Cap: {fmt(data['mcap'])}\n"
        f"💧 Liquidez total: {fmt(data['liquidity'])}\n\n"
        f"📈 *Cambio de precio*\n"
        f"5m: {change_emoji(data['change_m5'])} {pct(data['change_m5'])}\n"
        f"1h: {change_emoji(data['change_h1'])} {pct(data['change_h1'])}\n"
        f"6h: {change_emoji(data['change_h6'])} {pct(data['change_h6'])}\n"
        f"24h: {change_emoji(data['change_h24'])} {pct(data['change_h24'])}\n\n"
        f"📦 *Volumen*\n"
        f"1h: {fmt(data['volume_h1'])}\n"
        f"6h: {fmt(data['volume_h6'])}\n"
        f"24h: {fmt(data['volume_h24'])}\n\n"
        f"🔄 *Actividad de trading*\n"
        f"1h → {data['buys_h1']} compras / {data['sells_h1']} ventas ({buy_ratio(data['buys_h1'], data['sells_h1'])})\n"
        f"6h → {data['buys_h6']} compras / {data['sells_h6']} ventas ({buy_ratio(data['buys_h6'], data['sells_h6'])})\n"
        f"24h → {data['buys_h24']} compras / {data['sells_h24']} ventas ({buy_ratio(data['buys_h24'], data['sells_h24'])})"
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
        await update.message.reply_text(
            f"🐋 Alertas (ballenas + volumen inusual):\nEstado: {status}\n\n"
            f"/alerts on\n/alerts off"
        )
        return

    if args[0].lower() == "on":
        subscribed_chats.add(chat_id)
        await update.message.reply_text(
            f"🐋 Alertas activadas\n\n"
            f"• Ballenas > ${WHALE_THRESHOLD:,}\n"
            f"• Volumen inusual (≥ {VOLUME_MULTIPLIER}x promedio)"
        )
    else:
        subscribed_chats.discard(chat_id)
        await update.message.reply_text("🔕 Alertas desactivadas.")


async def help_command(update, context):
    await update.message.reply_text(
        "🐂 *BlackBullRadar*\n\n"
        "/price → Vista rápida\n"
        "/stats → Stats detalladas + ratio compras/ventas\n"
        f"/alerts on → Ballenas + Volumen inusual\n"
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
