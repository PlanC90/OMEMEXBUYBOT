import os
import json
import logging
import asyncio
import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
)
from telegram.constants import ParseMode # ParseMode'u import ediyoruz
from telegram.helpers import escape_markdown # MarkdownV2 için escape fonksiyonu
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import re # Regex için

# Logging ayarları
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is missing! Please set it.")
    raise RuntimeError("BOT_TOKEN environment variable is missing! Please set it.")

POOL_ADDRESS = os.getenv("POOL_ADDRESS", "0xc84edbf1e3fef5e4583aaa0f818cdfebfcae095b")
INTERVAL = int(os.getenv("INTERVAL", "30"))
TOKEN_NAME = os.getenv("TOKEN_NAME", "OMEMEX") # Bu base token'ın adı
QUOTE_TOKEN_SYMBOL = os.getenv("QUOTE_TOKEN_SYMBOL", "WOMAX") # Bu quote token'ın adı
IMAGE_URL = os.getenv(
    "IMAGE_URL",
    "https://apricot-rational-booby-281.mypinata.cloud/ipfs/bafybeib6snjshzd5n5asfcv42ckuoldzo7gjswctat6wrliz3fnm7zjezm"
)
PORT = int(os.getenv("PORT", "8080"))

CHAT_FILE = "data/chat_id.json"
GECKOTERMINAL_API_URL = f"https://api.geckoterminal.com/api/v2/networks/omax-chain/pools/{POOL_ADDRESS}/trades"
GECKOTERMINAL_POOL_INFO_API_URL = f"https://api.geckoterminal.com/api/v2/networks/omax-chain/pools/{POOL_ADDRESS}?include=base_token,quote_token" # Token detaylarını da çekelim
LARGE_BUY_THRESHOLD_TOKEN = float(os.getenv("LARGE_BUY_THRESHOLD_TOKEN", "5000.0"))
LARGE_BUY_THRESHOLD_USD = float(os.getenv("LARGE_BUY_THRESHOLD_USD", "50.0"))
SWAP_URL = "https://swap.omax.app/swap"
METAMASK_ADD_NETWORK_URL = "https://chainlist.org/?search=omax"

# Global variables
chat_ids = set()
processed_txs = set()
base_token_api_id = None # örn: omax-chain_0xTOKENADRESI (TOKEN_NAME için)
quote_token_api_id = None # örn: omax-chain_0xTOKENADRESI (QUOTE_TOKEN_SYMBOL için)
token_symbols_map = {} # {'omax-chain_0x...': 'OMEMEX', 'omax-chain_0x...': 'WOMAX'}


def escape_md_v2(text: str) -> str:
    """Helper function to escape text for MarkdownV2."""
    return escape_markdown(str(text), version=2)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')
    def log_message(self, format, *args):
        pass


def start_health_server():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
        logger.info(f"Health check server starting on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Could not start health check server: {e}", exc_info=True)

def load_chat_ids():
    global chat_ids
    logger.info(f"Attempting to load chat IDs from: {CHAT_FILE}")
    try:
        data_dir = os.path.dirname(CHAT_FILE)
        if not os.path.exists(data_dir) and data_dir: # data_dir boş değilse kontrol et
            logger.info(f"Data directory '{data_dir}' not found. Creating it.")
            os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(CHAT_FILE):
            with open(CHAT_FILE, "r") as f:
                data = json.load(f)
                chat_ids = set(data.get("chat_ids", []))
                logger.info(f"Loaded {len(chat_ids)} chat IDs.")
        else:
            logger.info(f"Chat ID file '{CHAT_FILE}' not found, starting with empty set.")
            chat_ids = set()
    except Exception as e:
        logger.error(f"Error loading chat IDs from '{CHAT_FILE}': {e}", exc_info=True)
        chat_ids = set()

def save_chat_ids():
    logger.info(f"Attempting to save chat IDs to: {CHAT_FILE}")
    try:
        data_dir = os.path.dirname(CHAT_FILE)
        if not os.path.exists(data_dir) and data_dir:
            logger.info(f"Data directory '{data_dir}' for saving not found. Creating it.")
            os.makedirs(data_dir, exist_ok=True)
        with open(CHAT_FILE, "w") as f:
            json.dump({"chat_ids": list(chat_ids)}, f)
        logger.info(f"Chat IDs saved to '{CHAT_FILE}'. Count: {len(chat_ids)}")
    except Exception as e:
        logger.error(f"Error saving chat IDs to '{CHAT_FILE}': {e}", exc_info=True)

async def start_memexbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        save_chat_ids()
        await update.message.reply_text(f"{TOKEN_NAME} buy notifications enabled for this chat!")
        logger.info(f"Added chat ID: {chat_id}. Total active chats: {len(chat_ids)}")
    else:
        await update.message.reply_text(f"{TOKEN_NAME} buy notifications are already enabled for this chat.")
        logger.info(f"Chat ID: {chat_id} attempted to start notifications again, already active.")

async def stop_memexbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        save_chat_ids()
        await update.message.reply_text(f"{TOKEN_NAME} buy notifications disabled for this chat.")
        logger.info(f"Removed chat ID: {chat_id}. Total active chats: {len(chat_ids)}")
    else:
        await update.message.reply_text("You have not enabled notifications for this chat yet.")

async def get_pool_info():
    global base_token_api_id, quote_token_api_id, token_symbols_map
    logger.debug(f"Attempting to fetch pool info from: {GECKOTERMINAL_POOL_INFO_API_URL}")
    try:
        pool_response = requests.get(GECKOTERMINAL_POOL_INFO_API_URL, timeout=15)
        logger.debug(f"Pool info API response status: {pool_response.status_code}")
        pool_response.raise_for_status()
        pool_data = pool_response.json()
        # logger.debug(f"Pool info data: {json.dumps(pool_data, indent=2)[:1000]}...")

        attributes = pool_data.get("data", {}).get("attributes", {})
        base_token_price_usd = float(attributes.get("base_token_price_usd", 0)) # Bu TOKEN_NAME fiyatı
        # quote_token_price_usd = float(attributes.get("quote_token_price_usd", 0)) # Bu QUOTE_TOKEN fiyatı
        price_change_24h = float(attributes.get("price_change_percentage", {}).get("h24", 0))
        fdv_usd = float(attributes.get("fdv_usd", 0)) # Bu base token (TOKEN_NAME) FDV'si

        logger.debug(f"Fetched prices: base_usd={base_token_price_usd}, fdv_usd={fdv_usd}, 24h_change={price_change_24h}%")

        # Token ID'lerini ve sembollerini alalım
        # `?include=base_token,quote_token` sayesinde `included` kısmında token detayları olmalı.
        # VEYA relationships altında da olabilir. API yanıtını kontrol etmek önemli.
        
        # Relationships'ten ID'leri al
        relationships = pool_data.get("data", {}).get("relationships", {})
        current_base_token_api_id = relationships.get("base_token", {}).get("data", {}).get("id")
        current_quote_token_api_id = relationships.get("quote_token", {}).get("data", {}).get("id")

        if current_base_token_api_id and base_token_api_id != current_base_token_api_id:
            base_token_api_id = current_base_token_api_id
            logger.info(f"Base token API ID set/updated: {base_token_api_id}")
        if current_quote_token_api_id and quote_token_api_id != current_quote_token_api_id:
            quote_token_api_id = current_quote_token_api_id
            logger.info(f"Quote token API ID set/updated: {quote_token_api_id}")

        # 'included' kısmından sembolleri ve isimleri çekmeye çalışalım
        new_symbols_map = {}
        included_tokens = pool_data.get("included", [])
        for token_data in included_tokens:
            if token_data.get("type") == "token":
                token_id = token_data.get("id")
                symbol = token_data.get("attributes", {}).get("symbol", "N/A_SYMBOL")
                # name = token_data.get("attributes", {}).get("name", "N/A_NAME")
                if token_id == base_token_api_id:
                    new_symbols_map[token_id] = TOKEN_NAME # Ortam değişkenindeki ismi kullan
                elif token_id == quote_token_api_id:
                    new_symbols_map[token_id] = QUOTE_TOKEN_SYMBOL # Ortam değişkenindeki ismi kullan
                else: # Diğer olası tokenlar için API sembolünü kullan
                    new_symbols_map[token_id] = symbol 
        
        # Eğer included'dan alınamazsa, varsayılanları ata
        if base_token_api_id and base_token_api_id not in new_symbols_map:
            new_symbols_map[base_token_api_id] = TOKEN_NAME
        if quote_token_api_id and quote_token_api_id not in new_symbols_map:
            new_symbols_map[quote_token_api_id] = QUOTE_TOKEN_SYMBOL
            
        if token_symbols_map != new_symbols_map and new_symbols_map:
            token_symbols_map = new_symbols_map
            logger.info(f"Updated/Initialized token_symbols_map: {token_symbols_map}")

        return base_token_price_usd, price_change_24h, fdv_usd
        
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout error fetching pool info from {GECKOTERMINAL_POOL_INFO_API_URL}")
        return None, None, None
    except requests.exceptions.RequestException as e:
        logger.warning(f"Network error fetching pool info: {e}")
        return None, None, None
    except json.JSONDecodeError as e:
        response_text = pool_response.text if 'pool_response' in locals() else "No response object"
        logger.error(f"JSON decode error fetching pool info. Response (first 200 chars): '{response_text[:200]}'. Error: {e}")
        return None, None, None
    except Exception as e:
        logger.error(f"Unexpected error fetching pool info: {e}", exc_info=True)
        return None, None, None

async def fetch_and_process_trades(context: ContextTypes.DEFAULT_TYPE):
    global processed_txs
    logger.debug(f"Running fetch_and_process_trades. Active chats: {len(chat_ids)}. Processed TXs (session): {len(processed_txs)}")

    if not chat_ids:
        logger.debug("No active chat IDs. Skipping trade check.")
        return

    omemex_price_usd, price_change_24h, market_cap_usd = await get_pool_info()

    if omemex_price_usd is None:
        logger.warning("Could not retrieve pool info (omemex_price_usd is None). Skipping trade check.")
        return
    if not base_token_api_id: # Base token ID'si olmadan hangi token'ı izlediğimizi bilemeyiz
        logger.warning("Base token API ID (for OMEMEX) is not set from pool info. Cannot reliably identify trades. Skipping.")
        return

    try:
        logger.debug(f"Fetching trades from {GECKOTERMINAL_API_URL}")
        response = requests.get(GECKOTERMINAL_API_URL, timeout=10)
        logger.debug(f"Trades API response status: {response.status_code}")
        response.raise_for_status()
        trades_data = response.json()
        trades = trades_data.get("data", [])
        logger.info(f"Fetched {len(trades)} trades from API.")

        if not processed_txs and trades:
            processed_txs.update(trade.get("attributes", {}).get("tx_hash") for trade in trades if trade.get("attributes", {}).get("tx_hash"))
            logger.info(f"First run with trades: Initialized processed_txs with {len(processed_txs)} existing trades. Notifications will be skipped for these.")
            return

        new_buys_attributes_list = []
        # API en yeni trade'i listenin başına koyar. Bu yüzden reversed() KULLANMIYORUZ.
        for trade_item in trades:
            attrs = trade_item.get("attributes", {})
            tx_hash = attrs.get("tx_hash")
            kind = attrs.get("kind") # 'buy' veya 'sell'
            
            # GeckoTerminal'de 'kind: "buy"' pool'un base_token'ının alındığı anlamına gelir.
            # Base token'ımız TOKEN_NAME.
            if tx_hash and kind == "buy" and tx_hash not in processed_txs:
                # Ekstra bir kontrol: to_token gerçekten bizim base_token'ımız mı?
                # trade_to_token_id = attrs.get("to_token_address") # API yanıtına göre bu alanın adı değişebilir.
                # Veya attrs.get("to_token", {}).get("id") olabilir, API'yi inceleyin.
                # if trade_to_token_id == base_token_api_id: # (veya base_token_api_id.endswith(trade_to_token_id) gibi)
                logger.info(f"NEW {TOKEN_NAME} BUY DETECTED: TX_HASH={tx_hash}, Kind={kind}")
                new_buys_attributes_list.append(attrs)
                # else:
                #    logger.debug(f"TX_HASH={tx_hash} is 'buy' but to_token ({trade_to_token_id}) is not our base_token ({base_token_api_id}). Skipping.")
            elif tx_hash in processed_txs:
                pass 
            elif kind != "buy":
                pass
            elif not tx_hash:
                logger.warning("Trade item found with no tx_hash. Skipping.")

        if new_buys_attributes_list:
            logger.info(f"Found {len(new_buys_attributes_list)} new {TOKEN_NAME} buy transaction(s) to process.")
            for buy_attr_item in new_buys_attributes_list:
                await process_buy_notification(buy_attr_item, omemex_price_usd, price_change_24h, market_cap_usd, context)
                processed_txs.add(buy_attr_item.get("tx_hash"))
            logger.info(f"Finished processing new buys. Total processed TXs in this session: {len(processed_txs)}")
        # else:
            # logger.info(f"No new {TOKEN_NAME} buy transactions found in this interval.") # Çok sık log olmaması için kapalı

    except requests.exceptions.Timeout:
        logger.warning(f"Timeout error fetching trades from {GECKOTERMINAL_API_URL}")
    except requests.exceptions.RequestException as e:
        logger.warning(f"Network error fetching trades: {e}")
    except json.JSONDecodeError as e:
        response_text = response.text if 'response' in locals() else "No response object"
        logger.error(f"JSON decode error fetching trades. Response (first 200 chars): '{response_text[:200]}'. Error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in fetch_and_process_trades: {e}", exc_info=True)

async def process_buy_notification(buy_attrs, omemex_price_usd, price_change_24h, market_cap_usd, context):
    tx_hash_for_log = buy_attrs.get('tx_hash', 'UNKNOWN_TX')
    logger.info(f"Processing buy notification for TX_HASH: {tx_hash_for_log}")
    # logger.debug(f"Buy attributes for TX {tx_hash_for_log}: {json.dumps(buy_attrs, indent=2)}")

    try:
        # API'den gelen token ID'lerini kullan
        api_from_token_id = buy_attrs.get("from_token", {}).get("id") # Ödenen token (örn: quote_token_api_id)
        api_to_token_id = buy_attrs.get("to_token", {}).get("id")     # Alınan token (örn: base_token_api_id)

        from_token_symbol = token_symbols_map.get(api_from_token_id, QUOTE_TOKEN_SYMBOL)
        to_token_symbol = token_symbols_map.get(api_to_token_id, TOKEN_NAME)
        
        logger.debug(f"TX {tx_hash_for_log}: From_token_id={api_from_token_id} (Symbol: {from_token_symbol}), To_token_id={api_to_token_id} (Symbol: {to_token_symbol})")

        to_amount_str = buy_attrs.get("to_token_amount")     # Alınan TOKEN_NAME miktarı
        from_amount_str = buy_attrs.get("from_token_amount") # Ödenen quote token miktarı
        usd_volume_from_api = float(buy_attrs.get("volume_in_usd", 0)) # API'nin verdiği USD hacmi

        if to_amount_str is None or from_amount_str is None:
            logger.error(f"TX {tx_hash_for_log}: Missing amount data. to_amount: {to_amount_str}, from_amount: {from_amount_str}. Skipping.")
            return

        to_amount = float(to_amount_str)
        from_amount = float(from_amount_str)
        
        # USD değeri için API'den geleni kullanalım, genellikle daha tutarlı olur.
        display_usd_value = usd_volume_from_api
        if display_usd_value == 0 and omemex_price_usd is not None and to_amount > 0: # API'den gelmezse hesapla
            display_usd_value = to_amount * omemex_price_usd
            logger.debug(f"TX {tx_hash_for_log}: Used calculated USD value: {display_usd_value} as API volume was 0.")


        leading = "🟢"
        if to_amount >= LARGE_BUY_THRESHOLD_TOKEN or display_usd_value >= LARGE_BUY_THRESHOLD_USD:
            leading = "🟢🟢🟢"

        change_text_val = ""
        if price_change_24h is not None:
            arrow = "▲" if price_change_24h >= 0 else "▼"
            emoji = "🟢" if price_change_24h >= 0 else "🔴"
            # Yüzdeyi escape etmeye gerek yok, ` ` içinde değil.
            change_text_val = f"{emoji} {arrow} {abs(price_change_24h):.2f}%"
        else:
            change_text_val = "N/A"

        # Sayıları MarkdownV2 için güvenli hale getirelim (özellikle virgüllü ve noktalı)
        # `escape_md_v2` fonksiyonu . ! - gibi karakterleri escape eder.
        # Ancak f-string formatlaması (,: .8f gibi) zaten string döndürür.
        # Bu stringleri escape etmek, formatlamayı bozabilir.
        # Sadece Telegram'ın sorun çıkarabileceği özel karakterleri içeren stringleri escape etmeliyiz.
        # Şimdilik doğrudan kullanalım, sorun devam ederse `escape_md_v2` ile sarmalarız.

        # Virgüllü sayılar için formatlama
        s_to_amount = f"{to_amount:,.8f}" 
        s_from_amount = f"{from_amount:,.8f}"
        s_display_usd_value = f"{display_usd_value:,.2f}"
        s_omemex_price_usd = f"{omemex_price_usd:,.10f}" # 10 ondalık çok fazla olabilir, Telegram keser
        s_market_cap_usd = f"{market_cap_usd:,.2f}"

        message = (
            f"{leading} *New {escape_md_v2(to_token_symbol)} Buy\\!* {leading}\n\n"
            f"🚀 *Amount Received:* `{s_to_amount}` {escape_md_v2(to_token_symbol)}\n"
            f"💰 *Amount Paid:* `{s_from_amount}` {escape_md_v2(from_token_symbol)}\n"
            f"💲 *Value \\(USD\\):* `${s_display_usd_value}`\n" # $ işaretini escape etmeye gerek yok ` ` içinde
            f"💵 *Unit Price \\({escape_md_v2(to_token_symbol)}\\):* `${s_omemex_price_usd}`\n"
            f"📈 *24h Change:* {change_text_val}\n"
            f"📊 *Market Cap \\({escape_md_v2(to_token_symbol)}\\):* `${s_market_cap_usd} USD`\n"
            f"🔐 _{escape_md_v2(TOKEN_NAME)} is strictly limited to `300,000,000,000` tokens only\\._\n"
        )
        logger.info(f"Constructed message for TX {tx_hash_for_log} (first 200 chars, newlines replaced): {message[:200].replace(chr(10), ' ')}...")

        buttons = [
            [InlineKeyboardButton("View Transaction", url=tx_url)],
            [InlineKeyboardButton(f"Swap {TOKEN_NAME}", url=SWAP_URL)],
            [InlineKeyboardButton("Add Omax Mainnet to MetaMask", url=METAMASK_ADD_NETWORK_URL)],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        if not chat_ids:
            logger.warning(f"No chat IDs to send notification for TX {tx_hash_for_log}.")
            return

        sent_to_chats = 0
        failed_chats = []
        active_chat_list = list(chat_ids)
        logger.info(f"Attempting to send notification for TX {tx_hash_for_log} to {len(active_chat_list)} chat(s).")
        for cid in active_chat_list:
            logger.debug(f"Sending to chat_id: {cid} for TX {tx_hash_for_log}")
            try:
                await context.bot.send_video(
                    chat_id=cid,
                    video=IMAGE_URL,
                    caption=message,
                    parse_mode=ParseMode.MARKDOWN_V2, # Açıkça MarkdownV2 kullan
                    reply_markup=reply_markup,
                )
                sent_to_chats += 1
                logger.debug(f"Successfully sent video for TX {tx_hash_for_log} to chat_id: {cid}")
                await asyncio.sleep(0.5) # Rate limit için biraz daha artırıldı
            except Exception as e:
                failed_chats.append(cid)
                logger.error(f"Error sending message for TX {tx_hash_for_log} to chat ID {cid}: {e}", exc_info=False)
                error_str = str(e).lower()
                if "chat not found" in error_str or \
                   "bot was blocked by the user" in error_str or \
                   "user is deactivated" in error_str or \
                   "group chat was deactivated" in error_str or \
                   "bot was kicked" in error_str or \
                   "parse entities" in error_str: # Parse hatasını da yakala
                    if "parse entities" not in error_str: # Sadece chat ile ilgiliyse sil
                        logger.info(f"Removing invalid chat ID: {cid} due to error: {str(e)[:100]}")
                        if cid in chat_ids:
                            chat_ids.remove(cid)
                            save_chat_ids()
                    else: # Parse hatası ise genel bir sorun var demektir.
                        logger.error(f"MARKDOWN PARSE ERROR for TX {tx_hash_for_log}. Message might be malformed. Error: {e}")
        logger.info(f"Notification for TX {tx_hash_for_log} sent to {sent_to_chats}/{len(active_chat_list)} chats. Failed for: {failed_chats if failed_chats else 'None'}")

    except Exception as e:
        logger.error(f"Critical error in process_buy_notification for TX {tx_hash_for_log}: {e}", exc_info=True)


async def health_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running! ✅ (Telegram Handler Active)")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_chats = len(chat_ids)
    processed_count = len(processed_txs)
    omemex_p, price_c_24h, market_c_usd = await get_pool_info()

    status_msg_parts = [
        f"*📊 {escape_md_v2(TOKEN_NAME)} Bot Status*\n",
        f"⚙️ Bot Version: `1.3 \\(MarkdownV2 Fix\\)`",
        f"🔔 Active Chats: `{active_chats}`",
        f"🔄 Processed TXs \\(session\\): `{processed_count}`",
        f"⏱️ Check Interval: `{INTERVAL} seconds`",
        f"💎 Target Token: `{escape_md_v2(TOKEN_NAME)}` \\(Base ID: `{escape_md_v2(str(base_token_api_id))}`\\)",
    ]
    if omemex_p is not None:
        status_msg_parts.extend([
            f"💵 Current Price: `${omemex_p:,.10f}`", # $ ` ` içinde sorun olmamalı
            f"📈 24h Change: `{price_c_24h:.2f}%`",
            f"💰 Market Cap: `${market_c_usd:,.2f}`"
        ])
    else:
        status_msg_parts.append("⚠️ Could not fetch current token price/market cap\\.")
    
    status_msg_parts.append(f"🗺️ Token Symbols Map: `{escape_md_v2(json.dumps(token_symbols_map))}`")
    status_msg_parts.append(f"📂 Chat File: `{escape_md_v2(CHAT_FILE)}`")

    await update.message.reply_text("\n".join(status_msg_parts), parse_mode=ParseMode.MARKDOWN_V2)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    # Kullanıcıya genel hata mesajı (isteğe bağlı)
    # if update and hasattr(update, 'message') and update.message:
    #    await update.message.reply_text("An error occurred. The developers have been notified.")


async def main():
    health_thread = None
    app = None
    try:
        logger.info(f"Starting {TOKEN_NAME} Buy Bot...")
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()

        load_chat_ids()
        logger.info("Attempting initial pool info fetch to set up token details...")
        await get_pool_info()
        if not base_token_api_id:
            logger.warning("Could not determine base_token_api_id on initial setup.")
        if not token_symbols_map:
            logger.warning("Could not determine token_symbols_map on initial setup.")

        app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        app.add_handler(CommandHandler("omemexbuystart", start_memexbuy))
        app.add_handler(CommandHandler("omemexbuystop", stop_memexbuy))
        app.add_handler(CommandHandler("health", health_check_command))
        app.add_handler(CommandHandler("status", status_command))
        app.add_error_handler(error_handler)

        await app.initialize()
        logger.info("Telegram Application initialized.")

        async def job_callback(context_job: ContextTypes.DEFAULT_TYPE):
            await fetch_and_process_trades(context_job)

        if app.job_queue:
            first_run_delay = max(10, INTERVAL // 3)
            app.job_queue.run_repeating(job_callback, interval=INTERVAL, first=first_run_delay)
            logger.info(f"Job queue started. Trades will be checked every {INTERVAL} seconds. First check in ~{first_run_delay}s.")
        else:
            logger.critical("JobQueue is NOT available after app.initialize(). Bot's core functionality will NOT work.")
            return

        await app.start()
        logger.info("Telegram Application background tasks (job_queue) started.")

        logger.info("Starting Telegram bot polling...")
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        logger.info(f"{TOKEN_NAME} Buy Bot is now fully operational and polling for updates.")

        while True:
            await asyncio.sleep(3600)
            logger.debug(f"Main loop heartbeat. Active chats: {len(chat_ids)}. Processed TXs (session): {len(processed_txs)}")

    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError) as e:
        logger.info(f"Shutdown signal ({type(e).__name__}) received. Cleaning up...")
    except Exception as e:
        logger.critical(f"Critical error in main function: {e}", exc_info=True)
    finally:
        logger.info("Initiating graceful shutdown of the Telegram bot...")
        if app:
            if app.updater and app.updater.running:
                logger.info("Stopping updater polling...")
                await app.updater.stop()
            if app.running:
                logger.info("Stopping application (jobs, etc.)...")
                await app.stop()
            logger.info("Performing final application shutdown...")
            await app.shutdown()
        else:
            logger.info("Application object was not created/available. No Telegram shutdown needed.")
        logger.info(f"{TOKEN_NAME} Buy Bot shutdown sequence completed.")


if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is not set. Exiting application.")
    else:
        try:
            asyncio.run(main())
        except RuntimeError as e:
            if "event loop is already running" in str(e) or "Cannot close a running event loop" in str(e):
                logger.critical(f"Asyncio event loop conflict detected at script exit: {e}.")
            else:
                logger.critical(f"Unhandled RuntimeError at script exit: {e}", exc_info=True)
        except Exception as e:
            logger.critical(f"Unhandled exception at script exit: {e}", exc_info=True)
