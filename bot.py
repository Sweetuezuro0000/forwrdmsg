import os
import time
import sqlite3
import asyncio

from aiohttp import web
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from telegram.ext import Application, CommandHandler


# =========================================================
# ENVIRONMENT
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
PORT = int(os.environ.get("PORT", "10000"))

START_TIME = time.time()


# =========================================================
# CONFIG
# =========================================================

def default_config():
    return {
        "prefix": "",
        "suffix": "",
        "replace_from": "",
        "replace_to": "",
        "caption_mode": "keep",
        "caption_replace_from": "",
        "caption_replace_to": "",
    }


CONFIG = default_config()


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
    target_msg_id,
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


def get_mapping(
    source_chat,
    source_topic,
    source_msg_id,
):
    conn = sqlite3.connect(DB_FILE)

    row = conn.execute("""
        SELECT
            target_chat,
            target_topic,
            target_msg_id
        FROM mappings
        WHERE source_chat = ?
          AND source_topic = ?
          AND source_msg_id = ?
    """, (
        str(source_chat),
        int(source_topic or 0),
        int(source_msg_id),
    )).fetchone()

    conn.close()

    return row


# =========================================================
# PYROGRAM USER SESSION
# =========================================================

app = Client(
    "userbot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,

    # IMPORTANT:
    # User session ko Telegram updates receive nahi karne hain.
    # Isse resolve_peer wali repeated update errors avoid hoti hain.
    no_updates=True,
)


# =========================================================
# CHAT HELPERS
# =========================================================

def normalize_chat(value):
    value = str(value).strip()

    # Saved Messages
    if value.lower() in ("me", "saved", "saved_messages"):
        return "me"

    if value.startswith(("https://t.me/", "http://t.me/")):
        value = value.rstrip("/")

        parts = value.split("/")

        if len(parts) >= 4:
            value = parts[3]

    value = value.replace("@", "")

    if value.lstrip("-").isdigit():
        return int(value)

    return value


async def ensure_access(chat):
    try:
        await app.get_chat(chat)
        return

    except Exception as first_err:

        # Public username ho to join try kar sakte hain
        if isinstance(chat, str) and chat != "me":

            try:
                await app.join_chat(chat)
                return

            except Exception as e:
                raise RuntimeError(
                    f"Public chat '{chat}' me join nahi ho paya.\n"
                    f"Error: {e}"
                ) from e

        raise RuntimeError(
            f"Chat '{chat}' access nahi ho raha.\n"
            f"Session-account ko is chat ka member hona chahiye.\n"
            f"Error: {first_err}"
        )


# =========================================================
# SOURCE HISTORY
# =========================================================

def _message_topic_id(message):
    for attr in (
        "message_thread_id",
        "reply_to_top_message_id",
    ):
        tid = getattr(message, attr, None)

        if tid:
            return tid

    reply_to = getattr(
        message,
        "reply_to_message_id",
        None,
    )

    if reply_to:
        return reply_to

    return None


async def fetch_source_messages(
    source,
    src_topic,
    from_id,
    to_id,
):
    await ensure_access(source)

    messages = []

    try:
        async for message in app.get_chat_history(source):

            if message.id < from_id:
                break

            if message.id > to_id:
                continue

            if src_topic:
                tid = _message_topic_id(message)

                if tid != src_topic:
                    continue

            messages.append(message)

    except RPCError as e:
        raise RuntimeError(str(e)) from e

    messages.reverse()

    return messages


# =========================================================
# FORWARD
# =========================================================

async def forward_one(
    message,
    target_chat,
):
    """
    Actual Telegram forwarding.

    Source:
        message.chat.id

    Target:
        target_chat

    Message:
        message.id
    """

    return await app.forward_messages(
        chat_id=target_chat,
        from_chat_id=message.chat.id,
        message_ids=message.id,
    )


# =========================================================
# OWNER CONTROL
# =========================================================

def owner_only(handler):

    async def wrapper(update, context):

        user = update.effective_user

        if user is None:
            return

        if user.id != OWNER_ID:
            return

        return await handler(
            update,
            context,
        )

    return wrapper


# =========================================================
# START
# =========================================================

async def start_command(
    update,
    context,
):
    await update.effective_message.reply_text(
        "🚀 Bulk Forward Bot\n\n"

        "/clone Source Target From_ID To_ID\n\n"

        "Example:\n"
        "/clone -1001234567890 -1009876543210 1 100\n\n"

        "Saved Messages se:\n"
        "/clone me TARGET 1 100\n\n"

        "/setprefix text\n"
        "/setsuffix text\n"
        "/setreplace old | new\n"
        "/captionmode keep|remove|replace\n"
        "/setcaptionreplace old | new\n"
        "/status\n"
        "/reset\n"
        "/ping\n"
        "/uptime\n\n"

        "📤 Messages actual user session se forward hote hain.\n"
        "🤖 Bot sirf commands receive karta hai."
    )


# =========================================================
# CLONE
# =========================================================

@owner_only
async def clone_command(
    update,
    context,
):
    args = context.args

    if len(args) < 4:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/clone Source Target From_ID To_ID\n\n"
            "Example:\n"
            "/clone -1001234567890 -1009876543210 1 100\n\n"
            "Saved Messages:\n"
            "/clone me TARGET 1 100"
        )
        return

    source = normalize_chat(args[0])
    target = normalize_chat(args[1])

    try:
        from_id = int(args[2])
        to_id = int(args[3])

    except ValueError:
        await update.effective_message.reply_text(
            "❌ Message IDs numbers hone chahiye."
        )
        return

    if from_id > to_id:
        from_id, to_id = to_id, from_id

    status = await update.effective_message.reply_text(
        "🔎 Source messages read kar raha hoon..."
    )

    # -----------------------------------------------------
    # SOURCE
    # -----------------------------------------------------

    try:
        messages = await fetch_source_messages(
            source,
            0,
            from_id,
            to_id,
        )

    except Exception as e:
        await status.edit_text(
            "❌ Source read failed:\n\n"
            f"{e}"
        )
        return

    if not messages:
        await status.edit_text(
            "⚠️ Is message range me koi message nahi mila."
        )
        return

    # -----------------------------------------------------
    # TARGET
    # -----------------------------------------------------

    try:
        await ensure_access(target)

    except Exception as e:
        await status.edit_text(
            "❌ Target access failed:\n\n"
            f"{e}"
        )
        return

    # -----------------------------------------------------
    # TRANSFER
    # -----------------------------------------------------

    success = 0
    failed = 0

    total = len(messages)

    for index, message_item in enumerate(
        messages,
        start=1,
    ):

        result = None

        try:

            result = await forward_one(
                message_item,
                target,
            )

        except FloodWait as e:

            wait_seconds = (
                int(getattr(e, "value", 1)) + 1
            )

            print(
                f"FloodWait: sleeping {wait_seconds}s",
                flush=True,
            )

            await asyncio.sleep(
                wait_seconds
            )

            try:

                result = await forward_one(
                    message_item,
                    target,
                )

            except Exception as retry_error:

                print(
                    f"Retry failed "
                    f"{message_item.id}: "
                    f"{retry_error}",
                    flush=True,
                )

        except Exception as e:

            print(
                f"Forward error "
                f"{message_item.id}: {e}",
                flush=True,
            )

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        if result:

            # forward_messages generally returns Message
            # or list depending on Pyrogram/version.
            if isinstance(result, list):
                target_message = (
                    result[0]
                    if result
                    else None
                )
            else:
                target_message = result

            if target_message:

                try:

                    save_mapping(
                        source,
                        0,
                        message_item.id,
                        target,
                        0,
                        target_message.id,
                    )

                except Exception as e:

                    print(
                        f"Mapping save error "
                        f"{message_item.id}: {e}",
                        flush=True,
                    )

            success += 1

        else:
            failed += 1

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        if (
            index == 1
            or index % 5 == 0
            or index == total
        ):

            percentage = (
                index / total * 100
            )

            try:

                await status.edit_text(
                    "⏳ Bulk Forward\n\n"
                    f"Processed: `{index}/{total}`\n"
                    f"Success: `{success}`\n"
                    f"Failed: `{failed}`\n"
                    f"Progress: `{percentage:.1f}%`"
                )

            except Exception:
                pass

        await asyncio.sleep(0.3)

    # -----------------------------------------------------
    # COMPLETE
    # -----------------------------------------------------

    try:

        await status.edit_text(
            "🏁 Bulk Forward Completed\n\n"
            f"✅ Success: `{success}`\n"
            f"❌ Failed: `{failed}`\n"
            f"📦 Total: `{total}`"
        )

    except Exception:
        pass


# =========================================================
# PREFIX
# =========================================================

@owner_only
async def set_prefix_command(
    update,
    context,
):
    if not context.args:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/setprefix Your text\n"
            "(/setprefix - to clear)"
        )

        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    CONFIG["prefix"] = (
        ""
        if value.strip() == "-"
        else value
    )

    await update.effective_message.reply_text(
        "✅ Prefix updated."
    )


# =========================================================
# SUFFIX
# =========================================================

@owner_only
async def set_suffix_command(
    update,
    context,
):
    if not context.args:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/setsuffix Your text\n"
            "(/setsuffix - to clear)"
        )

        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    CONFIG["suffix"] = (
        ""
        if value.strip() == "-"
        else value
    )

    await update.effective_message.reply_text(
        "✅ Suffix updated."
    )


# =========================================================
# REPLACE
# =========================================================

@owner_only
async def set_replace_command(
    update,
    context,
):
    if not context.args:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/setreplace old | new\n\n"
            "Clear:\n"
            "/setreplace -"
        )

        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    if value.strip() == "-":

        CONFIG["replace_from"] = ""
        CONFIG["replace_to"] = ""

        await update.effective_message.reply_text(
            "✅ Replacement cleared."
        )

        return

    if " | " not in value:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/setreplace old | new"
        )

        return

    old, new = value.split(
        " | ",
        1,
    )

    CONFIG["replace_from"] = old.strip()
    CONFIG["replace_to"] = new.strip()

    await update.effective_message.reply_text(
        "✅ Replacement configured."
    )


# =========================================================
# CAPTION MODE
# =========================================================

@owner_only
async def caption_mode_command(
    update,
    context,
):
    if (
        not context.args
        or context.args[0].lower()
        not in (
            "keep",
            "remove",
            "replace",
        )
    ):

        await update.effective_message.reply_text(
            "Usage:\n"
            "/captionmode keep|remove|replace"
        )

        return

    CONFIG["caption_mode"] = (
        context.args[0].lower()
    )

    await update.effective_message.reply_text(
        "✅ Caption mode set to: "
        f"{CONFIG['caption_mode']}"
    )


# =========================================================
# CAPTION REPLACE
# =========================================================

@owner_only
async def set_caption_replace_command(
    update,
    context,
):
    if not context.args:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/setcaptionreplace old | new"
        )

        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    if value.strip() == "-":

        CONFIG["caption_replace_from"] = ""
        CONFIG["caption_replace_to"] = ""

        await update.effective_message.reply_text(
            "✅ Caption replacement cleared."
        )

        return

    if " | " not in value:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/setcaptionreplace old | new"
        )

        return

    old, new = value.split(
        " | ",
        1,
    )

    CONFIG["caption_replace_from"] = old.strip()
    CONFIG["caption_replace_to"] = new.strip()

    await update.effective_message.reply_text(
        "✅ Caption replacement configured."
    )


# =========================================================
# STATUS
# =========================================================

@owner_only
async def status_command(
    update,
    context,
):
    await update.effective_message.reply_text(
        "📊 Settings\n\n"
        f"Prefix: "
        f"{CONFIG['prefix'] or 'None'}\n"
        f"Suffix: "
        f"{CONFIG['suffix'] or 'None'}\n"
        f"Text Replace: "
        f"{CONFIG['replace_from'] or 'None'} → "
        f"{CONFIG['replace_to'] or 'None'}\n"
        f"Caption Mode: "
        f"{CONFIG['caption_mode']}\n"
        f"Caption Replace: "
        f"{CONFIG['caption_replace_from'] or 'None'} → "
        f"{CONFIG['caption_replace_to'] or 'None'}"
    )


# =========================================================
# RESET
# =========================================================

@owner_only
async def reset_command(
    update,
    context,
):
    global CONFIG

    CONFIG = default_config()

    await update.effective_message.reply_text(
        "🔄 Configuration reset."
    )


# =========================================================
# UPTIME
# =========================================================

def _format_uptime():

    delta = int(
        time.time() - START_TIME
    )

    days, rem = divmod(
        delta,
        86400,
    )

    hours, rem = divmod(
        rem,
        3600,
    )

    minutes, seconds = divmod(
        rem,
        60,
    )

    parts = []

    if days:
        parts.append(
            f"{days}d"
        )

    if hours or days:
        parts.append(
            f"{hours}h"
        )

    if minutes or hours or days:
        parts.append(
            f"{minutes}m"
        )

    parts.append(
        f"{seconds}s"
    )

    return " ".join(parts)


# =========================================================
# PING
# =========================================================

async def ping_command(
    update,
    context,
):
    start = time.perf_counter()

    msg = await update.effective_message.reply_text(
        "🏓 Pinging..."
    )

    elapsed_ms = (
        time.perf_counter() - start
    ) * 1000

    await msg.edit_text(
        f"🏓 Pong! `{elapsed_ms:.0f} ms`"
    )


# =========================================================
# UPTIME COMMAND
# =========================================================

async def uptime_command(
    update,
    context,
):
    await update.effective_message.reply_text(
        f"⏱ Uptime: {_format_uptime()}"
    )


# =========================================================
# WEB SERVER
# =========================================================

async def home(request):

    return web.Response(
        text=(
            "Running.\n"
            f"Uptime: {_format_uptime()}"
        ),
        status=200,
    )


async def start_web_server():

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        home,
    )

    runner = web.AppRunner(
        web_app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    print(
        f"HTTP server running on port {PORT}",
        flush=True,
    )


# =========================================================
# TELEGRAM BOT
# =========================================================

async def run_bot():

    ptb_app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    ptb_app.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "clone",
            clone_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "setprefix",
            set_prefix_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "setsuffix",
            set_suffix_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "setreplace",
            set_replace_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "captionmode",
            caption_mode_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "setcaptionreplace",
            set_caption_replace_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "reset",
            reset_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "ping",
            ping_command,
        )
    )

    ptb_app.add_handler(
        CommandHandler(
            "uptime",
            uptime_command,
        )
    )

    await ptb_app.initialize()

    await ptb_app.bot.delete_webhook(
        drop_pending_updates=True
    )

    await ptb_app.start()

    await ptb_app.updater.start_polling(
        drop_pending_updates=True
    )

    print(
        "Bot polling started — "
        "all commands active.",
        flush=True,
    )

    try:

        while True:
            await asyncio.sleep(10)

    finally:

        try:
            await ptb_app.updater.stop()
        except Exception:
            pass

        try:
            await ptb_app.stop()
        except Exception:
            pass

        try:
            await ptb_app.shutdown()
        except Exception:
            pass


# =========================================================
# MAIN RUN
# =========================================================

async def run():

    init_db()

    # Start USER SESSION
    await app.start()

    me = await app.get_me()

    print(
        "Userbot session online: "
        f"{me.first_name} "
        f"(@{me.username})",
        flush=True,
    )

    await start_web_server()

    await run_bot()


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    asyncio.run(run())
