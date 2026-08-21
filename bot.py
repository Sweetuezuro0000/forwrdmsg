import os
import sqlite3
import asyncio
from aiohttp import web

from telegram import Update, BotCommand
from telegram.error import TelegramError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# =========================================================
# ENVIRONMENT
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "link_mapping.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mappings (
            source_chat TEXT NOT NULL,
            source_topic INTEGER NOT NULL DEFAULT 0,
            source_msg_id INTEGER NOT NULL,

            target_chat TEXT NOT NULL,
            target_topic INTEGER NOT NULL DEFAULT 0,
            target_msg_id INTEGER NOT NULL,

            PRIMARY KEY (
                source_chat,
                source_topic,
                source_msg_id
            )
        )
    """)
    conn.commit()
    conn.close()


def save_mapping(
    source_chat,
    source_topic,
    source_msg_id,
    target_chat,
    target_topic,
    target_msg_id
):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT OR REPLACE INTO mappings
        (
            source_chat,
            source_topic,
            source_msg_id,
            target_chat,
            target_topic,
            target_msg_id
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(source_chat),
        int(source_topic or 0),
        int(source_msg_id),
        str(target_chat),
        int(target_topic or 0),
        int(target_msg_id),
    ))
    conn.commit()
    conn.close()


# =========================================================
# CHAT NORMALIZATION
# =========================================================

def normalize_chat(value):
    value = str(value).strip()
    if value.startswith("https://t.me/"):
        value = value.rstrip("/")
        parts = value.split("/")
        if len(parts) >= 4:
            value = parts[3]
    
    # Agar username hai toh '@' laga rehene dein ya hata dein, Telegram API username (@channel) accept kar leta hai public ke liye
    if value.lstrip("-").isdigit():
        return int(value)
    
    if not value.startswith("@") and not value.lstrip("-").isdigit():
        return "@" + value
        
    return value


# =========================================================
# CLONE / TOPIC TRANSFER COMMAND
# =========================================================

async def clone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
    /clone Source Target From_ID To_ID [Src_Topic_ID] [Tgt_Topic_ID]
    Example: /clone @public_channel -10022222222 1 50 0 12
    """
    if not update.effective_message:
        return

    args = context.args
    if len(args) < 4:
        await update.effective_message.reply_text(
            "⚠️ **Sahi tarika use karein:**\n\n"
            "`/clone Source Target FromID ToID [SrcTopicID] [TgtTopicID]`\n\n"
            "Example:\n"
            "`/clone @public_channel -10098765 1 50 0 12`"
        )
        return

    source = normalize_chat(args[0])
    target = normalize_chat(args[1])

    try:
        from_id = int(args[2])
        to_id = int(args[3])
        
        src_topic = int(args[4]) if len(args) > 4 else 0
        tgt_topic = int(args[5]) if len(args) > 5 else 0
        
    except ValueError:
        await update.effective_message.reply_text("❌ IDs numbers honi chahiye.")
        return

    status = await update.effective_message.reply_text("🚀 Transfer shuru ho gaya hai...")

    success = 0
    failed = 0
    total = to_id - from_id + 1

    for msg_id in range(from_id, to_id + 1):
        try:
            kwargs = {}
            if tgt_topic:
                kwargs["message_thread_id"] = tgt_topic

            result = await context.bot.copy_message(
                chat_id=target,
                from_chat_id=source,
                message_id=msg_id,
                **kwargs
            )

            if result:
                save_mapping(source, src_topic, msg_id, target, tgt_topic, result.message_id)
                success += 1
            else:
                failed += 1

        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                kwargs = {}
                if tgt_topic:
                    kwargs["message_thread_id"] = tgt_topic
                    
                result = await context.bot.copy_message(
                    chat_id=target,
                    from_chat_id=source,
                    message_id=msg_id,
                    **kwargs
                )
                if result:
                    save_mapping(source, src_topic, msg_id, target, tgt_topic, result.message_id)
                    success += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.4)

    try:
        await status.edit_text(
            "🏁 **Transfer Poora Ho Gaya!**\n\n"
            f"✅ Success: `{success}`\n"
            f"❌ Failed: `{failed}`\n"
            f"📦 Total: `{total}`"
        )
    except TelegramError:
        pass


# =========================================================
# BASIC COMMANDS
# =========================================================

async def start_command(update, context):
    await update.effective_message.reply_text(
        "🚀 **Public Channel to Topic Bot**\n\n"
        "Command format:\n"
        "`/clone @source_channel TargetID FromID ToID [SrcTopicID] [TgtTopicID]`"
    )


# =========================================================
# WEB SERVER
# =========================================================

async def home(request):
    return web.Response(text="Bot is running.", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", home)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP server running on port {PORT}", flush=True)


async def setup_commands(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start bot"),
        BotCommand("clone", "Transfer messages"),
    ])


async def error_handler(update, context):
    print(f"BOT ERROR: {context.error}", flush=True)


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("clone", clone_command))
    application.add_error_handler(error_handler)

    async def run_services():
        await start_web_server()
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        print("Bot polling started successfully.", flush=True)
        
        stop_signal = asyncio.Event()
        await stop_signal.wait()

    try:
        asyncio.run(run_services())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
