"""Payments: IMAP auto verify, manual approval, QR, Gmail test."""
import re, io, time, html, secrets, asyncio, imaplib, email
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import quote
import qrcode
from pymongo.errors import DuplicateKeyError
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
import core as C
from core import (app, state, CMDS, PAY_WINDOW, fernet, OWNER_ID, cfg, is_admin, fmt, log, ist, user_line,
                  sc, temp, schedule_delete, dev_row, base_url)
from file import deliver


# ---------------- QR / UPI ----------------
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


# ---------------- IMAP ----------------
def text_of(em):
    parts = []
    for p in em.walk():
        if p.get_content_type() in ("text/plain", "text/html"):
            pl = p.get_payload(decode=True)
            if pl:
                parts.append(pl.decode(p.get_content_charset() or "utf-8", "replace"))
    t = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(parts)))
    return re.sub(r"\s+", " ", t)


def parse_mail(em):
    body = text_of(em)
    amts = [float(x.replace(",", "")) for x in
            re.findall(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", body, re.I)]
    m = re.search(r"\b\d{12}\b", body)
    credit = bool(re.search(r"credit|received|deposit", body, re.I))
    return amts, (m.group(0) if m else None), credit


def imap_login(c):
    pw = fernet.decrypt(c["app_pw"].encode()).decode()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(c["gmail"], pw)
    M.select("INBOX", readonly=True)
    return M


def imap_close(M):
    try:
        M.logout()
    except Exception:
        pass


def fetch_msg(M, i):
    _, msg = M.fetch(i, "(BODY.PEEK[])")
    return email.message_from_bytes(msg[0][1])


def check_mail(c, since_ts):
    """Sender ki parwah nahi: recent mails mein credit + amount + 12-digit UTR dhundho."""
    M = imap_login(c)
    try:
        day = datetime.fromtimestamp(since_ts - 86400, tz=timezone.utc).strftime("%d-%b-%Y")
        _, data = M.search(None, f"(SINCE {day})")
        out = []
        for i in reversed(data[0].split()[-40:]):
            em = fetch_msg(M, i)
            if not em["Date"]:
                continue
            ts = parsedate_to_datetime(em["Date"]).timestamp()
            if ts < since_ts - 30:  # order se pehle ke mail ignore
                continue
            amts, utr, credit = parse_mail(em)
            if utr and credit:
                out.append((amts, utr, ts))
        return out
    finally:
        imap_close(M)


def test_inbox(c):
    """Inbox ke latest 5 mail padho aur dikhao ki bot kya parse kar raha hai."""
    M = imap_login(c)
    try:
        _, data = M.search(None, "ALL")
        ids = data[0].split()[-5:]
        out = [f"🧪 Gmail Test\n✅ Login OK: {c['gmail']}"]
        if not ids:
            out.append("📭 Inbox khaali hai.")
        for n, i in enumerate(reversed(ids), 1):
            em = fetch_msg(M, i)
            try:
                when = ist(parsedate_to_datetime(em["Date"]).timestamp())
            except Exception:
                when = "?"
            amts, utr, credit = parse_mail(em)
            ok = bool(amts and utr and credit)
            out.append(f"\n{n}) 🕒 {when}\n💰 Amount: {', '.join(map(str, amts[:6])) or 'not found'}\n"
                       f"🔢 UTR: {utr or 'not found'}\n💚 Credit mail: {'yes' if credit else 'no'}"
                       f"\n{'✅ Payment mail jaisa dikh raha hai' if ok else '— payment mail nahi / parse nahi hua'}")
        return "\n".join(out)
    finally:
        imap_close(M)


def test_login(gmail, pw):
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(gmail, pw)
    finally:
        imap_close(M)


# ---------------- automatic payment ----------------
FRAMES = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]  # ghoomta hua circle
verifying = set()


async def create_auto_order(uid, it):
    oid = secrets.token_hex(6)
    amount = float(it["price"])
    await C.db.orders.insert_one({"_id": oid, "uid": uid, "item": it["_id"], "amount": amount,
                                  "mode": "auto", "status": "pending", "ts": time.time()})
    return oid, amount


async def precheck(o):
    """None = verify kar sakte hain; warna 'paid' | 'closed' | 'expired' | 'busy'."""
    if o["status"] == "paid":
        return "paid"
    if o["status"] != "pending":
        return "closed"
    if time.time() - o["ts"] > PAY_WINDOW:
        await C.db.orders.update_one({"_id": o["_id"]}, {"$set": {"status": "expired"}})
        return "expired"
    earlier = await C.db.orders.find_one({"mode": "auto", "status": "pending", "amount": o["amount"],
                                          "ts": {"$lt": o["ts"], "$gt": time.time() - PAY_WINDOW}})
    return "busy" if earlier else None


async def claim_payment(o, hits):
    """Mails mein amount + UTR match ho to order paid mark karo. True = payment mil gaya."""
    oid = o["_id"]
    for amts, key, _ in hits:
        if not any(abs(a - o["amount"]) < 0.005 for a in amts):
            continue
        try:
            await C.db.used_utr.insert_one({"_id": key, "oid": oid})
        except DuplicateKeyError:
            continue
        r = await C.db.orders.update_one({"_id": oid, "status": "pending"},
                                         {"$set": {"status": "paid", "utr": key}})
        if r.modified_count:
            await log(f"💰 Auto payment verified\nOrder {oid} | ₹{fmt(o['amount'])} | UTR/ID {key}\nUser: {o['uid']}")
            return True
    await log(f"❌ Verify failed (not found): order {oid}, ₹{fmt(o['amount'])}, mails checked: {len(hits)}")
    return False


@app.on_callback_query(filters.regex(r"^u:auto:"))
async def user_auto(cl, q):
    c = await cfg()
    if c["maint"] or not c["auto_on"]:
        return await q.answer("🛠 " + sc("automatic payment is under maintenance. please use manual."),
                              show_alert=True)
    it = await C.db.items.find_one({"_id": q.data.split(":")[2]})
    if not it:
        return await q.answer(sc("product not found"), show_alert=True)
    await q.answer()
    uid = q.from_user.id
    wait = await app.send_message(uid, "⏳ " + sc("invoice is generating, please wait..."))
    oid, amount = await create_auto_order(uid, it)
    qr = make_qr(upi_link(c, amount, oid))
    await wait.delete()
    rows = [[IKB("✅ " + sc("verify payment"), callback_data=f"u:ver:{oid}")]]
    base = base_url()
    if base.startswith("https://"):  # UPI app chooser page (Pay with UPI)
        rows.append([IKB("💳 " + sc("pay with upi"), url=f"{base}/pay/{oid}")])
    rows += [[IKB("✖️ " + sc("cancel"), callback_data=f"u:cx:{oid}")], dev_row()]
    await app.send_photo(uid, qr, reply_markup=IKM(rows), caption=(
        f"🧾 {sc('order')} `{oid}`\n📦 {sc('item')}: {it['title']}\n"
        f"💰 {sc('amount')}: **₹{amount:.2f}**\n🏦 {sc('upi id')}: `{c['upi_id']}`\n"
        f"⏱ {sc('valid for 10 minutes')}\n\n"
        + sc("tap pay with upi (or scan the qr), then tap verify.")))
    await log(f"🧾 Auto order {oid}: {user_line(q.from_user)} → {it['title']} ₹{fmt(amount)}")


@app.on_callback_query(filters.regex(r"^u:cx:"))
async def user_cancel(cl, q):
    await C.db.orders.update_one({"_id": q.data.split(":")[2], "uid": q.from_user.id, "status": "pending"},
                                 {"$set": {"status": "cancelled"}})
    await q.answer()
    await q.message.delete()


@app.on_callback_query(filters.regex(r"^u:ver:"))
async def user_verify(cl, q):
    uid = q.from_user.id
    oid = q.data.split(":")[2]
    await q.answer()  # popup nahi — neeche message aayega
    o = await C.db.orders.find_one({"_id": oid})
    if not o or o["uid"] != uid:
        return await temp(uid, "⚠️ " + sc("order not found."))
    st = await precheck(o)
    if st == "paid":
        return await temp(uid, "✅ " + sc("this order is already verified."))
    if st == "closed":
        return await temp(uid, "⚠️ " + sc("this order is closed."))
    if st == "expired":
        return await q.message.edit_caption("⌛ " + sc("order expired. please open the link again."))
    if st == "busy":
        return await temp(uid, "⏳ " + sc("another order with the same amount is being verified. try again shortly."))
    if oid in verifying:
        return
    verifying.add(oid)
    try:
        spin = await app.send_message(uid, f"{FRAMES[0]} " + sc("verifying payment"))
        c = await cfg()
        task = asyncio.create_task(asyncio.to_thread(check_mail, c, o["ts"]))
        i = 0
        while not task.done() and i < 60:
            await asyncio.sleep(1.1)
            i += 1
            try:
                await spin.edit_text(f"{FRAMES[i % 8]} " + sc("verifying payment") + "." * (i % 4))
            except Exception:
                pass
        try:
            hits = await asyncio.wait_for(task, 15)
        except Exception as e:
            print("imap error", e)
            await log(f"⚠️ IMAP error on verify (order {oid}): {e}")
            await spin.edit_text("⚠️ " + sc("verification error. please try again in a moment."))
            return schedule_delete(spin, 30)
        if await claim_payment(o, hits):
            await spin.delete()
            await q.message.delete()
            return await deliver(o["uid"], o["item"])
        await spin.edit_text("❌ " + sc("not verified yet.") + "\n"
                             + sc(f"make sure you paid exactly ₹{fmt(o['amount'])}, wait a minute and tap verify again."))
        schedule_delete(spin, 30)
    finally:
        verifying.discard(oid)


# ---------------- manual payment ----------------
async def begin_manual(uid, it):
    c = await cfg()
    if not c["manual_on"] or not c["upi_id"]:
        return await temp(uid, "⚠️ " + sc("manual payment is not available right now."))
    oid = secrets.token_hex(4)
    await C.db.orders.insert_one({"_id": oid, "uid": uid, "item": it["_id"], "amount": it["price"],
                                  "mode": "manual", "status": "awaiting", "ts": time.time()})
    state[uid] = {"step": "shot", "oid": oid}
    await app.send_photo(uid, make_qr(upi_link(c, float(it["price"]), oid)), reply_markup=IKM([dev_row()]),
                         caption=(f"🧾 **{sc('manual payment')}**\n📦 {sc('item')}: {it['title']}\n"
                                  f"💰 {sc('amount')}: **₹{fmt(it['price'])}**\n🏦 {sc('upi id')}: `{c['upi_id']}`\n\n"
                                  + sc("pay, then send the payment screenshot here.")))


@app.on_callback_query(filters.regex(r"^u:man:"))
async def user_manual(cl, q):
    c = await cfg()
    if not c["manual_on"] or not c["upi_id"]:
        return await q.answer("⚠️ " + sc("manual payment is not available right now."), show_alert=True)
    it = await C.db.items.find_one({"_id": q.data.split(":")[2]})
    if not it:
        return await q.answer(sc("product not found"), show_alert=True)
    await q.answer()
    await begin_manual(q.from_user.id, it)


@app.on_message(filters.private & ~filters.command(CMDS), group=2)
async def shot(cl, m):
    uid = m.from_user.id
    st = state.get(uid)
    if not st or st["step"] != "shot":
        return
    if not m.photo:
        return await m.reply("📸 " + sc("please send the payment screenshot (photo)."))
    o = await C.db.orders.find_one({"_id": st["oid"]})
    await C.db.orders.update_one({"_id": o["_id"]}, {"$set": {"status": "review"}})
    c = await cfg()
    it = await C.db.items.find_one({"_id": o["item"]})
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
    await log(f"🧾 Manual screenshot received: order {o['_id']} from {user_line(m.from_user)}")
    await m.reply("⏳ " + sc("screenshot sent to the admin. you will get your files once approved."))


@app.on_callback_query(filters.regex(r"^m:"))
async def manual_decide(cl, q):
    if not await is_admin(q.from_user.id):
        return await q.answer("Not allowed", show_alert=True)
    _, act, oid = q.data.split(":")
    new = "paid" if act == "ok" else "rejected"
    r = await C.db.orders.update_one({"_id": oid, "status": "review"}, {"$set": {"status": new}})
    if not r.modified_count:
        return await q.answer("Already handled.", show_alert=True)
    o = await C.db.orders.find_one({"_id": oid})
    await q.answer("Done")
    label = "✅ APPROVED" if act == "ok" else "❌ REJECTED"
    await q.message.edit_caption(q.message.caption + f"\n\n{label} by {q.from_user.first_name}")
    await log(f"{label} manual order {oid} by {q.from_user.id}")
    if act == "ok":
        await deliver(o["uid"], o["item"])
    else:
        await app.send_message(o["uid"], "❌ " + sc("your payment was rejected. please try again with a valid screenshot."))
