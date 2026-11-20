import os, re, io, time, html, base64, hashlib, random, secrets, asyncio, imaplib, email
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import quote

import qrcode
from aiohttp import web
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from pyrogram import Client, filters, idle
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB

# ---- ENV (sirf yeh 5 chahiye; baaki sab bot ke /admin panel se) ----
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
OWNER_ID = int(os.environ["OWNER_ID"])

PAY_WINDOW = 600  # 10 minutes
app = Client("paybot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(BOT_TOKEN.encode()).digest()))
db = None
BOT_USERNAME = ""
state = {}  # uid -> {"step": ..., ...}

DEFAULTS = dict(auto_on=False, maint=False, manual_on=True, upi_id="", upi_name="",
                gmail="", app_pw="", sender="", admins=[])


# ---------------- helpers ----------------
async def cfg():
    d = await db.settings.find_one({"_id": "cfg"}) or {}
    return {**DEFAULTS, **d}


async def setcfg(**kw):
    await db.settings.update_one({"_id": "cfg"}, {"$set": kw}, upsert=True)


async def is_admin(uid):
    return uid == OWNER_ID or uid in (await cfg())["admins"]


def tick(v):
    return "✅" if v else "❌"


def fmt(x):
    return f"{x:g}"


def fid(m):
    for a in ("document", "video", "audio", "voice", "photo", "animation", "video_note"):
        x = getattr(m, a, None)
        if x:
            return x.file_id


def make_qr(link):
    img = qrcode.make(link, box_size=10, border=2).get_image().convert("L").resize((250, 250), resample=0)
    b = io.BytesIO()
    b.name = "qr.png"
    img.save(b, "PNG")
    b.seek(0)
    return b


def upi_link(c, amount, note):
    return (f"upi://pay?pa={quote(c['upi_id'])}&pn={quote(c['upi_name'] or 'Pay')}"
            f"&am={amount:.2f}&tn={note}&cu=INR")


async def deliver(uid, item_id):
    it = await db.items.find_one({"_id": item_id})
    await app.send_message(uid, "✅ Payment verified! Aapki files bhej raha hoon...")
    for f in it["files"]:
        try:
            await app.send_cached_media(uid, f)
        except Exception as e:
            print("send error", e)


# ---------------- IMAP check ----------------
def text_of(em):
    parts = []
    for p in em.walk():
        if p.get_content_type() in ("text/plain", "text/html"):
            pl = p.get_payload(decode=True)
            if pl:
                parts.append(pl.decode(p.get_content_charset() or "utf-8", "replace"))
    t = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(parts)))
    return re.sub(r"\s+", " ", t)


def check_mail(c, since_ts):
    pw = fernet.decrypt(c["app_pw"].encode()).decode()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(c["gmail"], pw)
        M.select("INBOX")
        day = datetime.fromtimestamp(since_ts - 86400, tz=timezone.utc).strftime("%d-%b-%Y")
        _, data = M.search(None, f'(FROM "{c["sender"]}" SINCE {day})')
        out = []
        for i in reversed(data[0].split()[-25:]):
            _, msg = M.fetch(i, "(RFC822)")
            em = email.message_from_bytes(msg[0][1])
            if not em["Date"]:
                continue
            ts = parsedate_to_datetime(em["Date"]).timestamp()
            if ts < since_ts - 30:
                continue
            body = text_of(em)
            amts = [float(x.replace(",", "")) for x in
                    re.findall(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", body, re.I)]
            m = re.search(r"\b\d{12}\b", body)
            key = m.group(0) if m else (em["Message-ID"] or str(ts))
            out.append((amts, key, ts))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def test_login(gmail, pw):
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(gmail, pw)
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------- admin panel ----------------
async def panel():
    c = await cfg()
    text = (
        "⚙️ **Admin Panel**\n\n"
        f"💳 Auto Payment: {tick(c['auto_on'])}\n"
        f"🛠 Under Maintenance: {tick(c['maint'])}\n"
        f"🧾 Manual Payment: {tick(c['manual_on'])}\n"
        f"📧 Gmail: {c['gmail'] or 'not set'} {tick(bool(c['app_pw']))}\n"
        f"🏦 Bank sender: {c['sender'] or 'not set'}\n"
        f"💸 UPI: {c['upi_id'] or 'not set'}\n"
        f"👤 Admins: {len(c['admins'])}"
    )
    kb = IKM([
        [IKB(f"⚡ Auto Payment: {tick(c['auto_on'])}", callback_data="a:auto")],
        [IKB(f"🛠 Under Maintenance: {tick(c['maint'])}", callback_data="a:maint")],
        [IKB(f"🧾 Manual Payment: {tick(c['manual_on'])}", callback_data="a:manual")],
        [IKB("📧 Gmail + App Password", callback_data="a:gmail"),
         IKB("🏦 Bank Sender", callback_data="a:sender")],
        [IKB("💸 UPI ID / Name", callback_data="a:upi")],
        [IKB("➕ Add Files", callback_data="a:add"), IKB("📦 Products", callback_data="a:items")],
        [IKB("👤 Add Admin", callback_data="a:addadmin"), IKB("🗑 Remove Admin", callback_data="a:rmadmin")],
    ])
    return text, kb


async def refresh_panel(q):
    text, kb = await panel()
    try:
        await q.message.edit_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


@app.on_message(filters.command("admin") & filters.private)
async def admin_cmd(cl, m):
    if not await is_admin(m.from_user.id):
        return
    state.pop(m.from_user.id, None)
    text, kb = await panel()
    await m.reply(text, reply_markup=kb)


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_cmd(cl, m):
    state.pop(m.from_user.id, None)
    await m.reply("❌ Cancelled.")


@app.on_message(filters.command("done") & filters.private)
async def done_cmd(cl, m):
    st = state.get(m.from_user.id)
    if not st or st["step"] != "files" or not st["files"]:
        return await m.reply("Pehle kam se kam ek file bhejo.")
    st["step"] = "price"
    await m.reply("💰 Price kitna rakhna hai? (₹, sirf number)")


@app.on_callback_query(filters.regex(r"^a:"))
async def admin_cb(cl, q):
    uid = q.from_user.id
    if not await is_admin(uid):
        return await q.answer("Not allowed", show_alert=True)
    act = q.data.split(":")[1]
    c = await cfg()

    if act == "auto":
        if not c["auto_on"] and not (c["gmail"] and c["app_pw"] and c["sender"] and c["upi_id"]):
            return await q.answer("Pehle Gmail+App Password, Bank Sender aur UPI set karo.", show_alert=True)
        await setcfg(auto_on=not c["auto_on"])
        await q.answer()
        return await refresh_panel(q)
    if act == "maint":
        await setcfg(maint=not c["maint"])
        await q.answer()
        return await refresh_panel(q)
    if act == "manual":
        await setcfg(manual_on=not c["manual_on"])
        await q.answer()
        return await refresh_panel(q)

    prompts = {
        "upi": ("upi", "💸 Bhejo: `upi@bank | Naam`"),
        "gmail": ("gmail", "📧 Apna Gmail address bhejo."),
        "sender": ("sender", "🏦 Bank ka email address bhejo jis se payment alert aata hai (jaise alerts@hdfcbank.net)."),
        "addadmin": ("addadmin", "👤 Naye admin ki Telegram numeric ID bhejo."),
        "rmadmin": ("rmadmin", "🗑 Hatane wale admin ki numeric ID bhejo."),
    }
    if act in ("addadmin", "rmadmin") and uid != OWNER_ID:
        return await q.answer("Sirf owner kar sakta hai.", show_alert=True)
    if act in prompts:
        state[uid] = {"step": prompts[act][0]}
        await q.answer()
        return await q.message.reply(prompts[act][1] + "\n\n(/cancel se band karo)")
    if act == "add":
        state[uid] = {"step": "files", "files": []}
        await q.answer()
        return await q.message.reply("📁 Ek ya kai files bhejo. Ho jaye to /done likho. (/cancel se band)")
    if act == "items":
        await q.answer()
        items = await db.items.find().sort("_id", -1).to_list(20)
        if not items:
            return await q.message.reply("Koi product nahi hai.")
        txt = "\n".join(f"• {i['title']} — ₹{fmt(i['price'])} ({len(i['files'])} files)\n"
                        f"  https://t.me/{BOT_USERNAME}?start=item_{i['_id']}" for i in items)
        kb = IKM([[IKB(f"🗑 {i['title'][:30]}", callback_data=f"a:del:{i['_id']}")] for i in items])
        return await q.message.reply(txt, reply_markup=kb, disable_web_page_preview=True)
    if act == "del":
        await db.items.delete_one({"_id": q.data.split(":")[2]})
        await q.answer("Deleted ✅", show_alert=True)
        return await q.message.delete()


# ---------------- admin / user step flow ----------------
@app.on_message(filters.private & ~filters.command(["start", "admin", "done", "cancel"]), group=1)
async def flow(cl, m):
    uid = m.from_user.id
    st = state.get(uid)
    if not st:
        return
    step = st["step"]

    if step == "shot":  # user screenshot (manual payment)
        if not m.photo:
            return await m.reply("📸 Payment ka screenshot (photo) bhejo.")
        o = await db.orders.find_one({"_id": st["oid"]})
        await db.orders.update_one({"_id": o["_id"]}, {"$set": {"status": "review"}})
        c = await cfg()
        it = await db.items.find_one({"_id": o["item"]})
        kb = IKM([[IKB("✅ Approve", callback_data=f"m:ok:{o['_id']}"),
                   IKB("❌ Reject", callback_data=f"m:no:{o['_id']}")]])
        cap = (f"🧾 Manual payment\nUser: {m.from_user.mention} (`{uid}`)\n"
               f"Product: {it['title']}\nAmount: ₹{fmt(o['amount'])}\nOrder: `{o['_id']}`")
        for aid in {OWNER_ID, *c["admins"]}:
            try:
                await app.send_photo(aid, m.photo.file_id, caption=cap, reply_markup=kb)
            except Exception:
                pass
        state.pop(uid, None)
        return await m.reply("⏳ Screenshot admin ko bhej diya. Approve hote hi file mil jayegi.")

    if not await is_admin(uid):
        return

    txt = (m.text or "").strip()
    if step == "upi":
        if "@" not in txt:
            return await m.reply("Format: `upi@bank | Naam`")
        p = [x.strip() for x in txt.split("|")]
        await setcfg(upi_id=p[0], upi_name=p[1] if len(p) > 1 else "")
        state.pop(uid)
        return await m.reply("✅ UPI save ho gaya. /admin")
    if step == "gmail":
        st.update(step="apppw", gmail=txt)
        return await m.reply("🔑 Ab Gmail **App Password** bhejo (16 characters). Save hote hi message delete ho jayega.")
    if step == "apppw":
        pw = txt.replace(" ", "")
        try:
            await m.delete()
        except Exception:
            pass
        try:
            await asyncio.to_thread(test_login, st["gmail"], pw)
        except Exception as e:
            state.pop(uid)
            return await app.send_message(uid, f"❌ Login fail: {e}\nIMAP enable aur App Password check karo. /admin")
        await setcfg(gmail=st["gmail"], app_pw=fernet.encrypt(pw.encode()).decode())
        state.pop(uid)
        return await app.send_message(uid, "✅ Gmail login test pass, save ho gaya. /admin")
    if step == "sender":
        await setcfg(sender=txt)
        state.pop(uid)
        return await m.reply("✅ Bank sender save ho gaya. /admin")
    if step in ("addadmin", "rmadmin"):
        if not txt.isdigit():
            return await m.reply("Sirf numeric ID bhejo.")
        c = await cfg()
        ad = set(c["admins"])
        ad.add(int(txt)) if step == "addadmin" else ad.discard(int(txt))
        await setcfg(admins=list(ad))
        state.pop(uid)
        return await m.reply("✅ Done. /admin")
    if step == "files":
        f = fid(m)
        if not f:
            return await m.reply("Sirf file/video/audio/photo bhejo.")
        st["files"].append(f)
        return await m.reply(f"✅ {len(st['files'])} file add. Aur bhejo ya /done.")
    if step == "price":
        try:
            st["price"] = float(txt)
        except ValueError:
            return await m.reply("Sirf number bhejo, jaise 49")
        st["step"] = "title"
        return await m.reply("📝 Product ka naam likho.")
    if step == "title":
        iid = secrets.token_urlsafe(6)
        await db.items.insert_one({"_id": iid, "title": txt, "price": st["price"], "files": st["files"]})
        state.pop(uid)
        return await m.reply(f"✅ Product ban gaya!\n\n🔗 https://t.me/{BOT_USERNAME}?start=item_{iid}",
                             disable_web_page_preview=True)


# ---------------- user side ----------------
@app.on_message(filters.command("start") & filters.private)
async def start(cl, m):
    if len(m.command) > 1 and m.command[1].startswith("item_"):
        it = await db.items.find_one({"_id": m.command[1][5:]})
        if not it:
            return await m.reply("❌ Link invalid hai.")
        kb = IKM([[IKB("⚡ Automatic", callback_data=f"u:auto:{it['_id']}"),
                   IKB("🧾 Manual", callback_data=f"u:man:{it['_id']}")]])
        return await m.reply(f"📦 **{it['title']}**\n💰 Price: ₹{fmt(it['price'])}\n"
                             f"📁 Files: {len(it['files'])}\n\nChoose your payment method 👇", reply_markup=kb)
    await m.reply("👋 Welcome! Koi product link kholo.")


@app.on_callback_query(filters.regex(r"^u:auto:"))
async def user_auto(cl, q):
    c = await cfg()
    if c["maint"] or not c["auto_on"]:
        return await q.answer("🛠 Automatic payment abhi Under Maintenance hai. Manual use karo.", show_alert=True)
    it = await db.items.find_one({"_id": q.data.split(":")[2]})
    if not it:
        return await q.answer("Product nahi mila", show_alert=True)
    await q.answer()
    uid = q.from_user.id
    wait = await app.send_message(uid, "⏳ Invoice generate ho raha hai, please wait...")
    amount = float(it["price"])
    oid = secrets.token_hex(4)
    await db.orders.insert_one({"_id": oid, "uid": uid, "item": it["_id"], "amount": amount,
                                "mode": "auto", "status": "pending", "ts": time.time()})
    qr = make_qr(upi_link(c, amount, oid))
    await wait.delete()
    kb = IKM([[IKB("✅ Verify Payment", callback_data=f"u:ver:{oid}")],
              [IKB("✖️ Cancel", callback_data=f"u:cx:{oid}")]])
    await app.send_photo(uid, qr, reply_markup=kb, caption=(
        f"🧾 **Order** `{oid}`\n📦 {it['title']}\n💰 Pay exactly: **₹{amount:.2f}**\n"
        f"🏦 UPI: `{c['upi_id']}`\n⏱ Valid for 10 minutes\n\n"
        "QR scan karke **exact amount** pay karo, phir Verify dabao."))


@app.on_callback_query(filters.regex(r"^u:cx:"))
async def user_cancel(cl, q):
    await db.orders.update_one({"_id": q.data.split(":")[2], "uid": q.from_user.id, "status": "pending"},
                               {"$set": {"status": "cancelled"}})
    await q.answer("Cancelled")
    await q.message.delete()


@app.on_callback_query(filters.regex(r"^u:ver:"))
async def user_verify(cl, q):
    oid = q.data.split(":")[2]
    o = await db.orders.find_one({"_id": oid})
    if not o or o["uid"] != q.from_user.id:
        return await q.answer("Order nahi mila", show_alert=True)
    if o["status"] == "paid":
        return await q.answer("Already verified ✅", show_alert=True)
    if o["status"] != "pending":
        return await q.answer("Yeh order band ho chuka hai.", show_alert=True)
    if time.time() - o["ts"] > PAY_WINDOW:
        await db.orders.update_one({"_id": oid}, {"$set": {"status": "expired"}})
        await q.answer()
        return await q.message.edit_caption("⌛ Order expire ho gaya. Link se dobara try karo.")
    earlier = await db.orders.find_one({"mode": "auto", "status": "pending", "amount": o["amount"],
                                        "ts": {"$lt": o["ts"], "$gt": time.time() - PAY_WINDOW}})
    if earlier:
        return await q.answer("⏳ Same amount ka ek aur order pehle se verify ho raha hai. "
                              "Thodi der baad dobara try karo.", show_alert=True)
    await q.answer("🔎 Checking payment...")
    c = await cfg()
    try:
        hits = await asyncio.to_thread(check_mail, c, o["ts"])
    except Exception as e:
        print("imap error", e)
        return await app.send_message(q.from_user.id, "⚠️ Verification mein dikkat aayi, thodi der baad try karo.")
    for amts, key, _ in hits:
        if not any(abs(a - o["amount"]) < 0.005 for a in amts):
            continue
        try:
            await db.used_utr.insert_one({"_id": key, "oid": oid})
        except DuplicateKeyError:
            continue
        r = await db.orders.update_one({"_id": oid, "status": "pending"},
                                       {"$set": {"status": "paid", "utr": key}})
        if r.modified_count:
            await q.message.delete()
            return await deliver(o["uid"], o["item"])
    await app.send_message(q.from_user.id, "❌ Payment abhi nahi mila. Pay karke 1 minute baad dobara Verify dabao.")


@app.on_callback_query(filters.regex(r"^u:man:"))
async def user_manual(cl, q):
    c = await cfg()
    if not c["manual_on"] or not c["upi_id"]:
        return await q.answer("Manual payment abhi available nahi hai.", show_alert=True)
    it = await db.items.find_one({"_id": q.data.split(":")[2]})
    if not it:
        return await q.answer("Product nahi mila", show_alert=True)
    await q.answer()
    uid = q.from_user.id
    oid = secrets.token_hex(4)
    await db.orders.insert_one({"_id": oid, "uid": uid, "item": it["_id"], "amount": it["price"],
                                "mode": "manual", "status": "awaiting", "ts": time.time()})
    state[uid] = {"step": "shot", "oid": oid}
    await app.send_photo(uid, make_qr(upi_link(c, it["price"], oid)), caption=(
        f"🧾 **Manual Payment**\n📦 {it['title']}\n💰 Amount: **₹{fmt(it['price'])}**\n"
        f"🏦 UPI: `{c['upi_id']}`\n\nPay karke payment ka **screenshot** yahin bhejo."))


@app.on_callback_query(filters.regex(r"^m:"))
async def manual_decide(cl, q):
    if not await is_admin(q.from_user.id):
        return await q.answer("Not allowed", show_alert=True)
    _, act, oid = q.data.split(":")
    new = "paid" if act == "ok" else "rejected"
    r = await db.orders.update_one({"_id": oid, "status": "review"}, {"$set": {"status": new}})
    if not r.modified_count:
        return await q.answer("Already handled.", show_alert=True)
    o = await db.orders.find_one({"_id": oid})
    await q.answer("Done")
    await q.message.edit_caption(q.message.caption + f"\n\n{'✅ APPROVED' if act == 'ok' else '❌ REJECTED'} by {q.from_user.first_name}")
    if act == "ok":
        await deliver(o["uid"], o["item"])
    else:
        await app.send_message(o["uid"], "❌ Aapka payment reject ho gaya. Sahi screenshot ke saath dobara try karo.")


# ---------------- run ----------------
async def health(_):
    return web.Response(text="ok")


async def keep_alive():
    # Render free tier 15 min idle par sleep ho jata hai; yeh khud ko har 10 min ping karta hai.
    # Render apne aap RENDER_EXTERNAL_URL set karta hai. Extra UptimeRobot ping bhi laga sakte ho.
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL")
    if not url:
        return
    import aiohttp
    while True:
        await asyncio.sleep(600)
        try:
            async with aiohttp.ClientSession() as s:
                await s.get(url, timeout=aiohttp.ClientTimeout(total=20))
        except Exception as e:
            print("keep-alive error", e)


async def main():
    global db, BOT_USERNAME
    db = AsyncIOMotorClient(MONGO_URI)["paybot"]
    await app.start()
    me = await app.get_me()
    BOT_USERNAME = me.username or ""
    if not BOT_USERNAME:  # purane Pyrogram par kabhi username None aata hai
        import aiohttp
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe") as resp:
                    BOT_USERNAME = (await resp.json())["result"].get("username", "")
        except Exception as e:
            print("getMe error", e)
    BOT_USERNAME = BOT_USERNAME or os.environ.get("BOT_USERNAME", "")
    # Render web service: PORT par health server (default 10000)
    port = int(os.environ.get("PORT", 10000))
    a = web.Application()
    a.router.add_get("/", health)
    r = web.AppRunner(a)
    await r.setup()
    await web.TCPSite(r, "0.0.0.0", port).start()
    asyncio.create_task(keep_alive())
    print(f"Bot started as @{BOT_USERNAME} | port {port}")
    await idle()
    await app.stop()


app.run(main())
