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
base_token_price_usd_global = None
token_symbols_map = {}


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
            logger.info("Chat ID file not found, starting with empty set")
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
    global token_address, base_token_price_usd_global, token_symbols_map
    try:
        pool_response = requests.get(GECKOTERMINAL_POOL_INFO_API_URL, timeout=10)
        pool_response.raise_for_status()
        pool_data = pool_response.json()
        
        attributes = pool_data.get("data", {}).get("attributes", {})
        omemex_price = float(attributes.get("base_token_price_usd", 0))
        womax_price = float(attributes.get("quote_token_price_usd", 0))
        base_token_price_usd_global = womax_price
        price_change_24h = float(attributes.get("price_change_percentage", {}).get("h24", 0))
        market_cap_usd = float(attributes.get("fdv_usd", 0))

        relationships = pool_data.get("data", {}).get("relationships", {})
        base_id = relationships.get("base_token", {}).get("data", {}).get("id", "")
        quote_id = relationships.get("quote_token", {}).get("data", {}).get("id", "")
        
        if base_id and not token_address:
            token_address = base_id.split("_")[-1]
            
        token_symbols_map = {
            base_id: TOKEN_NAME,
            quote_id: "WOMAX"
        }
        
        return omemex_price, price_change_24h, market_cap_usd, womax_price
    except Exception as e:
        logger.error(f"Error fetching pool info: {e}")
        return None, None, None, None


async def fetch_and_process_trades(context: ContextTypes.DEFAULT_TYPE):
    """Fetch and process new trades"""
    global processed_txs, token_address, token_symbols_map

    if not chat_ids:
        logger.info("No active chat IDs. Skipping trade check.")
        return

    logger.info("Checking trades...")
    try:
        omemex_price_usd, price_change_24h, market_cap_usd, womax_price_usd = await get_pool_info()
        if not token_address:
            logger.warning("Token address not set, skipping.")
            return

        response = requests.get(GECKOTERMINAL_API_URL, timeout=10)
        response.raise_for_status()
        trades = response.json().get("data", [])

        # Initialize processed_txs on first run
        if not processed_txs and trades:
            processed_txs.update(trade.get("attributes", {}).get("tx_hash") for trade in trades if trade.get("attributes", {}).get("tx_hash"))
            logger.info("First run: existing trades skipped.")
            return

        # Find new buy trades
        new_buys = []
        for trade in reversed(trades):
            attrs = trade.get("attributes", {})
            tx_hash = attrs.get("tx_hash")
            kind = attrs.get("kind")
            if tx_hash and kind == "buy" and tx_hash not in processed_txs:
                new_buys.append(attrs)
                processed_txs.add(tx_hash)

        # Process new buys
        for buy in new_buys:
            await process_buy_notification(buy, omemex_price_usd, price_change_24h, market_cap_usd, context)

    except Exception as e:
        logger.error(f"Error fetching trades: {e}")


async def process_buy_notification(buy, omemex_price_usd, price_change_24h, market_cap_usd, context):
    """Process and send buy notification"""
    try:
        from_symbol = buy.get("from_token_symbol", "WOMAX")
        to_amount = float(buy.get("to_token_amount", 0))
        from_amount = float(buy.get("from_token_amount", 0))
        usd_value = to_amount * omemex_price_usd if omemex_price_usd else 0

        # Determine emoji based on trade size
        leading = "🟢"
        if to_amount >= LARGE_BUY_THRESHOLD_TOKEN or usd_value >= LARGE_BUY_THRESHOLD_USD:
            leading = "🟢🟢🟢"

        # Price change display
        change_text = ""
        if price_change_24h is not None:
            arrow = "▲" if price_change_24h >= 0 else "▼"
            emoji = "🟢" if price_change_24h >= 0 else "🔴"
            change_text = f"📈 **24h Change:** {emoji} `{arrow} {abs(price_change_24h):,.2f}%`\n"

        tx_url = f"https://omaxray.com/tx/{buy.get('tx_hash')}"

        message = (
            f"{leading} **New {TOKEN_NAME} Buy!** {leading}\n\n"
            f"🚀 **Amount Received:** `{to_amount:.8f}` {TOKEN_NAME}\n"
            f"💰 **Amount Paid:** `{from_amount:.8f}` {from_symbol}\n"
            f"💲 **Current Value:** `${usd_value:.8f} USD`\n"
            f"💵 **Unit Price ({TOKEN_NAME}):** `${omemex_price_usd:.10f}`\n"
            f"{change_text}"
            f"📊 **Market Cap ({TOKEN_NAME}):** `${market_cap_usd:,.2f} USD`\n"
            f"🔐 OMEMEX is strictly limited to `300,000,000,000` tokens only.\n"
        )

        buttons = [
            [InlineKeyboardButton("View Transaction", url=tx_url)],
            [InlineKeyboardButton("Swap", url=SWAP_URL)],
            [InlineKeyboardButton("Add Omax Mainnet to MetaMask", url=METAMASK_ADD_NETWORK_URL)],
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        # Send to all registered chats
        for cid in chat_ids.copy():  # Use copy to avoid modification during iteration
            try:
                await context.bot.send_video(
                    chat_id=cid,
                    video=IMAGE_URL,
                    caption=message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup,
                )
                await asyncio.sleep(1)  # Rate limiting
            except Exception as e:
                logger.error(f"Error sending message to {cid}: {e}")
                # Remove invalid chat IDs
                if "chat not found" in str(e).lower() or "blocked" in str(e).lower():
                    chat_ids.discard(cid)
                    save_chat_ids()
                    logger.info(f"Removed invalid chat ID: {cid}")

    except Exception as e:
        logger.error(f"Error processing buy notification: {e}")


async def health_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check command"""
    await update.message.reply_text("Bot is running! ✅")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Status command"""
    active_chats = len(chat_ids)
    processed_count = len(processed_txs)
    message = (
        f"📊 **Bot Status**\n\n"
        f"🔔 Active Chats: `{active_chats}`\n"
        f"📝 Processed Transactions: `{processed_count}`\n"
        f"⚡ Interval: `{INTERVAL} seconds`\n"
        f"💎 Token: `{TOKEN_NAME}`"
    )
    await update.message.reply_text(message, parse_mode='Markdown')


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by Updates."""
    logger.error(f"Exception while handling an update: {context.error}")


async def main():
    """Main function"""
    try:
        load_chat_ids()
        
        # Build application with explicit configuration
        app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        # Add handlers
        app.add_handler(CommandHandler("omemexbuystart", start_memexbuy))
        app.add_handler(CommandHandler("omemexbuystop", stop_memexbuy))
        app.add_handler(CommandHandler("health", health_check))
        app.add_handler(CommandHandler("status", status))
        
        # Add error handler
        app.add_error_handler(error_handler)

        # Add job
        async def job_callback(context: ContextTypes.DEFAULT_TYPE):
            await fetch_and_process_trades(context)

        if app.job_queue:
            app.job_queue.run_repeating(job_callback, interval=INTERVAL, first=10)

        logger.info(f"Bot is starting on port {PORT}...")
        
        # Always use polling for simplicity on Render
        logger.info("Starting bot with polling...")
        await app.run_polling(
            drop_pending_updates=True,
            close_queue=True,
            stop_signals=None
        )
            
    except Exception as e:
        logger.error(f"Error in main: {e}")
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    asyncio.run(main())
