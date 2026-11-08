import os
import time
import uuid
import logging
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify
from pymongo import MongoClient, ReturnDocument

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("joinbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "joinbot").strip() or "joinbot"
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
        db.settings.create_index("_id", unique=True)
        db.stats.create_index("_id", unique=True)
        mongo_ready = True
        log.info("MongoDB connected; database=%s", MONGODB_DB)
    else:
        log.error("MONGODB_URI is missing")
except Exception:
    log.exception("MongoDB connection failed")

started_at = time.time()
offset = 0
pending = {}
poller_owner = f"{os.getenv('RENDER_INSTANCE_ID', 'local')}:{uuid.uuid4().hex}"
poller_started = False
poller_lock = threading.Lock()
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

def tg(method, payload=None, timeout=35):
    if not BOT_TOKEN:
        return {"ok": False, "description": "BOT_TOKEN is not configured"}
    try:
        response = http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        return response.json()
    except Exception:
        log.exception("Telegram API call failed: %s", method)
        return {"ok": False, "description": "Telegram API request failed"}

def inc(field):
    if mongo_ready:
        try:
            db.stats.update_one(
                {"_id": "global"},
                {"$inc": {field: 1}, "$set": {"updated_at": datetime.utcnow()}},
                upsert=True,
            )
        except Exception:
            log.exception("Stats update failed")

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
    return (str(value or "").replace("{mention}", mention)
            .replace("{chat}", chat_name).replace("{bot_mention}", bot_mention()))

def save_message(kind, message):
    if not mongo_ready:
        return False
    item = message.get(kind) if kind in MEDIA_FIELDS else None
    if kind == "photo":
        photos = message.get("photo") or []
        item = photos[-1] if photos else {}
    record = {
        "_id": "start_message" if pending.get(int(message["from"]["id"])) == "start" else "join_message",
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

def handle_message(message):
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    uid = int(user.get("id") or 0)
    chat_id = int(chat.get("id") or 0)
    if chat.get("type") != "private":
        return
    text = str(message.get("text") or "").strip()
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
    if command == "/start":
        saved = db.settings.find_one({"_id": "start_message"}) if mongo_ready else None
        if saved:
            send_saved(saved, chat_id, user)
        else:
            welcome = ("✦ <b>WELCOME TO JOIN BOT</b> ✦\n\n"
                       f"Hello {render_text('{mention}', user)},\n\n"
                       f"I automatically approve channel join requests. Add me as an admin "
                       "and grant permission to invite users.")
            tg("sendMessage", {"chat_id": chat_id, "text": welcome, "parse_mode": "HTML"})
        inc("starts")
        return
    if not is_admin(uid):
        return
    if command in ("/setstart", "/joinmessage"):
        pending[uid] = "start" if command == "/setstart" else "join"
        label = "start message" if command == "/setstart" else "join message"
        tg("sendMessage", {"chat_id": chat_id, "text": f"Send the {label} now. Text or media is supported. /cancel to stop."})
        return
    if command == "/cancel":
        pending.pop(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "Cancelled. No changes were made."})
        return
    if command == "/default":
        if mongo_ready:
            db.settings.delete_one({"_id": "start_message"})
        pending.pop(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "Default /start message restored."})
        return
    if command == "/empty":
        if mongo_ready:
            db.settings.delete_one({"_id": "join_message"})
        pending.pop(uid, None)
        tg("sendMessage", {"chat_id": chat_id, "text": "Saved join message disabled."})
        return
    if command == "/stats":
        stats = db.stats.find_one({"_id": "global"}) if mongo_ready else {}
        uptime = max(0, int(time.time() - started_at))
        hours, rem = divmod(uptime, 3600)
        tg("sendMessage", {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": ("✦ <b>JOINBOT LIVE STATS</b>\n\n"
                     f"🗄️ Database: {'🟢 Connected' if mongo_ready else '🔴 Offline'}\n"
                     f"📥 Requests: <b>{(stats or {}).get('requests', 0)}</b>\n"
                     f"✅ Approved: <b>{(stats or {}).get('approved', 0)}</b>\n"
                     f"❌ Failed: <b>{(stats or {}).get('failed', 0)}</b>\n"
                     f"✉️ /start uses: <b>{(stats or {}).get('starts', 0)}</b>\n"
                     f"⏱ Uptime: <b>{hours}h {rem // 60}m</b>")
        })
        return
    if uid in pending:
        kind = next((k for k in MEDIA_FIELDS if message.get(k)), "text" if message.get("text") is not None else None)
        if not kind:
            tg("sendMessage", {"chat_id": chat_id, "text": "Please send text or supported media, or /cancel."})
            return
        if save_message(kind, message):
            pending.pop(uid, None)
            tg("sendMessage", {"chat_id": chat_id, "text": "Saved successfully in MongoDB."})
        else:
            tg("sendMessage", {"chat_id": chat_id, "text": "Could not save; check MongoDB connection."})

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
        send_saved(saved, user_chat_id, user, chat)

def process_update(update):
    if update.get("chat_join_request"):
        handle_join(update)
    elif update.get("message"):
        handle_message(update["message"])

def acquire_lease():
    if not mongo_ready:
        return False
    now = datetime.utcnow()
    expiry = now + timedelta(seconds=75)
    try:
        doc = db.settings.find_one_and_update(
            {"_id": "poller_lease", "$or": [{"expires_at": {"$lte": now}}, {"owner": poller_owner}]},
            {"$set": {"owner": poller_owner, "expires_at": expiry}},
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            return doc.get("owner") == poller_owner
        db.settings.insert_one({"_id": "poller_lease", "owner": poller_owner, "expires_at": expiry})
        return True
    except Exception as exc:
        if getattr(exc, "code", None) == 11000:
            return False
        log.exception("Polling lease error")
        return False

def poll_loop():
    global offset
    while not acquire_lease():
        time.sleep(5)
    tg("deleteWebhook", {"drop_pending_updates": False})
    last_renewal = 0
    log.info("Telegram polling started")
    while True:
        try:
            if time.monotonic() - last_renewal >= 15:
                updated = db.settings.update_one(
                    {"_id": "poller_lease", "owner": poller_owner},
                    {"$set": {"expires_at": datetime.utcnow() + timedelta(seconds=75)}},
                )
                if not updated.matched_count:
                    log.error("Lost MongoDB polling lease; stopping")
                    return
                last_renewal = time.monotonic()
            result = tg("getUpdates", {
                "offset": offset, "timeout": 25,
                "allowed_updates": ["message", "chat_join_request"],
            }, timeout=35)
            if not result.get("ok"):
                log.error("getUpdates failed: %s", result.get("description"))
                time.sleep(5)
                continue
            for update in result.get("result") or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    process_update(update)
                except Exception:
                    log.exception("Update processing failed")
        except Exception:
            log.exception("Polling loop crashed")
            time.sleep(3)

def start_bot():
    global poller_started
    with poller_lock:
        if poller_started:
            return
        poller_started = True
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is missing; bot polling disabled")
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
                {"command": "stats", "description": "View bot statistics"},
                {"command": "cancel", "description": "Cancel setup"},
            ],
            "scope": {"type": "chat", "chat_id": admin_id},
        })
    threading.Thread(target=poll_loop, daemon=True, name="telegram-poller").start()

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
    })

if __name__ == "__main__":
    start_bot()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)
