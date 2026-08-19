import os
import re
import asyncio
import sqlite3

from aiohttp import web

# Python 3.14 + Pyrogram compatibility
asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError


# =========================================================
# ENV
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

PORT = int(os.environ.get("PORT", "10000"))


# =========================================================
# PYROGRAM CLIENT
# =========================================================

app = Client(
    "bulk_manager_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True
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
        "new_chat": ""
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
            PRIMARY KEY(source_chat, source_msg_id)
        )
    """)

    conn.commit()
    conn.close()


def save_mapping(
    source_chat,
    source_msg_id,
    target_chat,
    target_msg_id
):
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        INSERT OR REPLACE INTO mappings
        (source_chat, source_msg_id, target_chat, target_msg_id)
        VALUES (?, ?, ?, ?)
    """, (
        str(source_chat),
        int(source_msg_id),
        str(target_chat),
        int(target_msg_id)
    ))

    conn.commit()
    conn.close()


def get_mapping(source_chat, source_msg_id):
    conn = sqlite3.connect(DB_FILE)

    row = conn.execute("""
        SELECT target_msg_id
        FROM mappings
        WHERE source_chat = ?
        AND source_msg_id = ?
    """, (
        str(source_chat),
        int(source_msg_id)
    )).fetchone()

    conn.close()

    return row[0] if row else None


# =========================================================
# CONTENT PROCESSING
# =========================================================

def process_content(text, entities, config, source_chat):
    if not text:
        return text, entities

    original_text = text

    # -------------------------
    # TEXT REPLACE
    # -------------------------

    if config["replace_from"]:
        text = text.replace(
            config["replace_from"],
            config["replace_to"]
        )

    # -------------------------
    # INTERNAL TELEGRAM LINKS
    # -------------------------

    old_chat = config["old_chat"]
    new_chat = config["new_chat"]

    if old_chat and new_chat:

        pattern = (
            r"https?://(?:t\.me|telegram\.me)/"
            r"(c/\d+|[A-Za-z0-9_]+)/(\d+)"
        )

        def replace_link(match):

            chat_part = match.group(1)
            msg_id = int(match.group(2))

            # Check whether this is the configured source chat
            if chat_part != old_chat:
                return match.group(0)

            mapped_id = get_mapping(
                source_chat,
                msg_id
            )

            if mapped_id is None:
                return match.group(0)

            return f"https://t.me/{new_chat}/{mapped_id}"

        text = re.sub(
            pattern,
            replace_link,
            text
        )

    # -------------------------
    # PREFIX / SUFFIX
    # -------------------------

    prefix = config["prefix"]
    suffix = config["suffix"]

    final_text = (
        prefix +
        text +
        suffix
    )

    # Shift entities because of prefix
    adjusted_entities = []

    if entities:

        for entity in entities:

            try:
                entity.offset += len(prefix)
            except Exception:
                pass

            adjusted_entities.append(entity)

    return final_text, adjusted_entities


# =========================================================
# START
# =========================================================

@app.on_message(filters.command("start"))
async def start_command(client, message):

    print(
        f"START RECEIVED from "
        f"{message.from_user.id if message.from_user else 'unknown'}",
        flush=True
    )

    await message.reply_text(
        "🚀 Advanced Bulk Content Manager Bot Active!\n\n"

        "⚙️ Configuration:\n"
        "/setprefix text\n"
        "/setsuffix text\n"
        "/setreplace old | new\n"
        "/setlink old_chat | new_chat\n"
        "/status\n"
        "/reset\n\n"

        "📦 Bulk Transfer:\n"
        "/clone Source Target From_ID To_ID Src_Topic_ID Tgt_Topic_ID"
    )


# =========================================================
# PREFIX
# =========================================================

@app.on_message(filters.command("setprefix"))
async def set_prefix(client, message):

    if len(message.command) < 2:
        await message.reply_text(
            "Usage:\n/setprefix Your text"
        )
        return

    value = message.text.split(
        maxsplit=1
    )[1]

    get_config(
        message.from_user.id
    )["prefix"] = value

    await message.reply_text(
        f"✅ Prefix set:\n{value}"
    )


# =========================================================
# SUFFIX
# =========================================================

@app.on_message(filters.command("setsuffix"))
async def set_suffix(client, message):

    if len(message.command) < 2:
        await message.reply_text(
            "Usage:\n/setsuffix Your text"
        )
        return

    value = message.text.split(
        maxsplit=1
    )[1]

    get_config(
        message.from_user.id
    )["suffix"] = value

    await message.reply_text(
        f"✅ Suffix set:\n{value}"
    )


# =========================================================
# REPLACE
# =========================================================

@app.on_message(filters.command("setreplace"))
async def set_replace(client, message):

    if len(message.command) < 2:
        await message.reply_text(
            "Usage:\n/setreplace old | new"
        )
        return

    value = message.text.split(
        maxsplit=1
    )[1]

    if " | " not in value:
        await message.reply_text(
            "Usage:\n/setreplace old | new"
        )
        return

    old, new = value.split(
        " | ",
        1
    )

    config = get_config(
        message.from_user.id
    )

    config["replace_from"] = old.strip()
    config["replace_to"] = new.strip()

    await message.reply_text(
        "✅ Replacement rule saved."
    )


# =========================================================
# LINK CONFIG
# =========================================================

@app.on_message(filters.command("setlink"))
async def set_link(client, message):

    if len(message.command) < 2:
        await message.reply_text(
            "Usage:\n/setlink old_chat | new_chat"
        )
        return

    value = message.text.split(
        maxsplit=1
    )[1]

    if " | " not in value:
        await message.reply_text(
            "Usage:\n/setlink old_chat | new_chat"
        )
        return

    old, new = value.split(
        " | ",
        1
    )

    config = get_config(
        message.from_user.id
    )

    config["old_chat"] = old.strip().replace(
        "@",
        ""
    )

    config["new_chat"] = new.strip().replace(
        "@",
        ""
    )

    await message.reply_text(
        "✅ Internal link mapping configured."
    )


# =========================================================
# STATUS
# =========================================================

@app.on_message(filters.command("status"))
async def status(client, message):

    config = get_config(
        message.from_user.id
    )

    await message.reply_text(
        "📊 Current Configuration\n\n"
        f"Prefix: {config['prefix'] or 'None'}\n"
        f"Suffix: {config['suffix'] or 'None'}\n"
        f"Replace: "
        f"{config['replace_from'] or 'None'}"
        " → "
        f"{config['replace_to'] or 'None'}\n"
        f"Old Chat: {config['old_chat'] or 'None'}\n"
        f"New Chat: {config['new_chat'] or 'None'}"
    )


# =========================================================
# RESET
# =========================================================

@app.on_message(filters.command("reset"))
async def reset(client, message):

    USER_CONFIGS[
        message.from_user.id
    ] = default_config()

    await message.reply_text(
        "🔄 Configuration reset."
    )


# =========================================================
# CLONE
# =========================================================

@app.on_message(filters.command("clone"))
async def clone(client, message):

    args = message.command[1:]

    if len(args) < 4:
        await message.reply_text(
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

        await message.reply_text(
            "❌ IDs must be numbers."
        )
        return

    if source.lstrip("-").isdigit():
        source = int(source)

    if target.lstrip("-").isdigit():
        target = int(target)

    config = get_config(
        message.from_user.id
    )

    status_message = await message.reply_text(
        "🚀 Bulk transfer started..."
    )

    success = 0
    failed = 0

    total = max(
        0,
        to_id - from_id + 1
    )

    for current_id in range(
        from_id,
        to_id + 1
    ):

        try:

            # -------------------------
            # PROGRESS
            # -------------------------

            if (
                current_id == from_id
                or
                (current_id - from_id) % 5 == 0
            ):

                completed = (
                    current_id - from_id
                )

                percentage = (
                    completed / total * 100
                    if total
                    else 100
                )

                try:
                    await status_message.edit_text(
                        "⏳ Bulk Transfer\n\n"
                        f"Message: `{current_id}`\n"
                        f"Success: `{success}`\n"
                        f"Failed: `{failed}`\n"
                        f"Progress: `{percentage:.1f}%`"
                    )
                except Exception:
                    pass

            # -------------------------
            # GET MESSAGE
            # -------------------------

            msg = await client.get_messages(
                source,
                current_id
            )

            if not msg or msg.empty:
                failed += 1
                continue

            if getattr(
                msg,
                "service",
                False
            ):
                continue

            # -------------------------
            # TOPIC CHECK
            # -------------------------

            thread_id = getattr(
                msg,
                "message_thread_id",
                None
            )

            if thread_id is None:
                thread_id = getattr(
                    msg,
                    "reply_to_message_id",
                    None
                )

            if (
                src_topic
                and thread_id != src_topic
            ):
                continue

            # -------------------------
            # CONTENT
            # -------------------------

            text = (
                msg.text
                or msg.caption
            )

            entities = (
                msg.entities
                or msg.caption_entities
            )

            new_text, new_entities = process_content(
                text,
                entities,
                config,
                str(source).replace("-100", "")
            )

            # -------------------------
            # TARGET TOPIC
            # -------------------------

            reply_to = (
                tgt_topic
                if tgt_topic
                else None
            )

            # -------------------------
            # SEND
            # -------------------------

            copied = None

            if msg.media:

                copied = await msg.copy(
                    chat_id=target,
                    caption=new_text,
                    caption_entities=new_entities,
                    reply_to_message_id=reply_to
                )

            elif new_text:

                copied = await client.send_message(
                    chat_id=target,
                    text=new_text,
                    entities=new_entities,
                    reply_to_message_id=reply_to
                )

            if copied:

                save_mapping(
                    str(source).replace("-100", ""),
                    current_id,
                    str(target).replace("-100", ""),
                    copied.id
                )

                success += 1

            await asyncio.sleep(1.5)

        except FloodWait as e:

            print(
                f"FloodWait: {e.value}s",
                flush=True
            )

            await asyncio.sleep(
                e.value + 2
            )

        except RPCError as e:

            failed += 1

            print(
                f"Telegram RPC error "
                f"{current_id}: {e}",
                flush=True
            )

        except Exception as e:

            failed += 1

            print(
                f"Error {current_id}: {e}",
                flush=True
            )

    await status_message.edit_text(
        "🏁 Bulk Transfer Completed\n\n"
        f"✅ Success: `{success}`\n"
        f"❌ Failed: `{failed}`\n"
        f"📦 Total: `{total}`"
    )


# =========================================================
# RENDER WEB SERVER
# =========================================================

async def health(request):

    return web.Response(
        text="Bulk Manager Bot is running.",
        status=200
    )


async def start_web_server():

    web_app = web.Application()

    web_app.router.add_get(
        "/",
        health
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
        f"Web server running on port {PORT}",
        flush=True
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    init_db()

    print(
        "Starting Pyrogram...",
        flush=True
    )

    await app.start()

    me = await app.get_me()

    print(
        f"Bot started: @{me.username}",
        flush=True
    )

    await start_web_server()

    print(
        "Bot is listening for Telegram messages.",
        flush=True
    )

    try:

        await asyncio.Event().wait()

    finally:

        await app.stop()


if __name__ == "__main__":

    asyncio.run(main())
