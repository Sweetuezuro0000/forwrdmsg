import os
import re
import sqlite3
import asyncio

from aiohttp import web

from pyrogram import Client
from pyrogram.enums import MessageEntityType
from pyrogram.errors import FloodWait, RPCError

from telegram import Update, BotCommand, MessageEntity as BotMessageEntity
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes


# =========================================================
# ENVIRONMENT
# =========================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
PORT = int(os.environ.get("PORT", "10000"))
PYROGRAM_SESSION = os.environ.get("PYROGRAM_SESSION", "source_reader_bot")


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

        # caption_mode: keep | remove | replace
        "caption_mode": "keep",
        "caption_replace_from": "",
        "caption_replace_to": "",
    }


def get_config(user_id):
    if user_id not in USER_CONFIGS:
        USER_CONFIGS[user_id] = default_config()
    return USER_CONFIGS[user_id]


# =========================================================
# DATABASE (source msg -> target msg mapping, used for link rewrite)
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

            PRIMARY KEY (source_chat, source_topic, source_msg_id)
        )
    """)
    conn.commit()
    conn.close()


def save_mapping(source_chat, source_topic, source_msg_id,
                  target_chat, target_topic, target_msg_id):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        INSERT OR REPLACE INTO mappings
        (source_chat, source_topic, source_msg_id,
         target_chat, target_topic, target_msg_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(source_chat), int(source_topic or 0), int(source_msg_id),
        str(target_chat), int(target_topic or 0), int(target_msg_id),
    ))
    conn.commit()
    conn.close()


def get_mapping(source_chat, source_topic, source_msg_id):
    conn = sqlite3.connect(DB_FILE)
    row = conn.execute("""
        SELECT target_chat, target_topic, target_msg_id
        FROM mappings
        WHERE source_chat = ? AND source_topic = ? AND source_msg_id = ?
    """, (str(source_chat), int(source_topic or 0), int(source_msg_id))).fetchone()
    conn.close()
    return row


# =========================================================
# PYROGRAM SOURCE CLIENT (reads source chat)
# =========================================================

source_client = Client(
    PYROGRAM_SESSION,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

target_bot = None  # set in main()


# =========================================================
# CHAT NORMALIZATION
# =========================================================

def normalize_chat(value):
    value = str(value).strip()

    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        value = value.rstrip("/")
        parts = value.split("/")
        if len(parts) >= 4:
            value = parts[3]

    value = value.replace("@", "")

    if value.lstrip("-").isdigit():
        return int(value)

    return value


# =========================================================
# AUTO ACCESS TO SOURCE CHAT
# (bot only needs to be admin in the TARGET chat; a public
#  source chat/topic is picked up automatically)
# =========================================================

async def ensure_source_access(chat):
    try:
        await source_client.get_chat(chat)
        return
    except Exception:
        pass

    if isinstance(chat, str):
        try:
            await source_client.join_chat(chat)
            return
        except Exception as e:
            raise RuntimeError(
                "Source group/channel me bot khud join nahi ho paya.\n"
                "Ye sirf tab automatic hota hai jab source PUBLIC ho "
                "(username wala link/handle).\n"
                f"Error: {e}"
            )

    raise RuntimeError(
        "Source private hai (sirf numeric ID diya gaya) — private chat me "
        "bot ko khud add nahi kar sakta, wahan bhi bot ko manually add karna hoga."
    )


# =========================================================
# UTF-16 HELPERS (Telegram entity offsets are UTF-16 code units)
# =========================================================

def utf16_len(text):
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


# =========================================================
# TELEGRAM LINK REWRITING
# =========================================================

TELEGRAM_LINK_PATTERN = re.compile(
    r"https?://t\.me/"
    r"(c/\d+|[A-Za-z0-9_]+)"
    r"(?:/(\d+))?"
    r"(?:/(\d+))?"
)


def _build_target_link(target_chat, target_topic, target_msg):
    target_chat_str = str(target_chat)

    if target_chat_str.lstrip("-").isdigit():
        cid = target_chat_str.replace("-100", "")
        if target_topic:
            return f"https://t.me/c/{cid}/{target_topic}/{target_msg}"
        return f"https://t.me/c/{cid}/{target_msg}"

    if target_topic:
        return f"https://t.me/{target_chat_str}/{target_topic}/{target_msg}"
    return f"https://t.me/{target_chat_str}/{target_msg}"


def rewrite_telegram_url(url, source_chat, source_topic):
    """If `url` points to a message we already transferred, rewrite it to
    point at the corresponding message in the target chat/topic."""
    if not url:
        return url

    match = TELEGRAM_LINK_PATTERN.fullmatch(url.strip())
    if not match:
        return url

    first_id = match.group(2)
    second_id = match.group(3)
    if not first_id:
        return url

    # For .../c/<chat>/<topic>/<msg> links second_id is the real message id.
    # For .../<chat>/<msg> links first_id is the message id.
    message_id = int(second_id or first_id)

    mapped = get_mapping(source_chat, source_topic or 0, message_id)
    if not mapped:
        return url

    target_chat, target_topic, target_msg = mapped
    return _build_target_link(target_chat, target_topic, target_msg)


def rewrite_plain_urls(text, source_chat, source_topic):
    if not text:
        return text

    def _sub(m):
        return rewrite_telegram_url(m.group(0), source_chat, source_topic)

    return TELEGRAM_LINK_PATTERN.sub(_sub, text)


# =========================================================
# PLAIN TEXT PROCESSING (prefix / suffix / replace)
# =========================================================

def apply_replace(text, from_, to_):
    if from_:
        text = text.replace(from_, to_)
    return text


def apply_prefix_suffix(text, prefix, suffix):
    return f"{prefix}{text}{suffix}"


# =========================================================
# ENTITY CONVERSION (Pyrogram -> python-telegram-bot)
# =========================================================

_ENTITY_TYPE_MAP = {
    MessageEntityType.MENTION: "mention",
    MessageEntityType.HASHTAG: "hashtag",
    MessageEntityType.CASHTAG: "cashtag",
    MessageEntityType.BOT_COMMAND: "bot_command",
    MessageEntityType.URL: "url",
    MessageEntityType.EMAIL: "email",
    MessageEntityType.PHONE_NUMBER: "phone_number",
    MessageEntityType.BOLD: "bold",
    MessageEntityType.ITALIC: "italic",
    MessageEntityType.UNDERLINE: "underline",
    MessageEntityType.STRIKETHROUGH: "strikethrough",
    MessageEntityType.SPOILER: "spoiler",
    MessageEntityType.CODE: "code",
    MessageEntityType.PRE: "pre",
    MessageEntityType.TEXT_LINK: "text_link",
    MessageEntityType.TEXT_MENTION: "text_mention",
    MessageEntityType.CUSTOM_EMOJI: "custom_emoji",
}

if hasattr(MessageEntityType, "BLOCKQUOTE"):
    _ENTITY_TYPE_MAP[MessageEntityType.BLOCKQUOTE] = "blockquote"


def entity_to_bot_entity(entity, offset_shift=0, url_override=None):
    bot_type = _ENTITY_TYPE_MAP.get(entity.type)
    if bot_type is None:
        return None  # unsupported / service entity types are skipped

    kwargs = {
        "type": bot_type,
        "offset": entity.offset + offset_shift,
        "length": entity.length,
    }

    if entity.type == MessageEntityType.TEXT_LINK:
        kwargs["url"] = url_override if url_override is not None else entity.url

    if entity.type == MessageEntityType.TEXT_MENTION and entity.user:
        kwargs["user"] = entity.user

    if entity.type == MessageEntityType.PRE and entity.language:
        kwargs["language"] = entity.language

    if entity.type == MessageEntityType.CUSTOM_EMOJI:
        kwargs["custom_emoji_id"] = entity.custom_emoji_id

    return BotMessageEntity(**kwargs)


def build_entities(entities, offset_shift, source_chat, source_topic):
    """Convert Pyrogram entities to Bot API entities, shifting offsets for
    any prepended prefix and rewriting internal Telegram links."""
    result = []
    for e in entities or []:
        url_override = None
        if e.type == MessageEntityType.TEXT_LINK:
            url_override = rewrite_telegram_url(e.url, source_chat, source_topic)

        try:
            be = entity_to_bot_entity(e, offset_shift=offset_shift, url_override=url_override)
        except Exception:
            be = None

        if be is not None:
            result.append(be)

    return result


def process_text_or_caption(raw_text, entities, config, source_chat, source_topic,
                             is_caption=False):
    """
    Returns (final_text, bot_entities_or_None).

    - If a plain text-replace is configured (global for text, or caption-specific
      for captions), we cannot safely keep the original entity offsets after an
      arbitrary find/replace, so formatting is dropped for that message and it
      is sent as plain text (prefix/suffix + link rewriting still apply).
    - Otherwise, formatting/entities are fully preserved, offsets are shifted
      for the prefix, and any t.me links pointing at already-transferred
      messages are rewritten to the target chat/topic.
    """
    raw_text = raw_text or ""

    if is_caption:
        mode = config["caption_mode"]
        if mode == "remove":
            return None, None
        replace_from = config["caption_replace_from"] if mode == "replace" else ""
        replace_to = config["caption_replace_to"] if mode == "replace" else ""
    else:
        replace_from = config["replace_from"]
        replace_to = config["replace_to"]

    prefix = config["prefix"]
    suffix = config["suffix"]

    if replace_from:
        # Formatting can't be reliably preserved through an arbitrary
        # substring replace, so fall back to plain text.
        text = apply_replace(raw_text, replace_from, replace_to)
        text = rewrite_plain_urls(text, source_chat, source_topic)
        text = apply_prefix_suffix(text, prefix, suffix)
        return (text or None) if is_caption else text, None

    shift = utf16_len(prefix)
    bot_entities = build_entities(entities, shift, source_chat, source_topic)
    final_text = apply_prefix_suffix(raw_text, prefix, suffix)

    if is_caption:
        return (final_text or None), (bot_entities or None)
    return final_text, (bot_entities or None)


# =========================================================
# SENDING
# =========================================================

async def send_text_message(message, target_chat, target_topic, config,
                             source_chat, source_topic):
    text = message.text
    if not text:
        return None

    final_text, entities = process_text_or_caption(
        text, message.entities, config, source_chat, source_topic, is_caption=False
    )

    if not final_text:
        return None

    kwargs = {}
    if target_topic:
        kwargs["message_thread_id"] = target_topic
    if entities:
        kwargs["entities"] = entities

    return await target_bot.send_message(chat_id=target_chat, text=final_text, **kwargs)


async def send_media_message(message, target_chat, target_topic, config,
                              source_chat, source_topic):
    caption_text, caption_entities = process_text_or_caption(
        message.caption, message.caption_entities, config,
        source_chat, source_topic, is_caption=True,
    )

    kwargs = {}
    if target_topic:
        kwargs["message_thread_id"] = target_topic
    if caption_text:
        kwargs["caption"] = caption_text
    if caption_entities:
        kwargs["caption_entities"] = caption_entities

    if message.photo:
        return await target_bot.send_photo(chat_id=target_chat, photo=message.photo.file_id, **kwargs)

    if message.video:
        return await target_bot.send_video(chat_id=target_chat, video=message.video.file_id, **kwargs)

    if message.document:
        return await target_bot.send_document(chat_id=target_chat, document=message.document.file_id, **kwargs)

    if message.audio:
        return await target_bot.send_audio(chat_id=target_chat, audio=message.audio.file_id, **kwargs)

    if message.voice:
        return await target_bot.send_voice(chat_id=target_chat, voice=message.voice.file_id, **kwargs)

    if message.animation:
        return await target_bot.send_animation(chat_id=target_chat, animation=message.animation.file_id, **kwargs)

    if message.sticker:
        return await target_bot.send_sticker(chat_id=target_chat, sticker=message.sticker.file_id,
                                              message_thread_id=target_topic or None)

    return None


async def send_one(message, target_chat, target_topic, config, source_chat, source_topic):
    if message.text:
        return await send_text_message(message, target_chat, target_topic, config,
                                        source_chat, source_topic)
    return await send_media_message(message, target_chat, target_topic, config,
                                     source_chat, source_topic)


# =========================================================
# FETCH SOURCE HISTORY (fixed topic filtering)
# =========================================================

async def fetch_source_messages(source, src_topic, from_id, to_id):
    await ensure_source_access(source)

    messages = []
    try:
        if src_topic:
            # search_messages supports server-side topic filtering via
            # message_thread_id — get_chat_history does NOT, which is why
            # the old code silently skipped every topic message.
            iterator = source_client.search_messages(source, message_thread_id=src_topic)
        else:
            iterator = source_client.get_chat_history(source)

        async for message in iterator:
            if message.id < from_id:
                break
            if message.id > to_id:
                continue
            messages.append(message)

    except RPCError as e:
        raise RuntimeError(str(e))

    messages.reverse()
    return messages


# =========================================================
# /clone COMMAND
# =========================================================

async def clone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_message:
        return

    args = context.args

    if len(args) < 4:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/clone Source Target From_ID To_ID [Src_Topic_ID] [Tgt_Topic_ID]\n\n"
            "Src_Topic_ID = 0 ya khali chodo agar General/non-topic group hai."
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
        await update.effective_message.reply_text("❌ IDs must be numbers.")
        return

    config = get_config(update.effective_user.id)

    status = await update.effective_message.reply_text("🔎 Source read kar raha hoon...")

    try:
        messages = await fetch_source_messages(source, src_topic, from_id, to_id)
    except Exception as e:
        await status.edit_text(f"❌ Could not read source chat:\n{e}")
        return

    if not messages:
        await status.edit_text(
            "⚠️ Is range/topic me koi message nahi mila.\n"
            "Check karo: Src_Topic_ID sahi hai? Bot source me access kar pa raha hai?"
        )
        return

    success = 0
    failed = 0
    total = len(messages)

    for index, message in enumerate(messages, start=1):
        try:
            result = await send_one(message, target, tgt_topic, config, source, src_topic)

            if result:
                save_mapping(source, src_topic, message.id, target, tgt_topic, result.message_id)
                success += 1
            else:
                failed += 1

        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            try:
                result = await send_one(message, target, tgt_topic, config, source, src_topic)
                if result:
                    save_mapping(source, src_topic, message.id, target, tgt_topic, result.message_id)
                    success += 1
                else:
                    failed += 1
            except Exception as retry_error:
                failed += 1
                print(f"Retry failed {message.id}: {retry_error}", flush=True)

        except Exception as e:
            failed += 1
            print(f"Transfer error {message.id}: {e}", flush=True)

        if index == 1 or index % 5 == 0 or index == total:
            percentage = index / total * 100
            try:
                await status.edit_text(
                    "⏳ Bulk Transfer\n\n"
                    f"Processed: `{index}/{total}`\n"
                    f"Success: `{success}`\n"
                    f"Failed: `{failed}`\n"
                    f"Progress: `{percentage:.1f}%`"
                )
            except TelegramError:
                pass

        await asyncio.sleep(0.3)

    try:
        await status.edit_text(
            "🏁 Bulk Transfer Completed\n\n"
            f"✅ Success: `{success}`\n"
            f"❌ Failed: `{failed}`\n"
            f"📦 Total: `{total}`\n\n"
            "ℹ️ Internal t.me links jo already-transferred messages ko point "
            "karte hain, wo target group/topic ki id se automatically update ho gaye."
        )
    except TelegramError:
        pass


# =========================================================
# BASIC COMMANDS
# =========================================================

async def start_command(update, context):
    await update.effective_message.reply_text(
        "🚀 Bulk Transfer Bot\n\n"
        "/clone Source Target From_ID To_ID [Src_Topic_ID] [Tgt_Topic_ID]\n\n"
        "/setprefix text\n"
        "/setsuffix text\n"
        "/setreplace old | new        (message text ke liye)\n"
        "/captionmode keep|remove|replace\n"
        "/setcaptionreplace old | new  (jab captionmode = replace ho)\n"
        "/status\n"
        "/reset\n\n"
        "Source group/channel PUBLIC hone par bot khud access le lega — "
        "sirf TARGET group me bot ko admin banana zaroori hai."
    )


async def set_prefix(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage:\n/setprefix Your text\n(/setprefix - to clear)")
        return
    value = update.effective_message.text.split(maxsplit=1)[1]
    get_config(update.effective_user.id)["prefix"] = "" if value.strip() == "-" else value
    await update.effective_message.reply_text("✅ Prefix updated.")


async def set_suffix(update, context):
    if not context.args:
        await update.effective_message.reply_text("Usage:\n/setsuffix Your text\n(/setsuffix - to clear)")
        return
    value = update.effective_message.text.split(maxsplit=1)[1]
    get_config(update.effective_user.id)["suffix"] = "" if value.strip() == "-" else value
    await update.effective_message.reply_text("✅ Suffix updated.")


async def set_replace(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setreplace old | new\n(/setreplace - to clear)\n\n"
            "⚠️ Text replace on hone par us message ki formatting (bold/link etc) "
            "preserve nahi hogi, plain text jayega."
        )
        return
    value = update.effective_message.text.split(maxsplit=1)[1]

    if value.strip() == "-":
        config = get_config(update.effective_user.id)
        config["replace_from"] = ""
        config["replace_to"] = ""
        await update.effective_message.reply_text("✅ Replacement cleared.")
        return

    if " | " not in value:
        await update.effective_message.reply_text("Usage:\n/setreplace old | new")
        return

    old, new = value.split(" | ", 1)
    config = get_config(update.effective_user.id)
    config["replace_from"] = old.strip()
    config["replace_to"] = new.strip()
    await update.effective_message.reply_text("✅ Replacement configured.")


async def caption_mode_command(update, context):
    if not context.args or context.args[0].lower() not in ("keep", "remove", "replace"):
        await update.effective_message.reply_text(
            "Usage:\n/captionmode keep\n/captionmode remove\n/captionmode replace"
        )
        return
    config = get_config(update.effective_user.id)
    config["caption_mode"] = context.args[0].lower()
    await update.effective_message.reply_text(f"✅ Caption mode set to: {config['caption_mode']}")


async def set_caption_replace(update, context):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage:\n/setcaptionreplace old | new\n"
            "(applies only when /captionmode is set to replace)"
        )
        return
    value = update.effective_message.text.split(maxsplit=1)[1]

    if " | " not in value:
        await update.effective_message.reply_text("Usage:\n/setcaptionreplace old | new")
        return

    old, new = value.split(" | ", 1)
    config = get_config(update.effective_user.id)
    config["caption_replace_from"] = old.strip()
    config["caption_replace_to"] = new.strip()
    await update.effective_message.reply_text("✅ Caption replacement configured.")


async def status_command(update, context):
    config = get_config(update.effective_user.id)
    await update.effective_message.reply_text(
        "📊 Settings\n\n"
        f"Prefix: {config['prefix'] or 'None'}\n"
        f"Suffix: {config['suffix'] or 'None'}\n"
        f"Text Replace: {config['replace_from'] or 'None'} → {config['replace_to'] or 'None'}\n"
        f"Caption Mode: {config['caption_mode']}\n"
        f"Caption Replace: {config['caption_replace_from'] or 'None'} → "
        f"{config['caption_replace_to'] or 'None'}"
    )


async def reset_command(update, context):
    USER_CONFIGS[update.effective_user.id] = default_config()
    await update.effective_message.reply_text("🔄 Configuration reset.")


# =========================================================
# WEB SERVER (keeps host platforms like Render happy)
# =========================================================

async def home(request):
    return web.Response(text="Bulk Manager Bot is running.", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", home)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP server running on port {PORT}", flush=True)


# =========================================================
# BOT COMMAND LIST
# =========================================================

async def setup_commands(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start bot"),
        BotCommand("setprefix", "Add prefix"),
        BotCommand("setsuffix", "Add suffix"),
        BotCommand("setreplace", "Replace text"),
        BotCommand("captionmode", "keep / remove / replace"),
        BotCommand("setcaptionreplace", "Caption replace text"),
        BotCommand("status", "Show settings"),
        BotCommand("reset", "Reset settings"),
        BotCommand("clone", "Bulk transfer"),
    ])


async def error_handler(update, context):
    print(f"BOT ERROR: {context.error}", flush=True)


# =========================================================
# MAIN
# =========================================================

def main():
    global target_bot

    init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(setup_commands).build()
    target_bot = application.bot

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("setprefix", set_prefix))
    application.add_handler(CommandHandler("setsuffix", set_suffix))
    application.add_handler(CommandHandler("setreplace", set_replace))
    application.add_handler(CommandHandler("captionmode", caption_mode_command))
    application.add_handler(CommandHandler("setcaptionreplace", set_caption_replace))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("reset", reset_command))
    application.add_handler(CommandHandler("clone", clone_command))
    application.add_error_handler(error_handler)

    async def run():
        await source_client.start()
        me = await source_client.get_me()
        print(f"Pyrogram source client online: @{me.username}", flush=True)

        await start_web_server()

        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        print("Target Bot polling started.", flush=True)

        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    finally:
        try:
            asyncio.run(source_client.stop())
        except Exception:
            pass


if __name__ == "__main__":
    main()
