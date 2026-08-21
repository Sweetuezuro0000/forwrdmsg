import os
import re
import sqlite3
import asyncio
from typing import Optional

from aiohttp import web

from pyrogram import Client
from pyrogram.enums import MessageEntityType
from pyrogram.errors import FloodWait, RPCError

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

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

PORT = int(os.environ.get("PORT", "10000"))

# Pyrogram bot session.
PYROGRAM_SESSION = os.environ.get(
    "PYROGRAM_SESSION",
    "source_reader_bot"
)


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

        "caption_mode": "keep",
        "caption_replace_from": "",
        "caption_replace_to": "",
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


def get_mapping(
    source_chat,
    source_topic,
    source_msg_id
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
# PYROGRAM SOURCE CLIENT
# =========================================================

source_client = Client(
    PYROGRAM_SESSION,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# =========================================================
# SOURCE CHAT NORMALIZATION
# =========================================================

def normalize_chat(value):

    value = str(value).strip()

    if value.startswith("https://t.me/"):
        value = value.rstrip("/")

        parts = value.split("/")

        if len(parts) >= 4:

            # https://t.me/username
            value = parts[3]

    value = value.replace("@", "")

    if value.lstrip("-").isdigit():
        return int(value)

    return value


# =========================================================
# TEXT PROCESSING
# =========================================================

def replace_plain_text(text, config):

    if not text:
        return ""

    if config["replace_from"]:
        text = text.replace(
            config["replace_from"],
            config["replace_to"]
        )

    return (
        config["prefix"]
        + text
        + config["suffix"]
    )


# =========================================================
# TELEGRAM LINK PROCESSING
# =========================================================

TELEGRAM_LINK_PATTERN = re.compile(
    r"https?://t\.me/"
    r"(c/\d+|[A-Za-z0-9_]+)"
    r"(?:/(\d+))?"
    r"(?:/(\d+))?"
)


def rewrite_telegram_url(
    url,
    source_chat,
    config
):

    if not url:
        return url

    match = TELEGRAM_LINK_PATTERN.fullmatch(
        url
    )

    if not match:
        return url

    chat_part = match.group(1)

    first_id = match.group(2)
    second_id = match.group(3)

    if not first_id:
        return url

    message_id = int(
        second_id or first_id
    )

    mapped = get_mapping(
        source_chat,
        0,
        message_id
    )

    if not mapped:
        return url

    target_chat, target_topic, target_msg = mapped

    # Public username target.
    target_chat_clean = str(
        target_chat
    ).replace("-100", "")

    # For public target chats.
    if str(target_chat).lstrip("-").isdigit():
        return (
            f"https://t.me/c/"
            f"{target_chat_clean}/"
            f"{target_msg}"
        )

    return (
        f"https://t.me/"
        f"{target_chat_clean}/"
        f"{target_msg}"
    )


# =========================================================
# ENTITY HELPERS
# =========================================================

def get_message_text(message):

    if message.text:
        return message.text

    if message.caption:
        return message.caption

    return ""


def get_entities(message):

    if message.entities:
        return message.entities

    if message.caption_entities:
        return message.caption_entities

    return []


# =========================================================
# ENTITY-AWARE TEXT PROCESSING
# =========================================================

def process_message_text(
    message,
    config,
    source_chat
):

    text = get_message_text(message)

    if not text:
        return "", []

    entities = get_entities(message)

    # -----------------------------------------------------
    # First pass: replace hyperlinks inside entities
    # -----------------------------------------------------

    rewritten_urls = {}

    for entity in entities:

        if entity.type in (
            MessageEntityType.TEXT_LINK,
            MessageEntityType.URL,
        ):

            try:

                if entity.type == MessageEntityType.TEXT_LINK:

                    old_url = entity.url

                    new_url = rewrite_telegram_url(
                        old_url,
                        source_chat,
                        config
                    )

                    rewritten_urls[
                        id(entity)
                    ] = new_url

            except Exception:
                pass

    # -----------------------------------------------------
    # Plain text processing
    # -----------------------------------------------------

    processed = replace_plain_text(
        text,
        config
    )

    return processed, entities


# =========================================================
# CAPTION PROCESSING
# =========================================================

def process_caption(
    caption,
    config
):

    if not caption:
        return None

    mode = config["caption_mode"]

    if mode == "remove":
        return None

    if mode == "replace":

        caption = caption.replace(
            config["caption_replace_from"],
            config["caption_replace_to"]
        )

    caption = (
        config["prefix"]
        + caption
        + config["suffix"]
    )

    return caption


# =========================================================
# SEND TEXT MESSAGE
# =========================================================

async def send_text_message(
    message,
    target_chat,
    target_topic,
    config
):

    text = message.text

    if not text:
        return None

    processed = replace_plain_text(
        text,
        config
    )

    kwargs = {}

    if target_topic:
        kwargs["message_thread_id"] = target_topic

    # Telegram Bot API will preserve entities
    # when supplied explicitly.

    if message.entities:

        kwargs["entities"] = [
            entity_to_bot_entity(e)
            for e in message.entities
        ]

    return await target_bot.send_message(
        chat_id=target_chat,
        text=processed,
        **kwargs
    )


# =========================================================
# PYROGRAM ENTITY → BOT API ENTITY
# =========================================================

from telegram import MessageEntity as BotMessageEntity


def entity_to_bot_entity(entity):

    entity_type = entity.type

    mapping = {

        MessageEntityType.MENTION:
            "mention",

        MessageEntityType.HASHTAG:
            "hashtag",

        MessageEntityType.CASHTAG:
            "cashtag",

        MessageEntityType.BOT_COMMAND:
            "bot_command",

        MessageEntityType.URL:
            "url",

        MessageEntityType.EMAIL:
            "email",

        MessageEntityType.PHONE_NUMBER:
            "phone_number",

        MessageEntityType.BOLD:
            "bold",

        MessageEntityType.ITALIC:
            "italic",

        MessageEntityType.UNDERLINE:
            "underline",

        MessageEntityType.STRIKETHROUGH:
            "strikethrough",

        MessageEntityType.SPOILER:
            "spoiler",

        MessageEntityType.CODE:
            "code",

        MessageEntityType.PRE:
            "pre",

        MessageEntityType.TEXT_LINK:
            "text_link",

        MessageEntityType.CUSTOM_EMOJI:
            "custom_emoji",
    }

    kwargs = {
        "type": mapping.get(
            entity_type,
            "text_mention"
        ),
        "offset": entity.offset,
        "length": entity.length,
    }

    if entity.type == MessageEntityType.TEXT_LINK:

        kwargs["url"] = entity.url

    if entity.type == MessageEntityType.TEXT_MENTION:

        kwargs["user"] = entity.user.id

    if entity.type == MessageEntityType.PRE:

        kwargs["language"] = entity.language

    if entity.type == MessageEntityType.CUSTOM_EMOJI:

        kwargs["custom_emoji_id"] = (
            entity.custom_emoji_id
        )

    return BotMessageEntity(
        **kwargs
    )


# =========================================================
# MEDIA SENDING
# =========================================================

async def send_media_message(
    message,
    target_chat,
    target_topic,
    config
):

    caption = process_caption(
        message.caption,
        config
    )

    kwargs = {}

    if target_topic:
        kwargs["message_thread_id"] = target_topic

    if caption:
        kwargs["caption"] = caption

    # -----------------------------------------------------
    # PHOTO
    # -----------------------------------------------------

    if message.photo:

        return await target_bot.send_photo(
            chat_id=target_chat,
            photo=message.photo.file_id,
            **kwargs
        )

    # -----------------------------------------------------
    # VIDEO
    # -----------------------------------------------------

    if message.video:

        return await target_bot.send_video(
            chat_id=target_chat,
            video=message.video.file_id,
            **kwargs
        )

    # -----------------------------------------------------
    # DOCUMENT
    # -----------------------------------------------------

    if message.document:

        return await target_bot.send_document(
            chat_id=target_chat,
            document=message.document.file_id,
            **kwargs
        )

    # -----------------------------------------------------
    # AUDIO
    # -----------------------------------------------------

    if message.audio:

        return await target_bot.send_audio(
            chat_id=target_chat,
            audio=message.audio.file_id,
            **kwargs
        )

    # -----------------------------------------------------
    # VOICE
    # -----------------------------------------------------

    if message.voice:

        return await target_bot.send_voice(
            chat_id=target_chat,
            voice=message.voice.file_id,
            **kwargs
        )

    # -----------------------------------------------------
    # ANIMATION
    # -----------------------------------------------------

    if message.animation:

        return await target_bot.send_animation(
            chat_id=target_chat,
            animation=message.animation.file_id,
            **kwargs
        )

    return None


# =========================================================
# CLONE
# =========================================================

async def clone_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.effective_message:
        return

    args = context.args

    if len(args) < 4:

        await update.effective_message.reply_text(
            "Usage:\n"
            "/clone Source Target From_ID To_ID "
            "[Src_Topic_ID] [Tgt_Topic_ID]"
        )

        return

    source = normalize_chat(args[0])
    target = normalize_chat(args[1])

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

    config = get_config(
        update.effective_user.id
    )

    total = max(
        0,
        to_id - from_id + 1
    )

    status = await update.effective_message.reply_text(
        "🚀 Transfer started..."
    )

    success = 0
    failed = 0

    # -----------------------------------------------------
    # SOURCE HISTORY
    # -----------------------------------------------------

    try:

        messages = []

        async for message in source_client.get_chat_history(
            source
        ):

            if message.id < from_id:
                break

            if message.id > to_id:
                continue

            # Topic filtering.
            if src_topic:

                message_topic = (
                    message.message_thread_id
                    or 0
                )

                if message_topic != src_topic:
                    continue

            messages.append(message)

        messages.reverse()

    except Exception as e:

        await status.edit_text(
            f"❌ Could not read source chat:\n{e}"
        )

        return

    # -----------------------------------------------------
    # TRANSFER
    # -----------------------------------------------------

    for index, message in enumerate(
        messages,
        start=1
    ):

        try:

            result = None

            # Text
            if message.text:

                result = await send_text_message(
                    message,
                    target,
                    tgt_topic,
                    config
                )

            # Media
            else:

                result = await send_media_message(
                    message,
                    target,
                    tgt_topic,
                    config
                )

            if result:

                save_mapping(
                    source,
                    src_topic,
                    message.id,

                    target,
                    tgt_topic,
                    result.message_id
                )

                success += 1

            else:

                failed += 1

        except FloodWait as e:

            await asyncio.sleep(
                e.value + 1
            )

            try:

                # Retry same message.
                if message.text:

                    result = await send_text_message(
                        message,
                        target,
                        tgt_topic,
                        config
                    )

                else:

                    result = await send_media_message(
                        message,
                        target,
                        tgt_topic,
                        config
                    )

                if result:

                    save_mapping(
                        source,
                        src_topic,
                        message.id,

                        target,
                        tgt_topic,
                        result.message_id
                    )

                    success += 1

            except Exception as retry_error:

                failed += 1

                print(
                    f"Retry failed "
                    f"{message.id}: "
                    f"{retry_error}",
                    flush=True
                )

        except Exception as e:

            failed += 1

            print(
                f"Transfer error "
                f"{message.id}: {e}",
                flush=True
            )

        # -------------------------------------------------
        # PROGRESS
        # -------------------------------------------------

        if (
            index == 1
            or index % 5 == 0
            or index == len(messages)
        ):

            percentage = (
                index / len(messages) * 100
                if messages
                else 100
            )

            try:

                await status.edit_text(
                    "⏳ Bulk Transfer\n\n"
                    f"Processed: `{index}/{len(messages)}`\n"
                    f"Success: `{success}`\n"
                    f"Failed: `{failed}`\n"
                    f"Progress: `{percentage:.1f}%`"
                )

            except TelegramError:
                pass

        await asyncio.sleep(
            0.3
        )

    # -----------------------------------------------------
    # COMPLETE
    # -----------------------------------------------------

    try:

        await status.edit_text(
            "🏁 Bulk Transfer Completed\n\n"
            f"✅ Success: `{success}`\n"
            f"❌ Failed: `{failed}`\n"
            f"📦 Total: `{len(messages)}`"
        )

    except TelegramError:
        pass


# =========================================================
# BASIC COMMANDS
# =========================================================

async def start_command(update, context):

    await update.effective_message.reply_text(
        "🚀 Bulk Transfer Bot\n\n"

        "/clone Source Target From_ID To_ID "
        "[Src_Topic_ID] [Tgt_Topic_ID]\n\n"

        "/setprefix text\n"
        "/setsuffix text\n"
        "/setreplace old | new\n"
        "/setlink old | new\n"
        "/status\n"
        "/reset"
    )


async def set_prefix(update, context):

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


async def set_suffix(update, context):

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


async def set_replace(update, context):

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
        "✅ Replacement configured."
    )


async def set_link(update, context):

    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setlink old | new"
        )
        return

    value = update.effective_message.text.split(
        maxsplit=1
    )[1]

    if " | " not in value:
        await update.effective_message.reply_text(
            "Usage:\n/setlink old | new"
        )
        return

    old, new = value.split(
        " | ",
        1
    )

    config = get_config(
        update.effective_user.id
    )

    config["old_chat"] = old.strip().replace(
        "@",
        ""
    )

    config["new_chat"] = new.strip().replace(
        "@",
        ""
    )

    await update.effective_message.reply_text(
        "✅ Link mapping configured."
    )


async def status_command(update, context):

    config = get_config(
        update.effective_user.id
    )

    await update.effective_message.reply_text(
        "📊 Settings\n\n"
        f"Prefix: {config['prefix'] or 'None'}\n"
        f"Suffix: {config['suffix'] or 'None'}\n"
        f"Replace: "
        f"{config['replace_from'] or 'None'} → "
        f"{config['replace_to'] or 'None'}\n"
        f"Old Chat: {config['old_chat'] or 'None'}\n"
        f"New Chat: {config['new_chat'] or 'None'}"
    )


async def reset_command(update, context):

    USER_CONFIGS[
        update.effective_user.id
    ] = default_config()

    await update.effective_message.reply_text(
        "🔄 Configuration reset."
    )


# =========================================================
# WEB SERVER
# =========================================================

async def home(request):

    return web.Response(
        text="Bulk Manager Bot is running.",
        status=200
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        home
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"HTTP server running on port {PORT}",
        flush=True
    )


# =========================================================
# BOT COMMANDS
# =========================================================

async def setup_commands(application):

    await application.bot.set_my_commands([

        BotCommand(
            "start",
            "Start bot"
        ),

        BotCommand(
            "setprefix",
            "Add prefix"
        ),

        BotCommand(
            "setsuffix",
            "Add suffix"
        ),

        BotCommand(
            "setreplace",
            "Replace text"
        ),

        BotCommand(
            "setlink",
            "Configure links"
        ),

        BotCommand(
            "status",
            "Show settings"
        ),

        BotCommand(
            "reset",
            "Reset settings"
        ),

        BotCommand(
            "clone",
            "Bulk transfer"
        ),

    ])


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context
):

    print(
        f"BOT ERROR: {context.error}",
        flush=True
    )


# =========================================================
# GLOBAL TARGET BOT
# =========================================================

target_bot = None


# =========================================================
# MAIN
# =========================================================

def main():

    global target_bot

    init_db()

    # -----------------------------------------------------
    # Telegram Bot API
    # -----------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_commands)
        .build()
    )

    target_bot = application.bot

    # -----------------------------------------------------
    # Pyrogram source bot
    # -----------------------------------------------------

    async def run():

        await source_client.start()

        me = await source_client.get_me()

        print(
            f"Pyrogram source client online: "
            f"@{me.username}",
            flush=True
        )

        # Web server
        await start_web_server()

        # Telegram Bot API polling
        await application.initialize()

        await application.start()

        await application.updater.start_polling(
            drop_pending_updates=True
        )

        print(
            "Target Bot polling started.",
            flush=True
        )

        await asyncio.Event().wait()

    # -----------------------------------------------------
    # Handlers
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "setprefix",
            set_prefix
        )
    )

    application.add_handler(
        CommandHandler(
            "setsuffix",
            set_suffix
        )
    )

    application.add_handler(
        CommandHandler(
            "setreplace",
            set_replace
        )
    )

    application.add_handler(
        CommandHandler(
            "setlink",
            set_link
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        CommandHandler(
            "reset",
            reset_command
        )
    )

    application.add_handler(
        CommandHandler(
            "clone",
            clone_command
        )
    )

    application.add_error_handler(
        error_handler
    )

    try:

        asyncio.run(run())

    finally:

        try:
            asyncio.run(
                source_client.stop()
            )
        except Exception:
            pass


if __name__ == "__main__":
    main() mera ye bilkul thik h 
