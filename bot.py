import os
import re
import asyncio
import sqlite3

from aiohttp import web

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError


# =========================================================
# ENV
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

PORT = int(os.environ.get("PORT", "10000"))

DB_FILE = "bulk_manager.db"


# =========================================================
# PYROGRAM
# =========================================================

app = Client(
    "bulk_manager_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
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
        "caption": None,
        "caption_mode": "keep",
    }


def get_config(user_id):
    if user_id not in USER_CONFIGS:
        USER_CONFIGS[user_id] = default_config()

    return USER_CONFIGS[user_id]


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(DB_FILE)


def init_db():

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS message_map (
            source_chat TEXT NOT NULL,
            source_message INTEGER NOT NULL,
            target_chat TEXT NOT NULL,
            target_message INTEGER NOT NULL,
            PRIMARY KEY(source_chat, source_message)
        )
    """)

    conn.commit()
    conn.close()


def save_mapping(
    source_chat,
    source_message,
    target_chat,
    target_message
):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT OR REPLACE INTO message_map
        (
            source_chat,
            source_message,
            target_chat,
            target_message
        )
        VALUES (?, ?, ?, ?)
    """, (
        str(source_chat),
        int(source_message),
        str(target_chat),
        int(target_message)
    ))

    conn.commit()
    conn.close()


def get_mapping(source_chat, source_message):

    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT target_message
        FROM message_map
        WHERE source_chat = ?
        AND source_message = ?
    """, (
        str(source_chat),
        int(source_message)
    ))

    row = cur.fetchone()

    conn.close()

    return row[0] if row else None


# =========================================================
# TELEGRAM INTERNAL LINK
# =========================================================

def normalize_chat_id(chat_id):
    """
    Converts:
    -1001234567890
    into:
    1234567890
    """

    value = str(chat_id)

    if value.startswith("-100"):
        return value[4:]

    return value


def build_message_link(chat_id, message_id):

    internal_id = normalize_chat_id(chat_id)

    return f"https://t.me/c/{internal_id}/{message_id}"


# =========================================================
# LINK REPLACEMENT
# =========================================================

INTERNAL_LINK_RE = re.compile(
    r"https?://t\.me/c/(\d+)/(\d+)"
)


def replace_internal_links(
    text,
    source_chat,
    target_chat
):

    if not text:
        return text

    def replacer(match):

        linked_chat = match.group(1)
        linked_message = int(match.group(2))

        source_chat_clean = normalize_chat_id(source_chat)

        if linked_chat != source_chat_clean:
            return match.group(0)

        target_message = get_mapping(
            source_chat,
            linked_message
        )

        if not target_message:
            return match.group(0)

        return build_message_link(
            target_chat,
            target_message
        )

    return INTERNAL_LINK_RE.sub(
        replacer,
        text
    )


# =========================================================
# TEXT PROCESSING
# =========================================================

def process_text(
    text,
    config,
    source_chat,
    target_chat
):

    if text is None:
        return None

    # Replace text
    if config["replace_from"]:
        text = text.replace(
            config["replace_from"],
            config["replace_to"]
        )

    # Internal Telegram links
    text = replace_internal_links(
        text,
        source_chat,
        target_chat
    )

    # Prefix
    if config["prefix"]:
        text = config["prefix"] + text

    # Suffix
    if config["suffix"]:
        text = text + config["suffix"]

    return text


# =========================================================
# START
# =========================================================

@app.on_message(filters.command("start"))
async def start(_, message):

    await message.reply_text(
        "🚀 **Bulk Manager Bot**\n\n"

        "**Bulk Transfer**\n"
        "`/clone source target from_id to_id src_topic target_topic`\n\n"

        "**Caption**\n"
        "`/setprefix text`\n"
        "`/setsuffix text`\n"
        "`/setcaption text`\n"
        "`/removecaption`\n"
        "`/keepcaption`\n\n"

        "**Text**\n"
        "`/setreplace old | new`\n\n"

        "**Other**\n"
        "`/status`\n"
        "`/reset`\n\n"

        "Example:\n"
        "`/clone -1001111111111 -1002222222222 100 500 123 456`"
    )


# =========================================================
# PREFIX
# =========================================================

@app.on_message(filters.command("setprefix"))
async def setprefix(_, message):

    if len(message.command) < 2:

        await message.reply_text(
            "Usage:\n`/setprefix Your Text`"
        )

        return

    config = get_config(
        message.from_user.id
    )

    config["prefix"] = message.text.split(
        maxsplit=1
    )[1]

    await message.reply_text(
        "✅ Prefix updated."
    )


# =========================================================
# SUFFIX
# =========================================================

@app.on_message(filters.command("setsuffix"))
async def setsuffix(_, message):

    if len(message.command) < 2:

        await message.reply_text(
            "Usage:\n`/setsuffix Your Text`"
        )

        return

    config = get_config(
        message.from_user.id
    )

    config["suffix"] = message.text.split(
        maxsplit=1
    )[1]

    await message.reply_text(
        "✅ Suffix updated."
    )


# =========================================================
# SET CAPTION
# =========================================================

@app.on_message(filters.command("setcaption"))
async def setcaption(_, message):

    if len(message.command) < 2:

        await message.reply_text(
            "Usage:\n`/setcaption New Caption`"
        )

        return

    config = get_config(
        message.from_user.id
    )

    config["caption"] = message.text.split(
        maxsplit=1
    )[1]

    config["caption_mode"] = "replace"

    await message.reply_text(
        "✅ Caption replacement enabled."
    )


# =========================================================
# REMOVE CAPTION
# =========================================================

@app.on_message(filters.command("removecaption"))
async def removecaption(_, message):

    config = get_config(
        message.from_user.id
    )

    config["caption"] = ""
    config["caption_mode"] = "remove"

    await message.reply_text(
        "✅ Captions will be removed."
    )


# =========================================================
# KEEP CAPTION
# =========================================================

@app.on_message(filters.command("keepcaption"))
async def keepcaption(_, message):

    config = get_config(
        message.from_user.id
    )

    config["caption"] = None
    config["caption_mode"] = "keep"

    await message.reply_text(
        "✅ Original captions will be kept."
    )


# =========================================================
# REPLACE TEXT
# =========================================================

@app.on_message(filters.command("setreplace"))
async def setreplace(_, message):

    if len(message.command) < 2:

        await message.reply_text(
            "Usage:\n"
            "`/setreplace old text | new text`"
        )

        return

    value = message.text.split(
        maxsplit=1
    )[1]

    if " | " not in value:

        await message.reply_text(
            "❌ Use:\n"
            "`/setreplace old | new`"
        )

        return

    old, new = value.split(
        " | ",
        1
    )

    config = get_config(
        message.from_user.id
    )

    config["replace_from"] = old
    config["replace_to"] = new

    await message.reply_text(
        "✅ Text replacement enabled."
    )


# =========================================================
# STATUS
# =========================================================

@app.on_message(filters.command("status"))
async def status(_, message):

    config = get_config(
        message.from_user.id
    )

    caption_mode = config["caption_mode"]

    await message.reply_text(
        "📊 **Current Settings**\n\n"
        f"Prefix: `{config['prefix'] or 'None'}`\n"
        f"Suffix: `{config['suffix'] or 'None'}`\n"
        f"Caption mode: `{caption_mode}`\n"
        f"Caption: `{config['caption'] or 'None'}`\n"
        f"Replace: `{config['replace_from'] or 'None'}`"
    )


# =========================================================
# RESET
# =========================================================

@app.on_message(filters.command("reset"))
async def reset(_, message):

    USER_CONFIGS[
        message.from_user.id
    ] = default_config()

    await message.reply_text(
        "🔄 Settings reset."
    )


# =========================================================
# GET MESSAGE TEXT/CAPTION
# =========================================================

def get_content(message):

    if message.text:
        return message.text

    if message.caption:
        return message.caption

    return None


# =========================================================
# BULK CLONE
# =========================================================

@app.on_message(filters.command("clone"))
async def clone(client, message):

    args = message.command[1:]

    if len(args) < 4:

        await message.reply_text(
            "❌ **Usage:**\n\n"
            "`/clone source target from_id to_id "
            "src_topic target_topic`"
        )

        return

    source = args[0]
    target = args[1]

    try:

        from_id = int(args[2])
        to_id = int(args[3])

        src_topic = (
            int(args[4])
            if len(args) >= 5
            else 0
        )

        target_topic = (
            int(args[5])
            if len(args) >= 6
            else 0
        )

    except ValueError:

        await message.reply_text(
            "❌ IDs must be numbers."
        )

        return

    # Convert numeric chat IDs
    if source.lstrip("-").isdigit():
        source = int(source)

    if target.lstrip("-").isdigit():
        target = int(target)

    if from_id > to_id:

        await message.reply_text(
            "❌ From ID must be smaller than To ID."
        )

        return

    config = get_config(
        message.from_user.id
    )

    total = to_id - from_id + 1

    success = 0
    failed = 0
    skipped = 0

    status = await message.reply_text(
        "🚀 **Bulk export started...**"
    )

    # =====================================================
    # FIRST PASS
    # Copy messages and save mapping
    # =====================================================

    for index, msg_id in enumerate(
        range(from_id, to_id + 1),
        start=1
    ):

        try:

            msg = await client.get_messages(
                source,
                msg_id
            )

            if not msg or msg.empty:

                skipped += 1
                continue

            # ---------------------------------------------
            # Topic check
            # ---------------------------------------------

            if src_topic:

                thread_id = (
                    getattr(
                        msg,
                        "message_thread_id",
                        None
                    )
                    or getattr(
                        msg,
                        "reply_to_top_message_id",
                        None
                    )
                    or getattr(
                        msg,
                        "reply_to_message_id",
                        None
                    )
                )

                if thread_id != src_topic:

                    skipped += 1
                    continue

            # ---------------------------------------------
            # COPY
            # ---------------------------------------------

            copied = None

            # We first copy the original message.
            # This preserves Telegram formatting.
            if msg.media:

                copied = await msg.copy(
                    chat_id=target,
                    reply_to_message_id=(
                        target_topic
                        if target_topic
                        else None
                    )
                )

            elif msg.text:

                copied = await client.send_message(
                    chat_id=target,
                    text=msg.text,
                    entities=msg.entities,
                    reply_to_message_id=(
                        target_topic
                        if target_topic
                        else None
                    )
                )

            else:

                skipped += 1
                continue

            if copied:

                save_mapping(
                    source,
                    msg.id,
                    target,
                    copied.id
                )

                success += 1

        except FloodWait as e:

            await asyncio.sleep(
                e.value + 2
            )

            try:

                msg = await client.get_messages(
                    source,
                    msg_id
                )

                if msg and not msg.empty:

                    copied = await msg.copy(
                        chat_id=target,
                        reply_to_message_id=(
                            target_topic
                            if target_topic
                            else None
                        )
                    )

                    if copied:

                        save_mapping(
                            source,
                            msg.id,
                            target,
                            copied.id
                        )

                        success += 1

            except Exception:
                failed += 1

        except Exception as e:

            failed += 1

            print(
                f"Clone error {msg_id}: {e}"
            )

        # ---------------------------------------------
        # PROGRESS
        # ---------------------------------------------

        if (
            index % 5 == 0
            or index == total
        ):

            percent = (
                index / total
            ) * 100

            try:

                await status.edit_text(
                    "📦 **Bulk Transfer**\n\n"
                    f"Progress: `{percent:.1f}%`\n"
                    f"Processed: `{index}/{total}`\n\n"
                    f"✅ Copied: `{success}`\n"
                    f"⏭ Skipped: `{skipped}`\n"
                    f"❌ Failed: `{failed}`"
                )

            except Exception:
                pass

    # =====================================================
    # SECOND PASS
    # Fix captions/text/internal links
    # =====================================================

    await status.edit_text(
        "🔗 **Updating captions and internal links...**"
    )

    for msg_id in range(
        from_id,
        to_id + 1
    ):

        try:

            target_id = get_mapping(
                source,
                msg_id
            )

            if not target_id:
                continue

            source_msg = await client.get_messages(
                source,
                msg_id
            )

            target_msg = await client.get_messages(
                target,
                target_id
            )

            if not source_msg or not target_msg:
                continue

            original = get_content(
                source_msg
            )

            if original is None:
                continue

            new_content = process_text(
                original,
                config,
                source,
                target
            )

            # Caption modification
            if source_msg.media:

                if config["caption_mode"] == "remove":

                    new_content = ""

                elif config["caption_mode"] == "replace":

                    new_content = config["caption"] or ""

                    new_content = process_text(
                        new_content,
                        config,
                        source,
                        target
                    )

                if new_content != original:

                    try:

                        await client.edit_message_caption(
                            target,
                            target_id,
                            caption=new_content
                        )

                    except Exception as e:

                        print(
                            f"Caption edit failed "
                            f"{target_id}: {e}"
                        )

            # Text message modification
            else:

                if new_content != original:

                    try:

                        await client.edit_message_text(
                            target,
                            target_id,
                            new_content
                        )

                    except Exception as e:

                        print(
                            f"Text edit failed "
                            f"{target_id}: {e}"
                        )

        except Exception as e:

            print(
                f"Post-process error "
                f"{msg_id}: {e}"
            )

    # =====================================================
    # FINISHED
    # =====================================================

    await status.edit_text(
        "🏁 **Bulk Export Completed**\n\n"
        f"📦 Total: `{total}`\n"
        f"✅ Copied: `{success}`\n"
        f"⏭ Skipped: `{skipped}`\n"
        f"❌ Failed: `{failed}`\n\n"
        "🔗 Internal links processed."
    )


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
        f"Web server running on port {PORT}"
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    init_db()

    await app.start()

    await start_web_server()

    print(
        "🚀 Bulk Manager Bot started."
    )

    await asyncio.Event().wait()


if __name__ == "__main__":

    asyncio.run(main())
