"""Shared stuff: env, pyrogram client, db handle, settings helpers, logging."""
import os, time, base64, hashlib, asyncio
from datetime import datetime, timezone, timedelta
from cryptography.fernet import Fernet
from pyrogram import Client, enums
from pyrogram.types import InlineKeyboardButton as IKB

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
OWNER_ID = int(os.environ["OWNER_ID"])

DEV = os.environ.get("DEV_USERNAME", "kcxry").lstrip("@")
CMDS = ["start", "admin", "done", "cancel"]
PAY_WINDOW = 600  # 10 minutes
IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata

app = Client("paybot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(BOT_TOKEN.encode()).digest()))

db = None            # main.py set karta hai (use C.db)
BOT_USERNAME = ""    # main.py set karta hai (use C.BOT_USERNAME)
state = {}           # uid -> {"step": ...}

DEFAULTS = dict(auto_on=False, maint=False, manual_on=True, upi_id="", upi_name="",
                gmail="", app_pw="", sender="", admins=[], log_chat=0, last_start=0,
                welcome_photo="", welcome_text="", miniapp_on=False)


def base_url():
    """Public https URL of this service (Render sets RENDER_EXTERNAL_URL automatically)."""
    return (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL") or "").rstrip("/")


def ist(ts=None):
    return datetime.fromtimestamp(ts if ts else time.time(), IST).strftime("%d-%b-%Y %I:%M:%S %p IST")


def tick(v):
    return "✅" if v else "❌"


def fmt(x):
    return f"{x:g}"


def fid(m):
    for a in ("document", "video", "audio", "voice", "photo", "animation", "video_note"):
        x = getattr(m, a, None)
        if x:
            return x.file_id


def user_line(u):
    name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "NoName"
    return f"{name}" + (f" (@{u.username})" if u.username else "") + f" [{u.id}]"


async def cfg():
    d = await db.settings.find_one({"_id": "cfg"}) or {}
    return {**DEFAULTS, **d}


async def setcfg(**kw):
    await db.settings.update_one({"_id": "cfg"}, {"$set": kw}, upsert=True)


async def is_admin(uid):
    return uid == OWNER_ID or uid in (await cfg())["admins"]


async def log(text):
    """DB mein save + (agar set ho) log channel mein bhejo. Time hamesha IST."""
    now = time.time()
    try:
        await db.logs.insert_one({"ts": now, "text": text})
    except Exception as e:
        print("log db error", e)
    c = await cfg()
    if c["log_chat"]:
        try:
            await app.send_message(c["log_chat"], f"🕒 {ist(now)}\n{text}",
                                   parse_mode=enums.ParseMode.DISABLED)
        except Exception as e:
            print("log channel error", e)


# ---------------- look & feel ----------------
_SC = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ")


def sc(text):
    """Small-caps font: sc('verify payment') -> ᴠᴇʀɪꜰʏ ᴘᴀʏᴍᴇɴᴛ"""
    return text.lower().translate(_SC)


def dev_row():
    return [IKB("👨‍💻 " + sc("developer"), url=f"https://t.me/{DEV}")]


_tasks = set()


async def delete_later(msg, secs=30):
    await asyncio.sleep(secs)
    try:
        await msg.delete()
    except Exception:
        pass


def schedule_delete(msg, secs=30):
    t = asyncio.create_task(delete_later(msg, secs))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def temp(chat_id, text, secs=30, **kw):
    """Message bhejo jo `secs` second baad apne-aap delete ho jaye."""
    msg = await app.send_message(chat_id, text, **kw)
    schedule_delete(msg, secs)
    return msg
