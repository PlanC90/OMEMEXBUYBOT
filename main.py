import os
import json
import logging
import asyncio
import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ApplicationBuilder,
)
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Logging ayarlarını DEBUG seviyesine çekerek daha fazla detay görebiliriz.
# Sorun çözüldükten sonra INFO'ya geri dönebilirsiniz.
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG  # DEBUG seviyesine çekildi
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is missing! Please set it.")
    raise RuntimeError("BOT_TOKEN environment variable is missing! Please set it.")

POOL_ADDRESS = os.getenv("POOL_ADDRESS", "0xc84edbf1e3fef5e4583aaa0f818cdfebfcae095b")
INTERVAL = int(os.getenv("INTERVAL", "30"))
TOKEN_NAME = os.getenv("TOKEN_NAME", "OMEMEX")
IMAGE_URL = os.getenv(
    "IMAGE_URL",
    "https://apricot-rational-booby-281.mypinata.cloud/ipfs/bafybeib6snjshzd5n5asfcv42ckuoldzo7gjswctat6wrliz3fnm7zjezm"
)
PORT = int(os.getenv("PORT", "8080"))

CHAT_FILE = "data/chat_id.json"
GECKOTERMINAL_API_URL = f"https://api.geckoterminal.com/api/v2/networks/omax-chain/pools/{POOL_ADDRESS}/trades"
GECKOTERMINAL_POOL_INFO_API_URL = f"https://api.geckoterminal.com/api/v2/networks/omax-chain/pools/{POOL_ADDRESS}"
LARGE_BUY_THRESHOLD_TOKEN = float(os.getenv("LARGE_BUY_THRESHOLD_TOKEN", "5000.0"))
LARGE_BUY_THRESHOLD_USD = float(os.getenv("LARGE_BUY_THRESHOLD_USD", "50.0"))
SWAP_URL = "https://swap.omax.app/swap"
METAMASK_ADD_NETWORK_URL = "https://chainlist.org/?search=omax"

# Global variables
chat_ids = set()
processed_txs = set() # Bot çalıştığı sürece işlenen TX'leri tutar, yeniden başlatmada sıfırlanır.
token_address = None
token_symbols_map = {} # {'omax-chain_0x...': 'OMEMEX', 'omax-chain_0x...': 'WOMAX'}


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
    try:
        if os.path.exists(CHAT_FILE):
            with open(CHAT_FILE, "r") as f:
                data = json.load(f)
                chat_ids = set(data.get("chat_ids", []))
                logger.info(f"Loaded {len(chat_ids)} chat IDs: {chat_ids if len(chat_ids) < 10 else str(list(chat_ids)[:10]) + '...'}")
        else:
            os.makedirs(os.path.dirname(CHAT_FILE), exist_ok=True)
            logger.info("Chat ID file not found, starting with empty set. Data directory created if it didn't exist.")
            chat_ids = set()
    except Exception as e:
        logger.error(f"Error loading chat IDs: {e}", exc_info=True)
        chat_ids = set()


def save_chat_ids():
    try:
        os.makedirs(os.path.dirname(CHAT_FILE), exist_ok=True)
        with open(CHAT_FILE, "w") as f:
            json.dump({"chat_ids": list(chat_ids)}, f)
        logger.info(f"Chat IDs saved. Count: {len(chat_ids)}")
    except Exception as e:
        logger.error(f"Error saving chat IDs: {e}", exc_info=True)


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
    global token_address, token_symbols_map
    logger.debug("Attempting to fetch pool info...")
    try:
        pool_response = requests.get(GECKOTERMINAL_POOL_INFO_API_URL, timeout=10)
        logger.debug(f"Pool info API response status: {pool_response.status_code}")
        pool_response.raise_for_status()
        pool_data = pool_response.json()
        logger.debug(f"Pool info data: {json.dumps(pool_data, indent=2)[:500]}...") # Log first 500 chars of pretty json

        attributes = pool_data.get("data", {}).get("attributes", {})
        base_token_price_usd = float(attributes.get("base_token_price_usd", 0))
        quote_token_price_usd = float(attributes.get("quote_token_price_usd", 0))
        price_change_24h = float(attributes.get("price_change_percentage", {}).get("h24", 0))
        fdv_usd = float(attributes.get("fdv_usd", 0))

        logger.debug(f"Fetched prices: base_usd={base_token_price_usd}, quote_usd={quote_token_price_usd}, fdv_usd={fdv_usd}")

        relationships = pool_data.get("data", {}).get("relationships", {})
        base_token_data = relationships.get("base_token", {}).get("data", {})
        quote_token_data = relationships.get("quote_token", {}).get("data", {})

        base_id_api = base_token_data.get("id")
        quote_id_api = quote_token_data.get("id")

        if base_id_api:
            extracted_token_address = base_id_api.split('_')[-1]
            if not token_address:
                token_address = extracted_token_address
                logger.info(f"Global 'token_address' for {TOKEN_NAME} set to: {token_address} from API base_token_id: {base_id_api}")
            elif token_address != extracted_token_address:
                logger.warning(f"Mismatch in token address! Global: {token_address}, API base_token_id: {base_id_api} (extracted: {extracted_token_address})")

        current_symbols_map = {}
        if base_id_api:
            current_symbols_map[base_id_api] = TOKEN_NAME
        if quote_id_api:
            # Assuming quote token is WOMAX, but it's better to get its symbol if API provides it
            # For now, let's keep it hardcoded or try to derive.
            # quote_token_symbol_api = pool_data.get("included", []) # Check if symbol is in 'included'
            current_symbols_map[quote_id_api] = "WOMAX" # Placeholder, actual symbol might be different

        if token_symbols_map != current_symbols_map and current_symbols_map:
            token_symbols_map = current_symbols_map
            logger.info(f"Updated token_symbols_map: {token_symbols_map}")
        elif not token_symbols_map and current_symbols_map: # First time setting
             token_symbols_map = current_symbols_map
             logger.info(f"Initialized token_symbols_map: {token_symbols_map}")


        return base_token_price_usd, price_change_24h, fdv_usd, quote_token_price_usd
    except requests.exceptions.Timeout:
        logger.error(f"Timeout error fetching pool info from {GECKOTERMINAL_POOL_INFO_API_URL}")
        return None, None, None, None
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching pool info: {e}")
        return None, None, None, None
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error fetching pool info: {pool_response.text[:200]}... Error: {e}")
        return None, None, None, None
    except Exception as e:
        logger.error(f"Unexpected error fetching pool info: {e}", exc_info=True)
        return None, None, None, None


async def fetch_and_process_trades(context: ContextTypes.DEFAULT_TYPE):
    global processed_txs
    logger.debug(f"Running fetch_and_process_trades. Active chats: {len(chat_ids)}. Processed TXs (session): {len(processed_txs)}")

    if not chat_ids:
        logger.debug("No active chat IDs. Skipping trade check for this interval.")
        return

    omemex_price_usd, price_change_24h, market_cap_usd, _ = await get_pool_info()

    if omemex_price_usd is None:
        logger.warning("Could not retrieve pool info (omemex_price_usd is None). Skipping trade check.")
        return

    if not token_address:
        logger.warning("Token address (for OMEMEX) is not set. Cannot reliably identify trades. Skipping.")
        return

    if not token_symbols_map:
        logger.warning("Token symbols map is not set. Trade notifications might have incorrect symbols. Skipping (or proceed with caution).")
        # return # Eğer symbols map olmadan devam etmek istemiyorsanız bu satırı açın

    try:
        logger.debug(f"Fetching trades from {GECKOTERMINAL_API_URL}")
        response = requests.get(GECKOTERMINAL_API_URL, timeout=10)
        logger.debug(f"Trades API response status: {response.status_code}")
        response.raise_for_status()
        trades_data = response.json()
        trades = trades_data.get("data", [])
        logger.info(f"Fetched {len(trades)} trades from API.")
        logger.debug(f"Current processed_txs before filtering: {processed_txs if len(processed_txs) < 5 else str(list(processed_txs)[:5]) + '...'}")


        if not processed_txs and trades: # İlk çalıştırma ve trade'ler var
            processed_txs.update(trade.get("attributes", {}).get("tx_hash") for trade in trades if trade.get("attributes", {}).get("tx_hash"))
            logger.info(f"First run with trades: Initialized processed_txs with {len(processed_txs)} existing trades. No notifications will be sent for these.")
            return

        new_buys_attributes_list = []
        for trade_item in reversed(trades): # API genellikle en yeni trade'i en üste koyar, reversed() en eskiden yeniye gider.
                                        # Eğer API en yeniyi en sona koyuyorsa reversed() olmadan kullanın. Test edin.
            attrs = trade_item.get("attributes", {})
            tx_hash = attrs.get("tx_hash")
            kind = attrs.get("kind") # 'buy' veya 'sell'
            # 'buy' means base_token (OMEMEX) was bought. 'sell' means base_token was sold.
            # to_token is the token received by the taker. For a 'buy' of OMEMEX, OMEMEX is the to_token.
            # from_token is the token spent by the taker.
            
            # API'den gelen to_token'ın ID'si bizim TOKEN_NAME'imizin ID'si ile eşleşmeli (base_token_id)
            # Ya da 'kind' == 'buy' ve base_token bizim tokenımız ise bu bir alımdır.
            # GeckoTerminal'de 'kind: "buy"' genellikle base_token'ın alındığı anlamına gelir.
            
            logger.debug(f"Checking trade: TX_HASH={tx_hash}, KIND={kind}, PriceUSD={attrs.get('price_in_usd')}, VolumeUSD={attrs.get('volume_in_usd')}")
            # logger.debug(f"Full trade attributes for TX {tx_hash}: {attrs}") # Çok detaylı, gerekirse açın

            if tx_hash and kind == "buy" and tx_hash not in processed_txs:
                # OMEMEX alımı olup olmadığını teyit etmek için to_token adresini de kontrol edebilirsiniz,
                # ama `kind == "buy"` ve pool adresinin doğru olması yeterli olmalı.
                # to_token_id_api = attrs.get("to_token", {}).get("id")
                # if to_token_id_api and token_address in to_token_id_api: # Daha spesifik kontrol
                logger.info(f"NEW OMEMEX BUY DETECTED: TX_HASH={tx_hash}, Kind={kind}")
                new_buys_attributes_list.append(attrs)
                # else:
                #    logger.debug(f"TX_HASH={tx_hash} is a 'buy' but to_token_id ({to_token_id_api}) does not match our token_address ({token_address}). Skipping.")
            elif tx_hash in processed_txs:
                logger.debug(f"Trade TX_HASH={tx_hash} (Kind: {kind}) already processed. Skipping.")
            elif kind != "buy":
                logger.debug(f"Trade TX_HASH={tx_hash} is not a buy (Kind: {kind}). Skipping.")
            elif not tx_hash:
                logger.warning("Trade item found with no tx_hash. Skipping.")


        if new_buys_attributes_list:
            logger.info(f"Found {len(new_buys_attributes_list)} new OMEMEX buy transaction(s) to process.")
            for buy_attr_item in new_buys_attributes_list:
                await process_buy_notification(buy_attr_item, omemex_price_usd, price_change_24h, market_cap_usd, context)
                processed_txs.add(buy_attr_item.get("tx_hash")) # Bildirim gönderildikten sonra ekle
            logger.info(f"Finished processing new buys. Total processed TXs in this session: {len(processed_txs)}")
        else:
            logger.info("No new OMEMEX buy transactions found in this interval.")

    except requests.exceptions.Timeout:
        logger.error(f"Timeout error fetching trades from {GECKOTERMINAL_API_URL}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching trades: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error fetching trades: {response.text[:200]}... Error: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in fetch_and_process_trades: {e}", exc_info=True)


async def process_buy_notification(buy_attrs, omemex_price_usd, price_change_24h, market_cap_usd, context):
    tx_hash_for_log = buy_attrs.get('tx_hash', 'UNKNOWN_TX')
    logger.info(f"Processing buy notification for TX_HASH: {tx_hash_for_log}")
    logger.debug(f"Buy attributes for TX {tx_hash_for_log}: {json.dumps(buy_attrs, indent=2)}")
    logger.debug(f"Using omemex_price_usd: {omemex_price_usd}, price_change_24h: {price_change_24h}, market_cap_usd: {market_cap_usd}")

    try:
        # GeckoTerminal 'buy' kind: base_token alınıyor (OMEMEX), quote_token satılıyor (WOMAX)
        # from_token_id: quote_token'ın ID'si (örn: omax-chain_0x...)
        # to_token_id: base_token'ın ID'si (örn: omax-chain_0x...)
        from_token_id_api = buy_attrs.get("from_token", {}).get("id")
        to_token_id_api = buy_attrs.get("to_token", {}).get("id")

        from_token_symbol = token_symbols_map.get(from_token_id_api, "PAID_TOKEN") # Ödenen (örn: WOMAX)
        to_token_symbol = token_symbols_map.get(to_token_id_api, TOKEN_NAME)   # Alınan (TOKEN_NAME)

        logger.debug(f"TX {tx_hash_for_log}: From_token_id={from_token_id_api} (Symbol: {from_token_symbol}), To_token_id={to_token_id_api} (Symbol: {to_token_symbol})")

        to_amount_str = buy_attrs.get("to_token_amount")     # Alınan TOKEN_NAME miktarı
        from_amount_str = buy_attrs.get("from_token_amount") # Ödenen quote token miktarı

        if to_amount_str is None or from_amount_str is None:
            logger.error(f"TX {tx_hash_for_log}: Missing amount data in trade attributes. to_amount: {to_amount_str}, from_amount: {from_amount_str}. Skipping.")
            return

        to_amount = float(to_amount_str)
        from_amount = float(from_amount_str)
        usd_value_api = float(buy_attrs.get("volume_in_usd", 0)) # API'nin hesapladığı USD volume

        # Kendi hesapladığımız USD değeri (omemex_price_usd anlık olduğu için daha doğru olabilir)
        calculated_usd_value = to_amount * omemex_price_usd if omemex_price_usd is not None else 0
        logger.debug(f"TX {tx_hash_for_log}: API USD value: {usd_value_api}, Calculated USD value: {calculated_usd_value}")
        
        # Hangi USD değerini kullanacağımıza karar verelim. API'den gelen daha genel olabilir.
        # Anlık fiyatla hesaplanan daha dinamik. Şimdilik hesaplananı kullanalım.
        display_usd_value = calculated_usd_value

        leading = "🟢"
        if to_amount >= LARGE_BUY_THRESHOLD_TOKEN or display_usd_value >= LARGE_BUY_THRESHOLD_USD:
            leading = "🟢🟢🟢"

        change_text = ""
        if price_change_24h is not None:
            arrow = "▲" if price_change_24h >= 0 else "▼"
            emoji = "🟢" if price_change_24h >= 0 else "🔴"
            change_text = f"📈 **24h Change:** {emoji} `{arrow} {abs(price_change_24h):.2f}%`\n"
        else:
            change_text = "📈 **24h Change:** `N/A`\n"

        tx_url = f"https://omaxray.com/tx/{tx_hash_for_log}"

        message = (
            f"{leading} **New {to_token_symbol} Buy!** {leading}\n\n"
            f"🚀 **Amount Received:** `{to_amount:,.8f}` {to_token_symbol}\n"
            f"💰 **Amount Paid:** `{from_amount:,.8f}` {from_token_symbol}\n"
            f"💲 **Value (USD):** `${display_usd_value:,.2f}`\n"
            f"💵 **Unit Price ({to_token_symbol}):** `${omemex_price_usd:,.10f}`\n"
            f"{change_text}"
            f"📊 **Market Cap ({to_token_symbol}):** `${market_cap_usd:,.2f} USD`\n"
            f"🔐 {TOKEN_NAME} is strictly limited to `300,000,000,000` tokens only.\n"
        )
        logger.info(f"Constructed message for TX {tx_hash_for_log}: {message[:200].replace(chr(10), ' ')}...") # Mesajın başını logla (newline'ları boşlukla değiştirerek)

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
        logger.info(f"Attempting to send notification for TX {tx_hash_for_log} to {len(chat_ids)} chat(s): {list(chat_ids if len(chat_ids) < 5 else list(chat_ids)[:5]) + ['...']}")
        for cid in list(chat_ids):
            logger.debug(f"Sending to chat_id: {cid} for TX {tx_hash_for_log}")
            try:
                await context.bot.send_video(
                    chat_id=cid,
                    video=IMAGE_URL, # Bu URL'nin geçerli bir video olduğundan emin olun
                    caption=message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup,
                )
                sent_to_chats += 1
                logger.debug(f"Successfully sent video for TX {tx_hash_for_log} to chat_id: {cid}")
                await asyncio.sleep(0.2) # Telegram API limitlerine takılmamak için küçük bir bekleme
            except Exception as e:
                failed_chats.append(cid)
                logger.error(f"Error sending message for TX {tx_hash_for_log} to chat ID {cid}: {e}", exc_info=False) # exc_info=False kısa log için
                if "chat not found" in str(e).lower() or \
                   "bot was blocked by the user" in str(e).lower() or \
                   "user is deactivated" in str(e).lower() or \
                   "group chat was deactivated" in str(e).lower() or \
                   "bot was kicked" in str(e).lower(): # Daha genel "kicked"
                    logger.info(f"Removing invalid chat ID: {cid} due to error: {str(e)[:50]}")
                    chat_ids.discard(cid)
                    save_chat_ids() # Her silme sonrası kaydetmek disk I/O'sunu artırır, toplu silme düşünülebilir. Şimdilik kalsın.
        logger.info(f"Notification for TX {tx_hash_for_log} sent to {sent_to_chats}/{len(chat_ids) + len(failed_chats)} chats. Failed for: {failed_chats if failed_chats else 'None'}")

    except Exception as e:
        logger.error(f"Critical error in process_buy_notification for TX {tx_hash_for_log}: {e}", exc_info=True)


async def health_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot is running! ✅ (Telegram Handler Active)")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE): # Adını değiştirdim
    active_chats = len(chat_ids)
    processed_count = len(processed_txs)
    omemex_p, price_c_24h, market_c_usd, _ = await get_pool_info()

    status_msg = (
        f"📊 **{TOKEN_NAME} Bot Status**\n\n"
        f"⚙️ Bot Version: `1.1` (Debug Logging)\n" # Örnek versiyon
        f"🔔 Active Chats: `{active_chats}`\n"
        f"🔄 Processed TXs (session): `{processed_count}`\n"
        f"⏱️ Check Interval: `{INTERVAL} seconds`\n"
        f"💎 Target Token: `{TOKEN_NAME}` (Address: `{token_address or 'Not set'}`)\n"
    )
    if omemex_p is not None:
        status_msg += (
            f"💵 Current Price: `${omemex_p:,.10f}`\n"
            f"📈 24h Change: `{price_c_24h:.2f}%`\n"
            f"💰 Market Cap: `${market_c_usd:,.2f}`\n"
        )
    else:
        status_msg += "⚠️ Could not fetch current token price/market cap.\n"
    
    status_msg += f"🗺️ Token Symbols Map: `{json.dumps(token_symbols_map)}`\n"

    await update.message.reply_text(status_msg, parse_mode='Markdown')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
    if update and hasattr(update, 'message') and update.message:
         await update.message.reply_text("An error occurred while processing your request. The developers have been notified.")
    elif update and hasattr(update, 'callback_query') and update.callback_query:
        await context.bot.answer_callback_query(
            callback_query_id=update.callback_query.id,
            text="An error occurred. Please try again.",
            show_alert=True
        )

async def main():
    health_thread = None
    app = None
    try:
        logger.info(f"Starting {TOKEN_NAME} Buy Bot...")
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()

        load_chat_ids()
        logger.info("Attempting initial pool info fetch to set up token details...")
        await get_pool_info() # token_address ve token_symbols_map'i doldurur
        if not token_address:
            logger.warning("Could not determine token_address on initial setup. Will retry in background.")
        if not token_symbols_map:
            logger.warning("Could not determine token_symbols_map on initial setup. Will retry in background.")

        app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        app.add_handler(CommandHandler("omemexbuystart", start_memexbuy))
        app.add_handler(CommandHandler("omemexbuystop", stop_memexbuy))
        app.add_handler(CommandHandler("health", health_check_command))
        app.add_handler(CommandHandler("status", status_command)) # Komut adını güncelledim
        app.add_error_handler(error_handler)

        await app.initialize()
        logger.info("Telegram Application initialized.")

        async def job_callback(context_job: ContextTypes.DEFAULT_TYPE):
            await fetch_and_process_trades(context_job)

        if app.job_queue:
            app.job_queue.run_repeating(job_callback, interval=INTERVAL, first=max(10, INTERVAL // 2)) # ilk çalıştırma için makul bir süre
            logger.info(f"Job queue started. Trades will be checked every {INTERVAL} seconds. First check in ~{max(10, INTERVAL // 2)}s.")
        else:
            logger.critical("JobQueue is not available after app.initialize(). Bot cannot function correctly.")
            # Bu durumda botun ana işlevi çalışmayacaktır.
            # Acil çıkış yapabilir veya bir uyarı mekanizması çalıştırabilirsiniz.
            return # veya raise Exception(...)

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
            if app.updater and app.updater.is_running:
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
    if not BOT_TOKEN: # main'e girmeden önce kritik bir kontrol
        # Zaten yukarıda raise ediyor ama burada da loglayabiliriz.
        logger.critical("BOT_TOKEN is not set. Exiting.")
    else:
        try:
            asyncio.run(main())
        except RuntimeError as e:
            if "event loop is already running" in str(e) or "Cannot close a running event loop" in str(e):
                logger.critical(f"Asyncio event loop conflict detected at script exit: {e}. This might indicate an issue with how asyncio.run() interacts with the environment or libraries.")
            else:
                logger.critical(f"Unhandled RuntimeError at script exit: {e}", exc_info=True)
        except Exception as e:
            logger.critical(f"Unhandled exception at script exit: {e}", exc_info=True)
