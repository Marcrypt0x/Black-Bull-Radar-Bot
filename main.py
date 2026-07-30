import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ.get("TELEGRAM_TOKEN", "REPLACE_ME")
TOKEN_ADDRESS = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"
WHALE_THRESHOLD = 5_500  # USD
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "")
HELIUS_RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}" if HELIUS_API_KEY else None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

subscribed_chats: set[int] = set()
seen_signatures: set[str] = set()
pair_address: str | None = None


def get_pair_info() -> dict | None:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{TOKEN_ADDRESS}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("pairs"):
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
        "liquidity": pair.get("liquidity",
