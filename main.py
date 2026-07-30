import os
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN", "REPLACE_ME")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WHALE_THRESHOLD = 5_500  # USD
SOLANA_RPC = "https://api.mainnet-beta.solana.com"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory state
subscribed_chats: set[int] = set()
seen_signatures: set[str] = set()
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
    """
    Extract trade info from a Solana transaction.
    Looks at token balance changes for TOKEN_ADDRESS,
    excludes the pool account itself, and returns the
    largest individual trade found.
    """
    if not tx:
        return None
    meta = tx.get("meta", {})
    if meta.get("err"):
        return None  # failed tx

    pre_balances  = {e["accountIndex"]: e for e in meta.get("preTokenBalances",  []) if e.get("mint") == TOKEN_ADDRESS}
    post_balances = {e["accountIndex"]: e for e in meta.get("postTokenBalances", []) if e.get("mint") == TOKEN_ADDRESS}

    all_indexes = set(pre_balances) | set(post_balances)
    best = None

    for idx in all_indexes:
        pre_entry  = pre_balances.get(idx,  {})
        post_entry = post_balances.get(idx, {})

        owner = (pre_entry or post_entry).get("owner", "")
        if owner == pool_owner:
            continue  # skip the pool's own account

        pre_amt  = float((pre_entry.get("uiTokenAmount")  or {}).get("uiAmount") or 0)
        post_amt = float((post_entry.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        delta    = post_amt - pre_amt  # + = bought, – = sold

        usd_value = abs(delta) * current_price
        if usd_value < WHALE_THRESHOLD:
            continue

        if best is None or usd_value > best["usd_value"]:
            best = {
                "side":      "BUY" if delta > 0 else "SELL",
                "tokens":    abs(delta),
                "usd_value": usd_value,
                "price":     current_price,
                "wallet":    owner,
            }

    return best


# ── Whale monitor (background job) ──────────────────────────────────────────

async def whale_job(context) -> None:
    addr = resolve_pair_address()
    if not addr:
        logger.warning("whale_job: pair address not resolved")
        return

    # Get current price for USD conversion
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
            continue  # skip failed txs

        tx = get_transaction(sig)
        whale = parse_whale_trade(tx, current_price, pool_owner=addr)
        if whale:
            whale["sig"] = sig
            new_whales.append(whale)

    if not new_whales or not subscribed_chats:
        return

    for whale in new_whales:
        emoji = "🟢" if whale["side"] == "BUY" else "🔴"
        short_sig = whale["sig"][:12] + "…"
        short_wallet = whale["wallet"][:8] + "…" + whale["wallet"][-4:]
        msg = (
            f"{emoji} *Whale Alert — ${whale['usd_value']:,.0f}*\n\n"
            f"Tipo: `{whale['side']}`\n"
            f"Tokens: `{whale['tokens']:,.2f} $ANSEM`\n"
            f"Precio: `${whale['price']:.6f}`\n"
            f"Wallet: `{short_wallet}`\n"
            f"TX: `{short_sig}`"
        )
        for chat_id in list(subscribed_chats):
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
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
        parse_mode="Markdown",
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
            f"Alertas de ballenas (>${WHALE_THRESHOLD:,} USD): {status}\n\n"
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
        parse_mode="Markdown",
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("price",  price))
    app.add_handler(CommandHandler("stats",  stats))
    app.add_handler(CommandHandler("alerts", alerts))
    app.add_handler(CommandHandler("help",   help_command))

    # Poll for whale trades every 60 seconds
    app.job_queue.run_repeating(whale_job, interval=60, first=10)

    print("BlackBullRadar arrancando con monitor de ballenas (Solana RPC)...")
    app.run_polling()


if __name__ == "__main__":
    main()
