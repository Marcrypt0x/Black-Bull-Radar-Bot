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
    total_holders = 0
    page = 1
    limit = 1000
    try:
        while True:
            payload = {
                "jsonrpc": "2.0",
                "id": f"holders-{page}",
                "method": "getTokenAccounts",
                "params": {
                    "mint": TOKEN_ADDRESS,
                    "limit": limit,
                    "page": page
                }
            }
            r = requests.post(HELIUS_RPC, json=payload, timeout=20)
            data = r.json()
            if "error" in data:
                logger.error(f"Helius error page {page}: {data['error']}")
                break
            result = data.get("result")
            if not result:
                break
            if "total" in result and page == 1:
                return int(result["total"])
            token_accounts = result.get("token_accounts") or result.get("tokenAccounts") or []
            count = len(token_accounts)
            if count == 0:
                break
            total_holders += count
            if count < limit:
                break
            page += 1
            if page > 20:
                break
        return total_holders if total_holders > 0 else None
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
    return solana_post
