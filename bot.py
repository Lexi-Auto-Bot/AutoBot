import os
import time
import uuid
import hashlib
import hmac
import logging
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, request
from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("joinbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "joinbot").strip() or "joinbot"
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
ADMIN_IDS = set()
for key in ("ADMIN_IDS", "ADMIN_IDS_2", "ADMIN_IDS_3", "ADMIN_ID"):
    for value in os.getenv(key, "").replace(";", ",").split(","):
        try:
            if value.strip():
                ADMIN_IDS.add(int(value.strip()))
        except ValueError:
            log.warning("Ignoring invalid admin ID in %s", key)

app = Flask(__name__)
http = requests.Session()
mongo_client = None
db = None
mongo_ready = False
try:
    if MONGODB_URI:
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
        mongo_client.admin.command("ping")
        db = mongo_client[MONGODB_DB]
        # Only the Telegram user ID (_id) and the retention timestamp are stored.
        # MongoDB's TTL monitor expires records after 365 days; no app cleanup loop.
        db.users.create_index("created_at", expireAfterSeconds=365 * 24 * 60 * 60, name="user_id_retention_365d")
        mongo_ready = True
        log.info("MongoDB connected; database=%s", MONGODB_DB)
    else:
        log.error("MONGODB_URI is missing")
except Exception:
    log.exception("MongoDB connection/index setup failed")

started_at = time.time()
pending_setup = {}
pending_broadcast = set()
active_chat_actions = {}
active_chat_actions_lock = threading.Lock()
MEDIA_FIELDS = ("photo", "video", "document", "animation", "audio", "voice", "video_note", "sticker")
SEND_METHODS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "document": ("sendDocument", "document"),
    "animation": ("sendAnimation", "animation"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendVoice", "voice"),
    "video_note": ("sendVideoNote", "video_note"),
    "sticker": ("sendSticker", "sticker"),
}
WEBHOOK_PATH = hashlib.sha256(("joinbot-webhook:" + BOT_TOKEN).encode()).hexdigest()[:40] if BOT_TOKEN else "not-configured"
WEBHOOK_SECRET = hashlib.sha256(("joinbot-secret:" + BOT_TOKEN).encode()).hexdigest() if BOT_TOKEN else ""


def tg(method, payload=None, timeout=30):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN is not configured"}
    try:
        response = http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        result = response.json()
        # Telegram clears chat actions when the bot sends a message/media reply.
        if result.get("ok") and method in {
            "sendMessage", "sendPhoto", "sendVideo", "sendDocument", "sendAnimation",
            "sendAudio", "sendVoice", "sendVideoNote", "sendSticker", "copyMessage",
        }:
            chat_id = (payload or {}).get("chat_id")
            with active_chat_actions_lock:
                stop_event = active_chat_actions.get(int(chat_id)) if chat_id is not None else None
            if stop_event:
                stop_event.set()
        return result
    except Exception:
        log.exception("Telegram API call failed: %s", method)
        return {"ok": False, "description": "Telegram API request failed"}



def send_owner_action(owner_chat_id, action="typing"):
    """Show Telegram's native bot status in the owner's private chat."""
    allowed = {
        "typing", "upload_photo", "record_video", "upload_video",
        "record_voice", "upload_voice", "upload_document", "choose_sticker",
        "find_location", "record_video_note", "upload_video_note", "watch_video",
    }
    if action not in allowed:
        action = "typing"
    return tg("sendChatAction", {"chat_id": int(owner_chat_id), "action": action}, timeout=8)


def start_chat_action(chat_id, action="typing"):
    """Keep Telegram's native status visible until the bot's reply is sent."""
    chat_id = int(chat_id)
    stop_event = threading.Event()
    with active_chat_actions_lock:
        previous = active_chat_actions.get(chat_id)
        if previous:
            previous.set()
        active_chat_actions[chat_id] = stop_event

    def refresh():
        try:
            send_owner_action(chat_id, action)
            # Telegram chat actions expire after about five seconds; refresh
            # every four seconds until tg() observes the outgoing reply.
            while not stop_event.wait(4):
                send_owner_action(chat_id, action)
        finally:
            with active_chat_actions_lock:
                if active_chat_actions.get(chat_id) is stop_event:
                    active_chat_actions.pop(chat_id, None)

    threading.Thread(target=refresh, daemon=True).start()


def action_for_message(message):
    """Choose a native Telegram chat action matching the incoming content type."""
    if message.get("photo"):
        return "upload_photo"
    if message.get("video") or message.get("animation"):
        return "upload_video"
    if message.get("document"):
        return "upload_document"
    if message.get("voice"):
        return "upload_voice"
    if message.get("audio"):
        return "upload_document"
    if message.get("video_note"):
        return "upload_video_note"
    if message.get("sticker"):
        return "choose_sticker"
    return "typing"


def inc(field, amount=1):
    if mongo_ready:
        try:
            db.stats.update_one(
                {"_id": "global"},
                {"$inc": {field: amount}, "$set": {"updated_at": datetime.utcnow()}},
                upsert=True,
            )
        except Exception:
            log.exception("Stats update failed")


def remember_user_id(user_id):
    """Store no profile fields: only _id=user ID plus created_at for 365-day TTL."""
    if not mongo_ready or not user_id:
        return
    now = datetime.utcnow()
    try:
        db.users.delete_one({"_id": int(user_id), "created_at": {"$lte": now - timedelta(days=365)}})
        db.users.update_one(
            {"_id": int(user_id)},
            {"$setOnInsert": {"created_at": now}},
            upsert=True,
        )
    except Exception:
        log.exception("Could not save user ID")


def ui_card(title, body, icon="✦"):
    """Consistent AutoBot dark-neon-inspired Telegram message layout."""
    return (
        f"{icon} <b>AUTOBOT  /  {title.upper()}</b>\n"
        "<i>━━━━━━━━━━━━━━━━━━━━</i>\n\n"
        f"{body}\n\n"
        "<i>━━━━━━━━━━━━━━━━━━━━</i>\n"
        "<i>⚡ Smart • Fast • Reliable</i>"
    )


def bot_mention():
    result = tg("getMe", timeout=8)
    if result.get("ok"):
        bot = result.get("result", {})
        return f'<a href="https://t.me/{bot.get("username", "")}">{bot.get("first_name", "JoinBot")}</a>'
    return "JoinBot"


def render_text(value, user=None, chat=None):
    import html
    user = user or {}
    chat = chat or {}
    name = " ".join(x for x in (user.get("first_name", ""), user.get("last_name", "")) if x).strip() or "there"
    mention = f'<a href="tg://user?id={int(user.get("id", 0))}">{html.escape(name)}</a>'
    chat_name = html.escape(str(chat.get("title") or chat.get("username") or "this chat"))
    rendered = str(value or "").replace("{mention}", mention).replace("{chat}", chat_name)
    # Avoid an unnecessary Telegram getMe network request on every /start.
    if "{bot_mention}" in rendered:
        rendered = rendered.replace("{bot_mention}", bot_mention())
    return rendered


def save_message(kind, message):
    if not mongo_ready:
        return False
    item = message.get(kind) if kind in MEDIA_FIELDS else None
    if kind == "photo":
        photos = message.get("photo") or []
        item = photos[-1] if photos else {}
    record = {
        "_id": "start_message" if pending_setup.get(int(message["from"]["id"])) == "start" else "join_message",
        "kind": kind,
        "text": message.get("text"),
        "caption": message.get("caption"),
        "entities": message.get("entities"),
        "caption_entities": message.get("caption_entities"),
        "reply_markup": message.get("reply_markup"),
        "source_chat_id": message.get("chat", {}).get("id"),
        "source_message_id": message.get("message_id"),
        "file_id": (item or {}).get("file_id") if item else None,
        "saved_at": datetime.utcnow(),
    }
    try:
        db.settings.replace_one({"_id": record["_id"]}, record, upsert=True)
        return True
    except Exception:
        log.exception("Could not save message")
        return False


def send_saved(record, destination, user=None, chat=None):
    if not record:
        return None
    template = record.get("text") if record.get("kind") == "text" else record.get("caption")
    has_vars = any(x in str(template or "") for x in ("{mention}", "{chat}", "{bot_mention}"))
    if not has_vars and record.get("source_chat_id") and record.get("source_message_id"):
        return tg("copyMessage", {
            "chat_id": destination,
            "from_chat_id": record["source_chat_id"],
            "message_id": record["source_message_id"],
        })
    kind = record.get("kind", "text")
    if kind == "text":
        payload = {"chat_id": destination, "text": render_text(record.get("text"), user, chat), "parse_mode": "HTML"}
        return tg("sendMessage", payload)
    method, media_key = SEND_METHODS.get(kind, (None, None))
    if not method or not record.get("file_id"):
        return {"ok": False, "description": "Saved media is unavailable"}
    payload = {"chat_id": destination, media_key: record["file_id"]}
    if record.get("caption"):
        payload.update({"caption": render_text(record["caption"], user, chat), "parse_mode": "HTML"})
    return tg(method, payload)


def is_admin(user_id):
    return int(user_id or 0) in ADMIN_IDS


def broadcast_message(source_chat_id, source_message_id, reply_markup=None):
    """Copy the exact source Telegram message; broadcast payload is never persisted."""
    if not mongo_ready:
        return 0, 0
    sent = failed = 0
    user_ids = [doc["_id"] for doc in db.users.find({}, {"_id": 1})]
    for user_id in user_ids:
        payload = {
            "chat_id": int(user_id),
            "from_chat_id": int(source_chat_id),
            "message_id": int(source_message_id),
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = tg("copyMessage", payload, timeout=20)
        if result.get("ok"):
            sent += 1
        else:
            failed += 1
            description = str(result.get("description", ""))
            if "bot was blocked" in description.lower() or "user is deactivated" in description.lower():
                try:
                    db.users.delete_one({"_id": int(user_id)})
                except Exception:
                    log.exception("Could not remove unreachable user ID")
            log.warning("Broadcast copy failed for user_id=%s: %s", user_id, description)
            parameters = result.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            if retry_after:
                time.sleep(min(int(retry_after), 30))
                retry = tg("copyMessage", payload, timeout=20)
                if retry.get("ok"):
                    sent += 1
                    failed -= 1
        # Keep within Telegram's practical per-chat broadcast throughput.
        time.sleep(0.05)
    inc("broadcasts")
    inc("broadcast_delivered", sent)
    inc("broadcast_failed", failed)
    return sent, failed


def handle_message(message):
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    uid = int(user.get("id") or 0)
    chat_id = int(chat.get("id") or 0)
    if chat.get("type") != "private" or not uid:
        return
    text = str(message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    # Start a status only for updates this bot will handle; keep it refreshed
    # until Telegram accepts the corresponding reply.
    admin_commands = {"/broadcast", "/cancel", "/setstart", "/joinmessage", "/default", "/empty", "/stats"}
    if command == "/start" or (is_admin(uid) and (
        command in admin_commands or uid in pending_setup or uid in pending_broadcast
    )):
        start_chat_action(chat_id, action_for_message(message))

    if command == "/start":
        # Send the welcome before optional owner status and database writes.
        saved = db.settings.find_one({"_id": "start_message"}) if mongo_ready else None
        if saved:
            send_saved(saved, chat_id, user)
        else:
            welcome = ui_card(
                "Welcome",
                f"Hey {render_text('{mention}', user)} 👋\n\n"
                "I automatically approve channel join requests.\n\n"
                "➜ Add me as a channel admin\n"
                "➜ Grant permission to invite users\n"
                "➜ I'll handle incoming join requests for you.",
                "🤖",
            )
            tg("sendMessage", {"chat_id": chat_id, "text": welcome, "parse_mode": "HTML"})
        inc("starts")
        remember_user_id(uid)
        return

    remember_user_id(uid)
    if not is_admin(uid):
        return

    if command == "/broadcast":
        pending_broadcast.add(uid)
        pending_setup.pop(uid, None)
        tg("sendMessage", {
            "chat_id": chat_id,
            "text": ui_card(
                "Broadcast Studio",
                "📡 <b>Ready to broadcast</b>\n\nSend your text, photo, video, GIF, document or a message with buttons. I'll copy the original message to saved users.\n\n🛑 Send /cancel anytime to stop setup.",
                "📣",
            ),
            "parse_mode": "HTML",
        })
        return

    if command == "/cancel":
        pending_setup.pop(uid, None)
        was_broadcast = uid in pending_broadcast
        pending_broadcast.discard(uid)
        tg("sendMessage", {
            "chat_id": chat_id,
            "text": ui_card("Cancelled", "Broadcast cancelled." if was_broadcast else "Setup cancelled. No changes were made.", "🛑"),
            "parse_mode": "HTML",
        })
        return

    if uid in pending_broadcast:
        if command:
            tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Waiting for message", "Send the message you want to broadcast, or /cancel.", "⏳"), "parse_mode": "HTML"})
            return
        pending_broadcast.discard(uid)
        tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Broadcast Started", "Your message is being delivered to saved users. This may take a little time.", "🚀"), "parse_mode": "HTML"})
        sent, failed = broadcast_message(
            chat_id,
            int(message.get("message_id") or 0),
            message.get("reply_markup"),
        )
        tg("sendMessage", {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": ui_card("Broadcast Report", f"🟢 <b>Delivered:</b> {sent:,}\n🔴 <b>Failed:</b> {failed:,}\n\n📊 <b>Attempted:</b> {sent + failed:,}", "📣"),
        })
        return

    if command in ("/setstart", "/joinmessage"):
        pending_setup[uid] = "start" if command == "/setstart" else "join"
        label = "start message" if command == "/setstart" else "join message"
        tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Message Setup", f"📝 Send your {label} now.\n\nText and supported media are accepted.\nUse /cancel to leave without saving.", "⚙️"), "parse_mode": "HTML"})
        return

    if command == "/default":
        if mongo_ready:
            db.settings.delete_one({"_id": "start_message"})
        pending_setup.pop(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Welcome Reset", "The built-in /start message has been restored.", "🔄"), "parse_mode": "HTML"})
        return

    if command == "/empty":
        if mongo_ready:
            db.settings.delete_one({"_id": "join_message"})
        pending_setup.pop(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Join Message Disabled", "The saved post after join approval is now disabled.", "🧹"), "parse_mode": "HTML"})
        return

    if command == "/stats":
        stats = db.stats.find_one({"_id": "global"}) if mongo_ready else {}
        users_count = db.users.count_documents({}) if mongo_ready else 0
        uptime = max(0, int(time.time() - started_at))
        days, rem = divmod(uptime, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        tg("sendMessage", {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": ui_card(
                "Live Analytics",
                "🧩 <b>SYSTEM</b>\n"
                f"🗄️ Database: <b>{'🟢 Connected' if mongo_ready else '🔴 Offline'}</b>\n"
                f"👥 Saved users: <b>{users_count:,}</b>\n"
                f"⏱ Uptime: <b>{days}d {hours}h {minutes}m</b>\n\n"
                "📨 <b>JOIN REQUESTS</b>\n"
                f"📥 Received: <b>{(stats or {}).get('requests', 0):,}</b>\n"
                f"✅ Approved: <b>{(stats or {}).get('approved', 0):,}</b>\n"
                f"⚠️ Failed: <b>{(stats or {}).get('failed', 0):,}</b>\n\n"
                "⚡ <b>BOT ACTIVITY</b>\n"
                f"✉️ Starts: <b>{(stats or {}).get('starts', 0):,}</b>\n"
                f"📣 Broadcasts: <b>{(stats or {}).get('broadcasts', 0):,}</b>\n"
                f"📤 Delivered: <b>{(stats or {}).get('broadcast_delivered', 0):,}</b>\n"
                f"📭 Failed deliveries: <b>{(stats or {}).get('broadcast_failed', 0):,}</b>",
                "📊",
            ),
        })
        return

    if uid in pending_setup:
        kind = next((k for k in MEDIA_FIELDS if message.get(k)), "text" if message.get("text") is not None else None)
        if not kind:
            tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Unsupported Message", "Please send text or supported media, or /cancel.", "⚠️"), "parse_mode": "HTML"})
            return
        if save_message(kind, message):
            pending_setup.pop(uid, None)
            tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Saved Successfully", "Your message has been saved in the database.", "✅"), "parse_mode": "HTML"})
        else:
            tg("sendMessage", {"chat_id": chat_id, "text": ui_card("Save Failed", "The message could not be saved. Check the database connection and try again.", "⚠️"), "parse_mode": "HTML"})


def handle_join(update):
    join = update.get("chat_join_request") or {}
    chat = join.get("chat") or {}
    user = join.get("from") or {}
    chat_id = int(chat.get("id") or 0)
    user_id = int(user.get("id") or 0)
    if not chat_id or not user_id:
        return
    inc("requests")
    result = tg("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id})
    if not result.get("ok"):
        desc = str(result.get("description") or "").lower()
        if "already" not in desc:
            inc("failed")
            log.error("Join request approval failed: %s", result.get("description"))
        return
    inc("approved")
    saved = db.settings.find_one({"_id": "join_message"}) if mongo_ready else None
    user_chat_id = int(join.get("user_chat_id") or 0)
    if saved and user_chat_id:
        start_chat_action(user_chat_id, "typing")
        send_saved(saved, user_chat_id, user, chat)


def process_update(update):
    if update.get("chat_join_request"):
        handle_join(update)
    elif update.get("message"):
        handle_message(update["message"])


@app.post(f"/telegram/{WEBHOOK_PATH}")
def telegram_webhook():
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
        return jsonify({"ok": False}), 403
    update = request.get_json(silent=True) or {}
    try:
        process_update(update)
    except Exception:
        log.exception("Webhook update processing failed")
    return jsonify({"ok": True})


def start_bot():
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing; bot webhook disabled")
        return
    if not ADMIN_IDS:
        log.warning("No admin IDs configured; admin commands will be unavailable")
    public_commands = [{"command": "start", "description": "Start the bot"}]
    tg("setMyCommands", {"commands": public_commands})
    for admin_id in ADMIN_IDS:
        tg("setMyCommands", {
            "commands": public_commands + [
                {"command": "setstart", "description": "Set welcome message"},
                {"command": "default", "description": "Restore welcome message"},
                {"command": "joinmessage", "description": "Set message after approval"},
                {"command": "empty", "description": "Disable join message"},
                {"command": "broadcast", "description": "Broadcast a copied message"},
                {"command": "stats", "description": "View bot statistics"},
                {"command": "cancel", "description": "Cancel setup or broadcast"},
            ],
            "scope": {"type": "chat", "chat_id": admin_id},
        })
    if not PUBLIC_URL:
        log.error("RENDER_EXTERNAL_URL is missing; cannot configure Telegram webhook")
        return
    webhook_url = f"{PUBLIC_URL}/telegram/{WEBHOOK_PATH}"
    result = tg("setWebhook", {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["message", "chat_join_request"],
        "drop_pending_updates": False,
    })
    if result.get("ok"):
        log.info("Telegram webhook configured; background polling is disabled")
    else:
        log.error("Telegram webhook setup failed: %s", result.get("description"))


@app.get("/")
@app.get("/health")
def health():
    return jsonify({
        "ok": bool(BOT_TOKEN and mongo_ready and ADMIN_IDS),
        "system": "JoinBot",
        "database": MONGODB_DB,
        "database_connected": mongo_ready,
        "admin_configured": bool(ADMIN_IDS),
        "auto_approval": True,
        "update_mode": "webhook",
        "background_poller": False,
        "user_id_retention_days": 365,
    })


if __name__ == "__main__":
    start_bot()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
