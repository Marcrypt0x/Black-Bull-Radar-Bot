import os
import logging
import requests
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN", "REPLACE_ME")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WHALE_THRESHOLD = 5_500  # USD
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# In-memory state
subscribed_chats: set[int] = set()
seen_signatures: set[str] = set()
pair_address: str | None = None


# ── Formatting helpers ───────────────────────────────────────────────────────

def fmt(value: float) -> str:
    """Format a USD number with M/K suffix."""
    v = float(value or 0)
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:.2f}"


def fmt_pct(value: float) -> str:
    emoji = "🟢" if value >= 0 else "🔴"
    sign  = "+" if value >= 0 else ""
    return f"{emoji} `{sign}{value:.2f}%`"


def pair_age(created_at_ms: int) -> str:
    """Return human-readable age from a Unix-ms timestamp."""
    delta = datetime.now(timezone.utc) - datetime.fromtimestamp(created_at_ms / 1000, tz=timezone.utc)
    days  = delta.days
    if days >= 30:
        return f"{days // 30}m {days % 30}d"
    if days >= 1:
        return f"{days}d {delta.seconds // 3600}h"
    hours = delta.seconds // 3600
    mins  = (delta.seconds % 3600) // 60
    return f"{hours}h {mins}m"


# ── DexScreener helpers ──────────────────────────────────────────────────────
def get_pair_info() -> dict | None:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("pairs"):
            # Elegimos el par con mayor liquidez
            return max(data["pairs"], key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
    except Exception as e:
        logger.error(f"get_pair_info error: {e}")
    return None


def get_ansem_data() -> dict | None:
    pair = get_pair_info()
    if not pair:
        return None
    return {
        "price": float(pair.get("priceUsd", 0)),
        "mcap": pair.get("marketCap") or pair.get("fdv") or 0,
        "volume": pair.get("volume", {}).get("h24", 0) or 0,
        "liquidity": pair.get("liquidity", {}).get("usd", 0) or 0,
        "change_24h": pair.get("priceChange", {}).get("h24", 0) or 0,
    }


def resolve_pair_address() -> str | None:
    global pair_address
    if pair_address:
        return pair_address
    pair = get_pair_info()
    if pair:
        pair_address = pair.get("pairAddress")
        logger.info(f"Pair address resolved: {pair_address}")
    return pair_address


# ── Solana RPC helpers ───────────────────────────────────────────────────────
def solana_post(method: str, params: list) -> dict | None:
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=15,
        )
        data = r.json()
        return data.get("result")
    except Exception as e:
        logger.error(f"Solana RPC error ({method}): {e}")
    return None


def get_recent_signatures(address: str, limit: int = 25) -> list[dict]:
    result = solana_post("getSignaturesForAddress", [address, {"limit": limit}])
    return result or []


def get_transaction(sig: str) -> dict | None:
    return solana_post(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )


def parse_whale_trade(tx: dict, current_price: float, pool_owner: str) -> dict | None:
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


# ── Whale monitor (background job) ──────────────────────────────────────────
async def whale_job(context) -> None:
    addr = resolve_pair_address()
    if not addr:
        logger.warning("whale_job: pair address not resolved")
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
            f"🪙 Tokens: `{whale['tokens']:,.2f} $ANSEM`\n"
            f"💰 Precio: `${whale['price']:.6f}`\n"
            f"👛 Wallet: `{short_wallet}`\n"
        )

        keyboard = [
            [
                InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack"),
            ],
            [
                InlineKeyboardButton("🔍 Ver TX", url=solscan_tx),
                InlineKeyboardButton("👛 Wallet", url=solscan_wallet),
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
                logger.error(f"Error sending whale alert to {chat_id}: {e}")


# ── Command handlers ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "Comandos disponibles:\n"
        "• /price — Precio y métricas\n"
        "• /stats — Métricas completas\n"
        "• /alerts on — Activar alertas de ballenas\n"
        "• /alerts off — Desactivar alertas\n"
        "• /help — Ayuda\n\n"
        "💡 Usa los botones de abajo para operar rápido.",
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_ansem_data()
    if not data:
        await update.message.reply_text("❌ Error obteniendo datos. Intenta de nuevo en unos segundos.")
        return

    change = data["change_24h"]
    change_emoji = "🟢" if change >= 0 else "🔴"

    msg = (
        f"🐂 *$ANSEM* — The Black Bull\n\n"
        f"💰 Precio: `${data['price']:.6f}`\n"
        f"📊 Market Cap: `{fmt(data['mcap'])}`\n"
        f"{change_emoji} 24h: `{change:+.2f}%`\n"
        f"💧 Liquidez: `{fmt(data['liquidity'])}`\n"
        f"📦 Volumen 24h: `{fmt(data['volume'])}`\n\n"
        f"🕐 Actualizado ahora"
    )

    keyboard = [
        [InlineKeyboardButton("🐂 The Bull Pen", url="https://bullpen.fi/@Mack")],
        [
            InlineKeyboardButton("📈 DexScreener", url=f"https://dexscreener.com/solana/{TOKEN_ADDRESS}"),
            InlineKeyboardButton("🦅 Birdeye", url=f"https://birdeye.so/token/{TOKEN_ADDRESS}?chain=solana")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pair = get_pair_info()
    if not pair:
        await update.message.reply_text("❌ Error obteniendo datos. Intenta de nuevo en unos segundos.")
        return

    txns     = pair.get("txns", {})
    changes  = pair.get("priceChange", {})
    volumes  = pair.get("volume", {})
    created  = pair.get("pairCreatedAt", 0)

    # Buy/sell counts
    h1_buys  = txns.get("h1",  {}).get("buys",  0)
    h1_sells = txns.get("h1",  {}).get("sells", 0)
    h6_buys  = txns.get("h6",  {}).get("buys",  0)
    h6_sells = txns.get("h6",  {}).get("sells", 0)
    h24_buys  = txns.get("h24", {}).get("buys",  0)
    h24_sells = txns.get("h24", {}).get("sells", 0)

    def pressure(buys, sells):
        total = buys + sells
        if total == 0:
            return "—"
        pct = buys / total * 100
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        return f"{bar} {pct:.0f}% compras"

    msg = (
        f"📊 *$ANSEM — Stats detalladas*\n\n"

        f"*📈 Cambio de precio*\n"
        f"  5m:  {fmt_pct(changes.get('m5',  0) or 0)}\n"
        f"  1h:  {fmt_pct(changes.get('h1',  0) or 0)}\n"
        f"  6h:  {fmt_pct(changes.get('h6',  0) or 0)}\n"
        f"  24h: {fmt_pct(changes.get('h24', 0) or 0)}\n\n"

        f"*📦 Volumen*\n"
        f"  1h:  `{fmt(volumes.get('h1',  0))}`\n"
        f"  6h:  `{fmt(volumes.get('h6',  0))}`\n"
        f"  24h: `{fmt(volumes.get('h24', 0))}`\n\n"

        f"*🔄 Actividad de trading (1h)*\n"
        f"  Compras: `{h1_buys}` · Ventas: `{h1_sells}`\n"
        f"  {pressure(h1_buys, h1_sells)}\n\n"

        f"*🔄 Actividad de trading (6h)*\n"
        f"  Compras: `{h6_buys}` · Ventas: `{h6_sells}`\n"
        f"  {pressure(h6_buys, h6_sells)}\n\n"

        f"*🔄 Actividad de trading (24h)*\n"
        f"  Compras: `{h24_buys}` · Ventas: `{h24_sells}`\n"
        f"  {pressure(h24_buys, h24_sells)}\n\n"

        f"*🕰 Edad del par:* `{pair_age(created) if created else '—'}`"
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


async def alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args

    if not args or args[0].lower() not in ("on", "off"):
        status = "✅ *activas*" if chat_id in subscribed_chats else "❌ *inactivas*"
        await update.message.reply_text(
            f"🐋 Alertas de ballenas (>${WHALE_THRESHOLD:,})\n"
            f"Estado actual: {status}\n\n"
            "• `/alerts on` — activar\n"
            "• `/alerts off` — desactivar",
            parse_mode="Markdown"
        )
        return

    if args[0].lower() == "on":
        subscribed_chats.add(chat_id)
        await update.message.reply_text(
            f"🐋 *Alertas activadas*\n\n"
            f"Te avisaré cuando haya compras o ventas mayores a *${WHALE_THRESHOLD:,}*.",
            parse_mode="Markdown"
        )
    else:
        subscribed_chats.discard(chat_id)
        await update.message.reply_text("🔕 Alertas de ballenas desactivadas.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐂 *BlackBullRadar* — Tracker de $ANSEM\n\n"
        "• /price o /stats → Precio + métricas\n"
        f"• /alerts on → Alertas de ballenas (>${WHALE_THRESHOLD:,})\n"
        "• /alerts off → Desactivar alertas\n"
        "• /help → Este mensaje\n\n"
        "Botones de compra prioritarios llevan a *The Bull Pen*.",
        parse_mode="Markdown"
    )


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("help", help_command))

    app.job_queue.run_repeating(whale_job, interval=60, first=10)

    print("🐂 BlackBullRadar arrancando con monitor de ballenas...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
