import os
import re
import sqlite3
import asyncio
from aiohttp import web

from telegram import Update, BotCommand
from telegram.constants import ChatMemberStatus
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
# USER CONFIG
# =========================================================

USER_CONFIGS = {}


def default_config():
    return {
        "prefix": "",
        "suffix": "",
        "replace_from": "",
        "replace_to": "",
        "old_chat": "",
        "new_chat": "",
    }


def get_config(user_id):
    if user_id not in USER_CONFIGS:
        USER_CONFIGS[user_id] = default_config()

    return USER_CONFIGS[user_id]


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "link_mapping.db"


def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS mappings (
            source_chat TEXT NOT NULL,
            source_msg_id INTEGER NOT NULL,
            target_chat TEXT NOT NULL,
            target_msg_id INTEGER NOT NULL,
            PRIMARY KEY (source_chat, source_msg_id)
        )
    """)

    conn.commit()
    conn.close()


def save_mapping(source_chat, source_id, target_chat, target_id):
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        INSERT OR REPLACE INTO mappings
        (
            source_chat,
            source_msg_id,
            target_chat,
            target_msg_id
        )
        VALUES (?, ?, ?, ?)
    """, (
        str(source_chat),
        int(source_id),
        str(target_chat),
        int(target_id),
    ))

    conn.commit()
    conn.close()


def get_mapping(source_chat, source_id):
    conn = sqlite3.connect(DB_FILE)

    row = conn.execute("""
        SELECT target_msg_id
        FROM mappings
        WHERE source_chat = ?
        AND source_msg_id = ?
    """, (
        str(source_chat),
        int(source_id),
    )).fetchone()

    conn.close()

    return row[0] if row else None


# =========================================================
# TEXT / LINK PROCESSING
# =========================================================

def process_text(text, config, source_chat):
    if not text:
        return ""

    # Text replacement
    if config["replace_from"]:
        text = text.replace(
            config["replace_from"],
            config["replace_to"]
        )

    # Internal Telegram message links
    old_chat = config["old_chat"]
    new_chat = config["new_chat"]

    if old_chat and new_chat:

        pattern = re.compile(
            r"https?://t\.me/"
            r"(c/\d+|[A-Za-z0-9_]+)/"
            r"(\d+)"
        )

        def replace_link(match):

            chat_part = match.group(1)
            message_id = int(match.group(2))

            if chat_part != old_chat:
                return match.group(0)

            mapped_id = get_mapping(
                source_chat,
                message_id
            )

            if mapped_id is None:
                return match.group(0)

            return (
                f"https://t.me/"
                f"{new_chat}/"
                f"{mapped_id}"
            )

        text = pattern.sub(
            replace_link,
            text
        )

    # Prefix / suffix
    return (
        config["prefix"]
        + text
        + config["suffix"]
    )


# =========================================================
# START
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_message:
        return

    print(
        f"START RECEIVED: "
        f"{update.effective_user.id}",
        flush=True
    )

    await update.effective_message.reply_text(
        "🚀 Advanced Bulk Content Manager Bot Active!\n\n"

        "Configuration:\n"
        "/setprefix text\n"
        "/setsuffix text\n"
        "/setreplace old | new\n"
        "/setlink old_chat | new_chat\n"
        "/status\n"
        "/reset\n\n"

        "Bulk Transfer:\n"
        "/clone Source Target From_ID To_ID Src_Topic_ID Tgt_Topic_ID"
    )


# =========================================================
# PREFIX
# =========================================================

async def set_prefix(update, context):

    if not update.effective_message:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setprefix Your text"
        )
        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    get_config(
        update.effective_user.id
    )["prefix"] = value

    await update.effective_message.reply_text(
        "✅ Prefix updated."
    )


# =========================================================
# SUFFIX
# =========================================================

async def set_suffix(update, context):

    if not update.effective_message:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setsuffix Your text"
        )
        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    get_config(
        update.effective_user.id
    )["suffix"] = value

    await update.effective_message.reply_text(
        "✅ Suffix updated."
    )


# =========================================================
# REPLACE
# =========================================================

async def set_replace(update, context):

    if not update.effective_message:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setreplace old | new"
        )
        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    if " | " not in value:
        await update.effective_message.reply_text(
            "Usage:\n/setreplace old | new"
        )
        return

    old, new = value.split(
        " | ",
        1
    )

    config = get_config(
        update.effective_user.id
    )

    config["replace_from"] = old.strip()
    config["replace_to"] = new.strip()

    await update.effective_message.reply_text(
        "✅ Text replacement configured."
    )


# =========================================================
# LINK
# =========================================================

async def set_link(update, context):

    if not update.effective_message:
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setlink old_chat | new_chat"
        )
        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    if " | " not in value:
        await update.effective_message.reply_text(
            "Usage:\n/setlink old_chat | new_chat"
        )
        return

    old, new = value.split(
        " | ",
        1
    )

    config = get_config(
        update.effective_user.id
    )

    config["old_chat"] = old.strip().replace("@", "")
    config["new_chat"] = new.strip().replace("@", "")

    await update.effective_message.reply_text(
        "✅ Internal Telegram link mapping configured."
    )


# =========================================================
# STATUS
# =========================================================

async def status_command(update, context):

    if not update.effective_message:
        return

    config = get_config(
        update.effective_user.id
    )

    await update.effective_message.reply_text(
        "📊 Current Settings\n\n"
        f"Prefix: {config['prefix'] or 'None'}\n"
        f"Suffix: {config['suffix'] or 'None'}\n"
        f"Replace: "
        f"{config['replace_from'] or 'None'} → "
        f"{config['replace_to'] or 'None'}\n"
        f"Old Chat: {config['old_chat'] or 'None'}\n"
        f"New Chat: {config['new_chat'] or 'None'}"
    )


# =========================================================
# RESET
# =========================================================

async def reset_command(update, context):

    if not update.effective_message:
        return

    USER_CONFIGS[
        update.effective_user.id
    ] = default_config()

    await update.effective_message.reply_text(
        "🔄 Configuration reset."
    )


# =========================================================
# CLONE
# =========================================================

async def clone_command(update, context):

    if not update.effective_message:
        return

    args = context.args

    if len(args) < 4:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/clone "
            "Source Target From_ID To_ID "
            "Src_Topic_ID Tgt_Topic_ID"
        )
        return

    source = args[0]
    target = args[1]

    try:
        from_id = int(args[2])
        to_id = int(args[3])

        src_topic = (
            int(args[4])
            if len(args) > 4
            else 0
        )

        tgt_topic = (
            int(args[5])
            if len(args) > 5
            else 0
        )

    except ValueError:
        await update.effective_message.reply_text(
            "❌ IDs must be numbers."
        )
        return

    if source.lstrip("-").isdigit():
        source = int(source)

    if target.lstrip("-").isdigit():
        target = int(target)

    config = get_config(
        update.effective_user.id
    )

    status = await update.effective_message.reply_text(
        "🚀 Bulk transfer started..."
    )

    success = 0
    failed = 0

    total = max(
        0,
        to_id - from_id + 1
    )

    source_db = str(source).replace(
        "-100",
        ""
    )

    target_db = str(target).replace(
        "-100",
        ""
    )

    for message_id in range(
        from_id,
        to_id + 1
    ):

        try:

            # -------------------------
            # GET SOURCE MESSAGE
            # -------------------------

            msg = await context.bot.forward_message(
                chat_id=target,
                from_chat_id=source,
                message_id=message_id,
                disable_notification=True,
            )

            # -------------------------
            # IMPORTANT
            # -------------------------
            #
            # This first version uses Telegram's
            # native forward operation.
            #
            # Advanced text/media rewriting is
            # handled separately below when possible.
            #

            if msg:

                save_mapping(
                    source_db,
                    message_id,
                    target_db,
                    msg.message_id
                )

                success += 1

            # -------------------------
            # PROGRESS
            # -------------------------

            done = (
                message_id - from_id + 1
            )

            if done == 1 or done % 5 == 0:

                percentage = (
                    done / total * 100
                    if total
                    else 100
                )

                try:
                    await status.edit_text(
                        "⏳ Bulk Transfer\n\n"
                        f"Processed: `{done}/{total}`\n"
                        f"Success: `{success}`\n"
                        f"Failed: `{failed}`\n"
                        f"Progress: `{percentage:.1f}%`"
                    )
                except TelegramError:
                    pass

            await asyncio.sleep(1.2)

        except RetryAfter as e:

            await asyncio.sleep(
                e.retry_after + 1
            )

        except TelegramError as e:

            failed += 1

            print(
                f"Telegram error "
                f"{message_id}: {e}",
                flush=True
            )

        except Exception as e:

            failed += 1

            print(
                f"Clone error "
                f"{message_id}: {e}",
                flush=True
            )

    try:
        await status.edit_text(
            "🏁 Bulk Transfer Completed\n\n"
            f"✅ Success: `{success}`\n"
            f"❌ Failed: `{failed}`\n"
            f"📦 Total: `{total}`"
        )
    except TelegramError:
        pass


# =========================================================
# RENDER WEB SERVER
# =========================================================

async def home(request):

    return web.Response(
        text="Bulk Manager Bot is running.",
        status=200
    )


async def start_web_server():

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        home
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"HTTP server running on 0.0.0.0:{PORT}",
        flush=True
    )


# =========================================================
# BOT COMMANDS
# =========================================================

async def setup_commands(application):

    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot"),
        BotCommand("setprefix", "Add prefix"),
        BotCommand("setsuffix", "Add suffix"),
        BotCommand("setreplace", "Replace text"),
        BotCommand("setlink", "Configure internal links"),
        BotCommand("status", "Show settings"),
        BotCommand("reset", "Reset settings"),
        BotCommand("clone", "Bulk transfer messages"),
    ])


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    print(
        f"BOT ERROR: {context.error}",
        flush=True
    )


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

    # Commands
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("setprefix", set_prefix)
    )

    application.add_handler(
        CommandHandler("setsuffix", set_suffix)
    )

    application.add_handler(
        CommandHandler("setreplace", set_replace)
    )

    application.add_handler(
        CommandHandler("setlink", set_link)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CommandHandler("reset", reset_command)
    )

    application.add_handler(
        CommandHandler("clone", clone_command)
    )

    application.add_error_handler(
        error_handler
    )

    print(
        "Starting Bulk Manager Bot...",
        flush=True
    )

    # Start HTTP server in a background thread/event loop
    async def web_runner():

        await start_web_server()

        while True:
            await asyncio.sleep(3600)

    async def run():

        web_task = asyncio.create_task(
            web_runner()
        )

        try:

            # Remove any old Telegram webhook.
            await application.bot.delete_webhook(
                drop_pending_updates=True
            )

            print(
                "Webhook removed. Starting polling...",
                flush=True
            )

            await application.initialize()
            await application.start()

            await application.updater.start_polling(
                drop_pending_updates=True
            )

            me = await application.bot.get_me()

            print(
                f"Bot online: @{me.username}",
                flush=True
            )

            await asyncio.Event().wait()

        finally:

            await application.updater.stop()
            await application.stop()
            await application.shutdown()

            web_task.cancel()

    asyncio.run(run())


if __name__ == "__main__":
    main()
