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
# USER CONFIGS (Prefix, Suffix, Replace)
# =========================================================

USER_CONFIGS = {}

def default_config():
    return {
        "prefix": "",
        "suffix": "",
        "replace_from": "",
        "replace_to": "",
    }

def get_config(user_id):
    if user_id not in USER_CONFIGS:
        USER_CONFIGS[user_id] = default_config()
    return USER_CONFIGS[user_id]


# =========================================================
# DATABASE (Link Mapping)
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

def save_mapping(source_chat, source_topic, source_msg_id, target_chat, target_topic, target_msg_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT OR REPLACE INTO mappings
        (source_chat, source_topic, source_msg_id, target_chat, target_topic, target_msg_id)
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
    
    if value.lstrip("-").isdigit():
        return int(value)
    
    if not value.startswith("@") and not value.lstrip("-").isdigit():
        return "@" + value
        
    return value


# =========================================================
# CLONE / TOPIC TRANSFER COMMAND (WITH ALL FEATURES)
# =========================================================

async def clone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
    /clone Source Target FromID ToID [SrcTopicID] [TgtTopicID]
    Example: /clone @source_channel -10022222222 1 50 0 12
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

            # Using standard Telegram copy_message
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
        except Exception as err:
            # Print error to terminal for debugging
            print(f"Failed msg {msg_id}: {err}", flush=True)
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
# CONFIG COMMANDS (Prefix, Suffix, Replace, Status, Reset)
# =========================================================

async def set_prefix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage:\n/setprefix Your text here")
        return
    value = update.effective_message.text.split(maxsplit=1)[1]
    get_config(update.effective_user.id)["prefix"] = value
    await update.effective_message.reply_text("✅ Prefix set successfully.")


async def set_suffix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text("Usage:\n/setsuffix Your text here")
        return
    value = update.effective_message.text.split(maxsplit=1)[1]
    get_config(update.effective_user.id)["suffix"] = value
    await update.effective_message.reply_text("✅ Suffix set successfully.")


async def set_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    if not context.args or " | " not in text:
        await update.effective_message.reply_text("Usage:\n/setreplace old_word | new_word")
        return
    value = text.split(maxsplit=1)[1]
    old, new = value.split(" | ", 1)
    config = get_config(update.effective_user.id)
    config["replace_from"] = old.strip()
    config["replace_to"] = new.strip()
    await update.effective_message.reply_text("✅ Replacement rules saved.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = get_config(update.effective_user.id)
    await update.effective_message.reply_text(
        "📊 **Current Settings:**\n\n"
        f"• Prefix: `{config['prefix'] or 'None'}`\n"
        f"• Suffix: `{config['suffix'] or 'None'}`\n"
        f"• Replace: `{config['replace_from'] or 'None'} → {config['replace_to'] or 'None'}`"
    )


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    USER_CONFIGS[update.effective_user.id] = default_config()
    await update.effective_message.reply_text("🔄 Configurations reset to default.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🚀 **Advanced Bot-Only Transfer Manager**\n\n"
        "Commands:\n"
        "• `/clone Source Target FromID ToID [SrcTopic] [TgtTopic]`\n"
        "• `/setprefix text`\n"
        "• `/setsuffix text`\n"
        "• `/setreplace old | new`\n"
        "• `/status`\n"
        "• `/reset`"
    )


# =========================================================
# WEB SERVER & SETUP
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
        BotCommand("status", "Show settings"),
        BotCommand("reset", "Reset settings"),
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
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setsuffix", set_suffix))
    application.add_handler(CommandHandler("setreplace", set_replace))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("reset", reset_command))
    
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
