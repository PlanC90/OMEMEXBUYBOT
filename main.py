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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing! Please set it.")

POOL_ADDRESS = os.getenv("POOL_ADDRESS", "0xc84edbf1e3fef5e4583aaa0f818cdfebfcae095b")
INTERVAL = int(os.getenv("INTERVAL", "30"))
TOKEN_NAME = os.getenv("TOKEN_NAME", "OMEMEX")
IMAGE_URL = os.getenv(
    "IMAGE_URL",
    "https://apricot-rational-booby-281.mypinata.cloud/ipfs/bafybeib6snjshzd5n5asfcv42ckuoldzo7gjswctat6wrliz3fnm7zjezm"
)
PORT = int(os.getenv("PORT", "8080"))  # Render için port

CHAT_FILE = "data/chat_id.json"
GECKOTERMINAL_API_URL = f"https://api.geckoterminal.com/api/v2/networks/omax-chain/pools/{POOL_ADDRESS}/trades"
GECKOTERMINAL_POOL_INFO_API_URL = f"https://api.geckoterminal.com/api/v2/networks/omax-chain/pools/{POOL_ADDRESS}"
LARGE_BUY_THRESHOLD_TOKEN = float(os.getenv("LARGE_BUY_THRESHOLD_TOKEN", "5000.0"))
LARGE_BUY_THRESHOLD_USD = float(os.getenv("LARGE_BUY_THRESHOLD_USD", "50.0"))
SWAP_URL = "https://swap.omax.app/swap"
METAMASK_ADD_NETWORK_URL = "https://chainlist.org/?search=omax"

# Global variables
chat_ids = set()
processed_txs = set()
token_address = None
base_token_price_usd_global = None # Bu değişken artık doğrudan get_pool_info içinde kullanılıyor gibi, globalde tutulması gerekmeyebilir
token_symbols_map = {}


class HealthHandler(BaseHTTPRequestHandler):
    """Simple health check handler"""
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'OK')

    def log_message(self, format, *args):
        pass  # Suppress default logging


def start_health_server():
    """Start health check server"""
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
        logger.info(f"Health check server starting on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Could not start health check server: {e}", exc_info=True)


def load_chat_ids():
    """Load chat IDs from file"""
    global chat_ids
    try:
        if os.path.exists(CHAT_FILE):
            with open(CHAT_FILE, "r") as f:
                data = json.load(f)
                chat_ids = set(data.get("chat_ids", []))
                logger.info(f"Loaded chat IDs: {chat_ids}")
        else:
            os.makedirs(os.path.dirname(CHAT_FILE), exist_ok=True) # Dosya yoksa klasörü oluştur
            logger.info("Chat ID file not found, starting with empty set. Data directory created if it didn't exist.")
    except Exception as e:
        logger.error(f"Error loading chat IDs: {e}")
        chat_ids = set()


def save_chat_ids():
    """Save chat IDs to file"""
    try:
        os.makedirs(os.path.dirname(CHAT_FILE), exist_ok=True)
        with open(CHAT_FILE, "w") as f:
            json.dump({"chat_ids": list(chat_ids)}, f)
        logger.info("Chat IDs saved.")
    except Exception as e:
        logger.error(f"Error saving chat IDs: {e}")


async def start_memexbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start buy notifications for a chat"""
    chat_id = update.message.chat_id
    chat_ids.add(chat_id)
    save_chat_ids()
    await update.message.reply_text(f"{TOKEN_NAME} buy notifications enabled for this chat!")
    logger.info(f"Added chat ID: {chat_id}")


async def stop_memexbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop buy notifications for a chat"""
    chat_id = update.message.chat_id
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        save_chat_ids()
        await update.message.reply_text(f"{TOKEN_NAME} buy notifications disabled for this chat.")
        logger.info(f"Removed chat ID: {chat_id}")
    else:
        await update.message.reply_text("You have not enabled notifications yet.")


async def get_pool_info():
    """Fetch pool information from GeckoTerminal API"""
    global token_address, token_symbols_map # base_token_price_usd_global kaldırıldı, doğrudan return edilecek
    try:
        pool_response = requests.get(GECKOTERMINAL_POOL_INFO_API_URL, timeout=10)
        pool_response.raise_for_status()
        pool_data = pool_response.json()

        attributes = pool_data.get("data", {}).get("attributes", {})
        omemex_price = float(attributes.get("base_token_price_usd", 0)) # Bu OMEMEX (base token) fiyatı
        quote_token_price_usd = float(attributes.get("quote_token_price_usd", 0)) # Bu WOMAX (quote token) fiyatı
        price_change_24h = float(attributes.get("price_change_percentage", {}).get("h24", 0))
        market_cap_usd = float(attributes.get("fdv_usd", 0)) # Genellikle base token FDV'si olur

        relationships = pool_data.get("data", {}).get("relationships", {})
        base_id = relationships.get("base_token", {}).get("data", {}).get("id", "")
        quote_id = relationships.get("quote_token", {}).get("data", {}).get("id", "")

        if base_id and not token_address: # Sadece ilk çalıştığında token_address'i set et
            token_address_from_api = base_id.split("_")[-1]
            if not token_address: # Eğer global token_address henüz set edilmemişse
                 token_address = token_address_from_api
                 logger.info(f"Token address for {TOKEN_NAME} set to: {token_address}")
            elif token_address != token_address_from_api: # Eğer set edilmiş ama API'den farklı geldiyse uyar
                 logger.warning(f"Mismatch in token address! Global: {token_address}, API: {token_address_from_api}")


        # token_symbols_map'i her zaman güncelle, eğer bir değişiklik olursa yansıtsın
        current_token_symbols_map = {
            base_id: TOKEN_NAME,
            quote_id: "WOMAX" # Bu varsayımı kontrol et, quote token her zaman WOMAX mı?
        }
        if token_symbols_map != current_token_symbols_map:
            token_symbols_map = current_token_symbols_map
            logger.info(f"Updated token symbols map: {token_symbols_map}")

        return omemex_price, price_change_24h, market_cap_usd, quote_token_price_usd
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching pool info: {e}")
        return None, None, None, None
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error fetching pool info: {e}")
        return None, None, None, None
    except Exception as e:
        logger.error(f"Unexpected error fetching pool info: {e}", exc_info=True)
        return None, None, None, None


async def fetch_and_process_trades(context: ContextTypes.DEFAULT_TYPE):
    """Fetch and process new trades"""
    global processed_txs, token_address, token_symbols_map

    if not chat_ids:
        # logger.info("No active chat IDs. Skipping trade check.") # Bu log çok sık olabilir, isterseniz DEBUG seviyesine alın.
        return

    # logger.info("Checking trades...") # Bu log da çok sık olabilir.

    try:
        # get_pool_info'dan gelen değerleri al
        omemex_price_usd, price_change_24h, market_cap_usd, womax_price_usd = await get_pool_info()

        if omemex_price_usd is None: # Eğer pool info alınamadıysa, işlemi atla
            logger.warning("Could not retrieve pool info. Skipping trade check for this interval.")
            return

        if not token_address: # Eğer token_address hala set edilmemişse (get_pool_info'da da edilememişse)
            logger.warning("Token address not set, cannot reliably process trades. Skipping.")
            return

        response = requests.get(GECKOTERMINAL_API_URL, timeout=10)
        response.raise_for_status()
        trades_data = response.json()
        trades = trades_data.get("data", [])

        if not trades and not processed_txs:
            logger.info("No trades found and no transactions processed yet. Initializing processed_txs as empty.")
            return # İlk çalıştırmada veya hiç trade yoksa döngüye girme

        # Initialize processed_txs on first run with actual trades
        if not processed_txs and trades:
            processed_txs.update(trade.get("attributes", {}).get("tx_hash") for trade in trades if trade.get("attributes", {}).get("tx_hash"))
            logger.info(f"First run: Initialized processed_txs with {len(processed_txs)} existing trades. No notifications will be sent for these.")
            return

        new_buys = []
        for trade in reversed(trades): # En son tradelerden başla (genelde API'ler böyle sıralar)
            attrs = trade.get("attributes", {})
            tx_hash = attrs.get("tx_hash")
            kind = attrs.get("kind") # 'buy' veya 'sell'
            
            # API'den gelen to_token_address'ı kullanarak bizim TOKEN_NAME'imize yapılan bir alım mı kontrol et
            # GeckoTerminal'de 'buy' kind, base token'ın alındığı anlamına gelir (bu örnekte TOKEN_NAME).
            # 'sell' ise base token'ın satıldığı anlamına gelir.
            # Biz sadece 'buy' (TOKEN_NAME alımı) ile ilgileniyoruz.
            if tx_hash and kind == "buy" and tx_hash not in processed_txs:
                new_buys.append(attrs)
                # processed_txs'e eklemeyi, mesaj başarıyla gönderildikten sonra yapmak daha güvenli olabilir
                # ama çifte gönderimi engellemek için burada eklemek de bir yöntemdir. Şimdilik burada bırakalım.

        if new_buys:
            logger.info(f"Found {len(new_buys)} new buy transaction(s).")
            for buy_attrs in new_buys:
                # Burada omemex_price_usd, price_change_24h, market_cap_usd değerlerini pass ediyoruz.
                await process_buy_notification(buy_attrs, omemex_price_usd, price_change_24h, market_cap_usd, context)
                processed_txs.add(buy_attrs.get("tx_hash")) # Bildirim gönderildikten sonra ekle
            logger.info(f"Processed new buys. Total processed TXs: {len(processed_txs)}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error fetching trades: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error fetching trades: {e}")
    except Exception as e:
        logger.error(f"Error fetching/processing trades: {e}", exc_info=True)


async def process_buy_notification(buy_attrs, omemex_price_usd, price_change_24h, market_cap_usd, context):
    """Process and send buy notification"""
    try:
        # buy_attrs, trade'in 'attributes' kısmıdır.
        from_token_symbol = token_symbols_map.get(buy_attrs.get("from_token", {}).get("id"), "UNKNOWN_QUOTE") # Sembolü map'ten al
        to_token_symbol = token_symbols_map.get(buy_attrs.get("to_token", {}).get("id"), TOKEN_NAME) # Sembolü map'ten al

        # GeckoTerminal'de 'buy' işlemi, base token (bizim TOKEN_NAME'imiz) alımıdır.
        # Yani 'to_token_amount' bizim TOKEN_NAME miktarımız, 'from_token_amount' ise ödenen (örn: WOMAX) miktarıdır.
        to_amount_str = buy_attrs.get("to_token_amount") # Bu bizim TOKEN_NAME miktarımız
        from_amount_str = buy_attrs.get("from_token_amount") # Bu ödenen quote token miktarı (örn: WOMAX)

        if to_amount_str is None or from_amount_str is None:
            logger.error(f"Missing amount data in trade attributes: {buy_attrs}")
            return

        to_amount = float(to_amount_str)
        from_amount = float(from_amount_str)

        # USD değeri, alınan TOKEN_NAME miktarı * TOKEN_NAME'in anlık USD fiyatı
        usd_value = to_amount * omemex_price_usd if omemex_price_usd is not None else 0

        leading = "🟢"
        if to_amount >= LARGE_BUY_THRESHOLD_TOKEN or usd_value >= LARGE_BUY_THRESHOLD_USD:
            leading = "🟢🟢🟢"

        change_text = ""
        if price_change_24h is not None:
            arrow = "▲" if price_change_24h >= 0 else "▼"
            emoji = "🟢" if price_change_24h >= 0 else "🔴"
            change_text = f"📈 **24h Change:** {emoji} `{arrow} {abs(price_change_24h):.2f}%`\n"
        else:
            change_text = "📈 **24h Change:** `N/A`\n"


        tx_url = f"https://omaxray.com/tx/{buy_attrs.get('tx_hash')}"

        message = (
            f"{leading} **New {to_token_symbol} Buy!** {leading}\n\n"
            f"🚀 **Amount Received:** `{to_amount:,.8f}` {to_token_symbol}\n" # Alınan token TOKEN_NAME
            f"💰 **Amount Paid:** `{from_amount:,.8f}` {from_token_symbol}\n" # Ödenen token WOMAX (veya neyse)
            f"💲 **Current Value:** `${usd_value:,.2f} USD`\n" # USD değerini 2 ondalıkla göstermek daha yaygın
            f"💵 **Unit Price ({to_token_symbol}):** `${omemex_price_usd:,.10f}`\n"
            f"{change_text}"
            f"📊 **Market Cap ({to_token_symbol}):** `${market_cap_usd:,.2f} USD`\n"
            f"🔐 {TOKEN_NAME} is strictly limited to `300,000,000,000` tokens only.\n"
        )

        buttons = [
            [InlineKeyboardButton("View Transaction", url=tx_url)],
            [InlineKeyboardButton(f"Swap {TOKEN_NAME}", url=SWAP_URL)],
            [InlineKeyboardButton("Add Omax Mainnet to MetaMask", url=METAMASK_ADD_NETWORK_URL)],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        for cid in list(chat_ids):  # Iterate over a copy for safe removal
            try:
                await context.bot.send_video(
                    chat_id=cid,
                    video=IMAGE_URL,
                    caption=message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup,
                )
                await asyncio.sleep(0.1) # Telegram API limitlerine takılmamak için küçük bir bekleme
            except Exception as e:
                logger.error(f"Error sending message to chat ID {cid}: {e}")
                if "chat not found" in str(e).lower() or \
                   "bot was blocked by the user" in str(e).lower() or \
                   "user is deactivated" in str(e).lower() or \
                   "group chat was deactivated" in str(e).lower() or \
                   "bot was kicked from the supergroup chat" in str(e).lower():
                    logger.info(f"Removing invalid chat ID: {cid}")
                    chat_ids.discard(cid)
                    save_chat_ids()

    except Exception as e:
        logger.error(f"Error processing buy notification for TX {buy_attrs.get('tx_hash')}: {e}", exc_info=True)


async def health_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE): # Komut adı health_check -> health_check_command
    """Health check command for Telegram"""
    await update.message.reply_text("Bot is running! ✅ (Telegram Handler Active)")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Status command"""
    active_chats = len(chat_ids)
    processed_count = len(processed_txs)
    omemex_price, price_change_24h, market_cap_usd, _ = await get_pool_info() # Anlık durumu da alalım

    status_message = (
        f"📊 **Bot Status**\n\n"
        f"🔔 Active Chats: `{active_chats}`\n"
        f"📝 Processed Transactions (since last restart/initialization): `{processed_count}`\n"
        f"⚡ Interval: `{INTERVAL} seconds`\n"
        f"💎 Token: `{TOKEN_NAME}`\n"
    )
    if omemex_price is not None:
        status_message += f"💵 Current Price: `${omemex_price:,.10f}`\n"
        status_message += f"📈 24h Change: `{price_change_24h:.2f}%`\n"
        status_message += f"💰 Market Cap: `${market_cap_usd:,.2f}`\n"
    else:
        status_message += "⚠️ Could not fetch current token price/market cap.\n"

    await update.message.reply_text(status_message, parse_mode='Markdown')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by Updates."""
    logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)


async def main():
    """Main function"""
    health_thread = None
    app = None
    try:
        health_thread = threading.Thread(target=start_health_server, daemon=True)
        health_thread.start()
        # health_thread'in başladığından emin olmak için kısa bir bekleme, isteğe bağlı
        # await asyncio.sleep(1)

        load_chat_ids()
        # İlk pool bilgisini alıp token_address ve token_symbols_map'i doldurmaya çalış
        logger.info("Fetching initial pool info to set up token details...")
        await get_pool_info()
        if not token_address:
            logger.warning("Could not determine token address on initial setup. Retrying in background.")
        if not token_symbols_map:
            logger.warning("Could not determine token symbols map on initial setup. Retrying in background.")


        app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True) # Birden fazla güncellemeyi eş zamanlı işleyebilir
            # .connection_pool_size(10) # İsteğe bağlı: Daha fazla eş zamanlı istek için
            # .http_version('1.1') # İsteğe bağlı: Eski HTTP versiyonu, bazen ağ sorunlarına iyi gelebilir
            # .get_updates_http_version('1.1') # İsteğe bağlı
            .build()
        )

        app.add_handler(CommandHandler("omemexbuystart", start_memexbuy))
        app.add_handler(CommandHandler("omemexbuystop", stop_memexbuy))
        app.add_handler(CommandHandler("health", health_check_command)) # Komut adını değiştirdik
        app.add_handler(CommandHandler("status", status))
        app.add_error_handler(error_handler)

        await app.initialize() # JobQueue ve diğer bileşenleri başlatır
        logger.info("Telegram Application initialized.")

        # job_callback'in async olduğundan emin ol
        async def job_callback(context_job: ContextTypes.DEFAULT_TYPE): # context adı context_job olarak değiştirildi
            await fetch_and_process_trades(context_job) # Doğru context'i pass et

        if app.job_queue:
            app.job_queue.run_repeating(job_callback, interval=INTERVAL, first=10) # first=10: Bot başladıktan 10sn sonra ilk iş
            logger.info(f"Job queue started. Trades will be checked every {INTERVAL} seconds.")
        else:
            logger.error("JobQueue is not available after app.initialize(). This is unexpected.")
            # Bu durumda botun ana işlevi çalışmayacaktır, bir şeyler ciddi şekilde yanlış.

        await app.start() # Arka plan görevlerini (job queue gibi) başlatır
        logger.info("Telegram Application background tasks started.")

        logger.info("Starting Telegram bot polling...")
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        logger.info("Telegram bot polling is now active.")

        # Uygulamanın kapanana kadar çalışmasını sağla
        # Render gibi bir ortamda, platform servisi durdurmak için sinyal gönderecektir.
        # Bu sinyal asyncio.run tarafından yakalanıp CancelledError oluşturacaktır.
        while True:
            await asyncio.sleep(3600) # Periyodik olarak uyan, ama esasen CancelledError'ı bekle
            logger.debug("Main loop heartbeat.") # İsteğe bağlı: Ana döngünün çalıştığını görmek için

    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError) as e:
        logger.info(f"Shutdown signal ({type(e).__name__}) received. Cleaning up...")
    except Exception as e:
        logger.error(f"Critical error in main function: {e}", exc_info=True)
    finally:
        logger.info("Initiating graceful shutdown of the Telegram bot...")
        if app:
            if app.updater and app.updater.is_running: # is_polling yerine is_running daha genel
                logger.info("Stopping updater polling...")
                await app.updater.stop()
            if app.running:
                logger.info("Stopping application (jobs, etc.)...")
                await app.stop()
            logger.info("Performing final application shutdown...")
            await app.shutdown()
        else:
            logger.info("Application object was not created. No Telegram shutdown needed.")

        if health_thread and health_thread.is_alive():
            logger.info("Health check server is managed by daemon thread, will exit with main.")
            # HTTP server'ı doğrudan kapatmak için bir yöntem yok, daemon olduğu için ana thread bitince o da biter.

        logger.info("Graceful shutdown sequence completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e: # Özellikle "Cannot close a running event loop" gibi hataları yakalamak için
        if "event loop is already running" in str(e) or "Cannot close a running event loop" in str(e):
            logger.critical(f"Asyncio event loop conflict detected at the very end: {e}. This might indicate an issue with how asyncio.run() interacts with the environment or libraries.")
        else:
            logger.critical(f"Unhandled RuntimeError at script exit: {e}", exc_info=True)
    except Exception as e:
        logger.critical(f"Unhandled exception at script exit: {e}", exc_info=True)
