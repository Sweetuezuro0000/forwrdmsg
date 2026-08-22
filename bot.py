import os
import re
import copy
import time
import sqlite3
import asyncio
import traceback

from aiohttp import web

from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.enums import MessageEntityType
from pyrogram.errors import FloodWait, RPCError
import pyrogram.utils as _pyroutils


# =========================================================
# PATCH: pyrogram 2.0.106 has outdated MIN_CHANNEL_ID/MIN_CHAT_ID range
# constants, so it raises "Peer id invalid" for newer (larger) channel
# IDs Telegram now issues. That crash kills processing of the WHOLE
# incoming update batch — silently dropping any other messages bundled
# in it, including our own commands. This replaces the range-check with
# a simple, future-proof format check (well-documented community fix).
# =========================================================

def _patched_get_peer_type(peer_id: int) -> str:
    peer_id_str = str(peer_id)
    if not peer_id_str.startswith("-"):
        return "user"
    if peer_id_str.startswith("-100"):
        return "channel"
    return "chat"


_pyroutils.get_peer_type = _patched_get_peer_type

from telegram.ext import Application, CommandHandler


# =========================================================
# ENVIRONMENT
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]   # your account — does EVERYTHING

BOT_TOKEN = os.environ["BOT_TOKEN"]             # used ONLY for /ping and /uptime
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

        # caption_mode: keep | remove | replace
        "caption_mode": "keep",
        "caption_replace_from": "",
        "caption_replace_to": "",
    }


CONFIG = default_config()


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
# PYROGRAM CLIENT — YOUR ACCOUNT. Reads source, sends to target,
# AND receives your control commands. This is the only thing doing work.
# =========================================================

app = Client(
    "userbot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)


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


async def ensure_access(chat):
    try:
        await app.get_chat(chat)
        return
    except Exception as first_err:
        err = first_err

    if isinstance(chat, str):
        try:
            await app.join_chat(chat)
            return
        except Exception as e:
            raise RuntimeError(f"Public chat '{chat}' me join nahi ho paya.\nError: {e}")

    raise RuntimeError(
        f"Chat ID {chat} access nahi ho raha. Aapka session-account is chat "
        f"ka member hona chahiye.\nError: {err}"
    )


# =========================================================
# UTF-16 HELPERS
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
    if not url:
        return url

    match = TELEGRAM_LINK_PATTERN.fullmatch(url.strip())
    if not match:
        return url

    first_id = match.group(2)
    second_id = match.group(3)
    if not first_id:
        return url

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
# ENTITY HANDLING (Pyrogram objects reused directly — same client sends)
# =========================================================

def clone_entity(entity, offset_shift, url_override=None):
    new_entity = copy.copy(entity)
    new_entity.offset = entity.offset + offset_shift
    if url_override is not None and entity.type == MessageEntityType.TEXT_LINK:
        new_entity.url = url_override
    return new_entity


def build_entities(entities, offset_shift, source_chat, source_topic):
    result = []
    for e in entities or []:
        url_override = None
        if e.type == MessageEntityType.TEXT_LINK:
            url_override = rewrite_telegram_url(e.url, source_chat, source_topic)
        try:
            result.append(clone_entity(e, offset_shift, url_override))
        except Exception:
            continue
    return result


def process_text_or_caption(raw_text, entities, config, source_chat, source_topic,
                             is_caption=False):
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
        text = raw_text.replace(replace_from, replace_to)
        text = rewrite_plain_urls(text, source_chat, source_topic)
        text = f"{prefix}{text}{suffix}"
        return (text or None) if is_caption else text, None

    shift = utf16_len(prefix)
    new_entities = build_entities(entities, shift, source_chat, source_topic)
    final_text = f"{prefix}{raw_text}{suffix}"

    if is_caption:
        return (final_text or None), (new_entities or None)
    return final_text, (new_entities or None)


# =========================================================
# SENDING
# =========================================================

async def send_text_message(message, target_chat, target_topic, config,
                             source_chat, source_topic):
    if not message.text:
        return None

    final_text, entities = process_text_or_caption(
        message.text, message.entities, config, source_chat, source_topic, is_caption=False
    )
    if not final_text:
        return None

    return await app.send_message(
        chat_id=target_chat,
        text=final_text,
        entities=entities,
        message_thread_id=target_topic or None,
    )


async def send_media_message(message, target_chat, target_topic, config,
                              source_chat, source_topic):
    caption_text, caption_entities = process_text_or_caption(
        message.caption, message.caption_entities, config,
        source_chat, source_topic, is_caption=True,
    )

    kwargs = {
        "message_thread_id": target_topic or None,
        "caption": caption_text,
        "caption_entities": caption_entities,
    }

    if message.photo:
        return await app.send_photo(target_chat, photo=message.photo.file_id, **kwargs)
    if message.video:
        return await app.send_video(target_chat, video=message.video.file_id, **kwargs)
    if message.document:
        return await app.send_document(target_chat, document=message.document.file_id, **kwargs)
    if message.audio:
        return await app.send_audio(target_chat, audio=message.audio.file_id, **kwargs)
    if message.voice:
        return await app.send_voice(target_chat, voice=message.voice.file_id, **kwargs)
    if message.animation:
        return await app.send_animation(target_chat, animation=message.animation.file_id, **kwargs)
    if message.sticker:
        return await app.send_sticker(
            target_chat, sticker=message.sticker.file_id, message_thread_id=target_topic or None
        )

    return None


async def send_one(message, target_chat, target_topic, config, source_chat, source_topic):
    if message.text:
        return await send_text_message(message, target_chat, target_topic, config,
                                        source_chat, source_topic)
    return await send_media_message(message, target_chat, target_topic, config,
                                     source_chat, source_topic)


# =========================================================
# FETCH SOURCE HISTORY
# =========================================================

def _message_topic_id(message):
    for attr in ("message_thread_id", "reply_to_top_message_id"):
        tid = getattr(message, attr, None)
        if tid:
            return tid
    reply_to = getattr(message, "reply_to_message_id", None)
    if reply_to:
        return reply_to
    return None


async def fetch_source_messages(source, src_topic, from_id, to_id):
    await ensure_access(source)

    messages = []
    seen_topic_ids = set()

    try:
        async for message in app.get_chat_history(source):
            if message.id < from_id:
                break
            if message.id > to_id:
                continue

            if src_topic:
                tid = _message_topic_id(message)
                if tid:
                    seen_topic_ids.add(tid)
                if tid != src_topic:
                    continue

            messages.append(message)

    except RPCError as e:
        raise RuntimeError(str(e))

    messages.reverse()

    if src_topic and not messages and seen_topic_ids:
        raise RuntimeError(
            f"Topic ID {src_topic} ka koi message range me nahi mila. "
            f"Is range me ye topic IDs mile: {sorted(seen_topic_ids)}."
        )

    return messages


# =========================================================
# CONTROL COMMANDS — sent from YOUR OWN account only (Saved Messages,
# or any chat). filters.me guarantees no one else can trigger these.
#
# IMPORTANT: each command handler is its own decorated function in its
# own dispatch group, so one handler can never block another from running
# (this was the earlier bug — two handlers sharing a group only let the
# first one fire).
# =========================================================

async def cmd_help(client, message):
    await message.reply_text(
        "🚀 Userbot Transfer Control\n\n"
        "/clone Source Target From_ID To_ID [Src_Topic_ID] [Tgt_Topic_ID]\n\n"
        "/setprefix text          (- to clear)\n"
        "/setsuffix text          (- to clear)\n"
        "/setreplace old | new    (message text; - to clear)\n"
        "/captionmode keep|remove|replace\n"
        "/setcaptionreplace old | new\n"
        "/status\n"
        "/reset\n\n"
        "Ye commands sirf aapke apne account se accept hote hain. Bot "
        "(alag se) sirf /ping aur /uptime dikhata hai, kuch aur nahi."
    )


async def cmd_clone(client, message):
    args = message.command[1:]

    if len(args) < 4:
        await message.reply_text(
            "Usage:\n/clone Source Target From_ID To_ID [Src_Topic_ID] [Tgt_Topic_ID]"
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
        await message.reply_text("❌ IDs must be numbers.")
        return

    status = await message.reply_text("🔎 Source read kar raha hoon...")

    try:
        messages = await fetch_source_messages(source, src_topic, from_id, to_id)
    except Exception as e:
        await status.edit_text(f"❌ Could not read source chat:\n{e}")
        return

    if not messages:
        await status.edit_text(
            "⚠️ Is range/topic me koi message nahi mila.\n"
            "Check karo: Src_Topic_ID sahi hai? Aapka account source ka member hai?"
        )
        return

    try:
        await ensure_access(target)
    except Exception as e:
        await status.edit_text(f"❌ Target access issue:\n{e}")
        return

    success = 0
    failed = 0
    total = len(messages)

    for index, message_item in enumerate(messages, start=1):
        try:
            result = await send_one(message_item, target, tgt_topic, CONFIG, source, src_topic)
            if result:
                save_mapping(source, src_topic, message_item.id, target, tgt_topic, result.id)
                success += 1
            else:
                failed += 1

        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            try:
                result = await send_one(message_item, target, tgt_topic, CONFIG, source, src_topic)
                if result:
                    save_mapping(source, src_topic, message_item.id, target, tgt_topic, result.id)
                    success += 1
                else:
                    failed += 1
            except Exception as retry_error:
                failed += 1
                print(f"Retry failed {message_item.id}: {retry_error}", flush=True)

        except Exception as e:
            failed += 1
            print(f"Transfer error {message_item.id}: {e}", flush=True)
            traceback.print_exc()

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
            except Exception:
                pass

        await asyncio.sleep(0.3)

    try:
        await status.edit_text(
            "🏁 Bulk Transfer Completed\n\n"
            f"✅ Success: `{success}`\n"
            f"❌ Failed: `{failed}`\n"
            f"📦 Total: `{total}`"
        )
    except Exception:
        pass


async def cmd_set_prefix(client, message):
    args = message.command[1:]
    if not args:
        await message.reply_text("Usage:\n/setprefix Your text\n(/setprefix - to clear)")
        return
    value = message.text.split(maxsplit=1)[1]
    CONFIG["prefix"] = "" if value.strip() == "-" else value
    await message.reply_text("✅ Prefix updated.")


async def cmd_set_suffix(client, message):
    args = message.command[1:]
    if not args:
        await message.reply_text("Usage:\n/setsuffix Your text\n(/setsuffix - to clear)")
        return
    value = message.text.split(maxsplit=1)[1]
    CONFIG["suffix"] = "" if value.strip() == "-" else value
    await message.reply_text("✅ Suffix updated.")


async def cmd_set_replace(client, message):
    args = message.command[1:]
    if not args:
        await message.reply_text(
            "Usage:\n/setreplace old | new\n(/setreplace - to clear)\n\n"
            "⚠️ Text replace on hone par us message ki formatting preserve nahi hoti."
        )
        return
    value = message.text.split(maxsplit=1)[1]

    if value.strip() == "-":
        CONFIG["replace_from"] = ""
        CONFIG["replace_to"] = ""
        await message.reply_text("✅ Replacement cleared.")
        return

    if " | " not in value:
        await message.reply_text("Usage:\n/setreplace old | new")
        return

    old, new = value.split(" | ", 1)
    CONFIG["replace_from"] = old.strip()
    CONFIG["replace_to"] = new.strip()
    await message.reply_text("✅ Replacement configured.")


async def cmd_caption_mode(client, message):
    args = message.command[1:]
    if not args or args[0].lower() not in ("keep", "remove", "replace"):
        await message.reply_text("Usage:\n/captionmode keep|remove|replace")
        return
    CONFIG["caption_mode"] = args[0].lower()
    await message.reply_text(f"✅ Caption mode set to: {CONFIG['caption_mode']}")


async def cmd_set_caption_replace(client, message):
    args = message.command[1:]
    if not args:
        await message.reply_text("Usage:\n/setcaptionreplace old | new")
        return
    value = message.text.split(maxsplit=1)[1]

    if " | " not in value:
        await message.reply_text("Usage:\n/setcaptionreplace old | new")
        return

    old, new = value.split(" | ", 1)
    CONFIG["caption_replace_from"] = old.strip()
    CONFIG["caption_replace_to"] = new.strip()
    await message.reply_text("✅ Caption replacement configured.")


async def cmd_status(client, message):
    await message.reply_text(
        "📊 Settings\n\n"
        f"Prefix: {CONFIG['prefix'] or 'None'}\n"
        f"Suffix: {CONFIG['suffix'] or 'None'}\n"
        f"Text Replace: {CONFIG['replace_from'] or 'None'} → {CONFIG['replace_to'] or 'None'}\n"
        f"Caption Mode: {CONFIG['caption_mode']}\n"
        f"Caption Replace: {CONFIG['caption_replace_from'] or 'None'} → "
        f"{CONFIG['caption_replace_to'] or 'None'}"
    )


async def cmd_reset(client, message):
    global CONFIG
    CONFIG = default_config()
    await message.reply_text("🔄 Configuration reset.")


def _wrap(handler):
    """Every command runs in isolation: an error in one handler is caught
    and reported back in Telegram — it can never silently swallow other
    commands or crash the client."""
    async def wrapped(client, message):
        try:
            await handler(client, message)
        except Exception as e:
            print(f"[ERROR] command failed: {e}", flush=True)
            traceback.print_exc()
            try:
                await message.reply_text(f"❌ Error:\n`{e}`")
            except Exception:
                pass
    return wrapped


# Each command gets its OWN handler + OWN dispatch group so none of them
# can ever block one another.
_COMMANDS = [
    ("help", cmd_help),
    ("clone", cmd_clone),
    ("setprefix", cmd_set_prefix),
    ("setsuffix", cmd_set_suffix),
    ("setreplace", cmd_set_replace),
    ("captionmode", cmd_caption_mode),
    ("setcaptionreplace", cmd_set_caption_replace),
    ("status", cmd_status),
    ("reset", cmd_reset),
]

for group_index, (cmd_name, cmd_fn) in enumerate(_COMMANDS):
    app.add_handler(
        MessageHandler(
            _wrap(cmd_fn),
            filters.me & filters.command(cmd_name, prefixes=["/", "."]),
        ),
        group=group_index,
    )


async def _debug_all(client, message):
    print(
        f"[DEBUG] update received | chat={message.chat.id} "
        f"from={getattr(message.from_user, 'id', None)} "
        f"is_self={getattr(message.from_user, 'is_self', None)} "
        f"text={message.text!r}",
        flush=True,
    )


app.add_handler(MessageHandler(_debug_all, filters.all), group=-1000)


# =========================================================
# STATUS BOT — ONLY /ping and /uptime. Fully isolated: if this ever
# crashes (e.g. token conflict), it retries on its own and NEVER touches
# the userbot / your control commands above.
# =========================================================

def _format_uptime():
    delta = int(time.time() - START_TIME)
    days, rem = divmod(delta, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


async def ping_command(update, context):
    start = time.perf_counter()
    msg = await update.effective_message.reply_text("🏓 Pinging...")
    elapsed_ms = (time.perf_counter() - start) * 1000
    await msg.edit_text(f"🏓 Pong! `{elapsed_ms:.0f} ms`")


async def uptime_command(update, context):
    await update.effective_message.reply_text(f"⏱ Uptime: {_format_uptime()}")


# =========================================================
# WEB SERVER (health check for hosting platforms)
# =========================================================

async def home(request):
    return web.Response(text=f"Running.\nUptime: {_format_uptime()}", status=200)


async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", home)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"HTTP server running on port {PORT}", flush=True)


# =========================================================
# MAIN
# =========================================================

def main():
    init_db()

    async def run_status_bot():
        """Isolated /ping /uptime bot. Any failure here (e.g. a stray
        duplicate poller during a redeploy) is contained to this loop and
        retried — it can never affect the userbot control commands."""
        while True:
            ptb_app = Application.builder().token(BOT_TOKEN).build()
            ptb_app.add_handler(CommandHandler("ping", ping_command))
            ptb_app.add_handler(CommandHandler("uptime", uptime_command))
            try:
                await ptb_app.initialize()
                await ptb_app.bot.delete_webhook(drop_pending_updates=True)
                await ptb_app.start()
                await ptb_app.updater.start_polling(drop_pending_updates=True)
                print("Status bot (/ping, /uptime) polling started.", flush=True)

                while ptb_app.updater.running:
                    await asyncio.sleep(5)

            except Exception as e:
                print(f"Status bot error: {e}", flush=True)

            finally:
                try:
                    if ptb_app.updater.running:
                        await ptb_app.updater.stop()
                except Exception:
                    pass
                try:
                    await ptb_app.stop()
                    await ptb_app.shutdown()
                except Exception:
                    pass

            print("Status bot restarting in 15s...", flush=True)
            await asyncio.sleep(15)

    async def run():
        await app.start()
        me = await app.get_me()
        print(f"Userbot session online: {me.first_name} (@{me.username})", flush=True)

        # Populate the peer cache for every chat this account is in. Without
        # this, an in-memory session doesn't know how to resolve incoming
        # updates for channels/groups it hasn't explicitly fetched yet,
        # which is what caused the "Peer id invalid" warnings.
        dialog_count = 0
        async for _ in app.get_dialogs():
            dialog_count += 1
        print(f"Peer cache warmed up: {dialog_count} chats.", flush=True)

        # ---- SELF-TEST: does the update dispatcher actually deliver
        # incoming messages to our handlers at all? This removes any
        # dependency on manually testing via Telegram — it proves or
        # disproves update delivery automatically, right here in the logs.
        self_test_event = asyncio.Event()

        async def _self_test_handler(c, m):
            if m.text == "__SELFTEST__":
                print("[SELFTEST] PASS — update dispatch is working.", flush=True)
                self_test_event.set()

        app.add_handler(MessageHandler(_self_test_handler, filters.me), group=-2000)

        await app.send_message("me", "__SELFTEST__")
        try:
            await asyncio.wait_for(self_test_event.wait(), timeout=20)
            await app.send_message(
                "me",
                "✅ SELFTEST PASSED — update dispatch is working. "
                "Ab /help, /status, /clone jaisi commands yahi (Saved Messages) "
                "me kaam karengi."
            )
        except asyncio.TimeoutError:
            print("[SELFTEST] FAIL — no live updates received.", flush=True)
            try:
                await app.send_message(
                    "me",
                    "❌ SELFTEST FAILED — is session ko live updates nahi mil "
                    "rahe (connection-level issue hai, command/filter ka bug "
                    "nahi). Isliye koi bhi command (/status /clone etc.) "
                    "abhi respond nahi karegi."
                )
            except Exception:
                pass

        await start_web_server()

        # Status bot is a background task — isolated, can't affect the userbot.
        asyncio.create_task(run_status_bot())

        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    finally:
        try:
            asyncio.run(app.stop())
        except Exception:
            pass


if __name__ == "__main__":
    main()
