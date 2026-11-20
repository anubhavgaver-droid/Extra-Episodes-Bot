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
from core import (app, state, CMDS, PAY_WINDOW, fernet, OWNER_ID, cfg, is_admin, fmt, log, ist, user_line)
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
@app.on_callback_query(filters.regex(r"^u:auto:"))
async def user_auto(cl, q):
    c = await cfg()
    if c["maint"] or not c["auto_on"]:
        return await q.answer("🛠 Automatic payment abhi Under Maintenance hai. Manual use karo.", show_alert=True)
    it = await C.db.items.find_one({"_id": q.data.split(":")[2]})
    if not it:
        return await q.answer("Product nahi mila", show_alert=True)
    await q.answer()
    uid = q.from_user.id
    wait = await app.send_message(uid, "⏳ Invoice generate ho raha hai, please wait...")
    amount = float(it["price"])
    oid = secrets.token_hex(4)
    await C.db.orders.insert_one({"_id": oid, "uid": uid, "item": it["_id"], "amount": amount,
                                  "mode": "auto", "status": "pending", "ts": time.time()})
    qr = make_qr(upi_link(c, amount, oid))
    await wait.delete()
    kb = IKM([[IKB("✅ Verify Payment", callback_data=f"u:ver:{oid}")],
              [IKB("✖️ Cancel", callback_data=f"u:cx:{oid}")]])
    await app.send_photo(uid, qr, reply_markup=kb, caption=(
        f"🧾 **Order** `{oid}`\n📦 {it['title']}\n💰 Pay: **₹{amount:.2f}**\n"
        f"🏦 UPI: `{c['upi_id']}`\n⏱ Valid for 10 minutes\n\n"
        "QR scan karke pay karo, phir Verify dabao."))
    await log(f"🧾 Auto order {oid}: {user_line(q.from_user)} → {it['title']} ₹{fmt(amount)}")


@app.on_callback_query(filters.regex(r"^u:cx:"))
async def user_cancel(cl, q):
    await C.db.orders.update_one({"_id": q.data.split(":")[2], "uid": q.from_user.id, "status": "pending"},
                                 {"$set": {"status": "cancelled"}})
    await q.answer("Cancelled")
    await q.message.delete()


@app.on_callback_query(filters.regex(r"^u:ver:"))
async def user_verify(cl, q):
    oid = q.data.split(":")[2]
    o = await C.db.orders.find_one({"_id": oid})
    if not o or o["uid"] != q.from_user.id:
        return await q.answer("Order nahi mila", show_alert=True)
    if o["status"] == "paid":
        return await q.answer("Already verified ✅", show_alert=True)
    if o["status"] != "pending":
        return await q.answer("Yeh order band ho chuka hai.", show_alert=True)
    if time.time() - o["ts"] > PAY_WINDOW:
        await C.db.orders.update_one({"_id": oid}, {"$set": {"status": "expired"}})
        await q.answer()
        return await q.message.edit_caption("⌛ Order expire ho gaya. Link se dobara try karo.")
    earlier = await C.db.orders.find_one({"mode": "auto", "status": "pending", "amount": o["amount"],
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
        await log(f"⚠️ IMAP error on verify (order {oid}): {e}")
        return await app.send_message(q.from_user.id, "⚠️ Verification mein dikkat aayi, thodi der baad try karo.")
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
            await q.message.delete()
            await log(f"💰 Auto payment verified\nOrder {oid} | ₹{fmt(o['amount'])} | UTR/ID {key}\nUser: {o['uid']}")
            return await deliver(o["uid"], o["item"])
    await log(f"❌ Verify failed (not found): order {oid}, ₹{fmt(o['amount'])}, mails checked: {len(hits)}")
    await app.send_message(q.from_user.id, "❌ Payment abhi nahi mila. Pay karke 1 minute baad dobara Verify dabao.")


# ---------------- manual payment ----------------
@app.on_callback_query(filters.regex(r"^u:man:"))
async def user_manual(cl, q):
    c = await cfg()
    if not c["manual_on"] or not c["upi_id"]:
        return await q.answer("Manual payment abhi available nahi hai.", show_alert=True)
    it = await C.db.items.find_one({"_id": q.data.split(":")[2]})
    if not it:
        return await q.answer("Product nahi mila", show_alert=True)
    await q.answer()
    uid = q.from_user.id
    oid = secrets.token_hex(4)
    await C.db.orders.insert_one({"_id": oid, "uid": uid, "item": it["_id"], "amount": it["price"],
                                  "mode": "manual", "status": "awaiting", "ts": time.time()})
    state[uid] = {"step": "shot", "oid": oid}
    await app.send_photo(uid, make_qr(upi_link(c, float(it["price"]), oid)), caption=(
        f"🧾 **Manual Payment**\n📦 {it['title']}\n💰 Amount: **₹{fmt(it['price'])}**\n"
        f"🏦 UPI: `{c['upi_id']}`\n\nPay karke payment ka **screenshot** yahin bhejo."))


@app.on_message(filters.private & ~filters.command(CMDS), group=2)
async def shot(cl, m):
    uid = m.from_user.id
    st = state.get(uid)
    if not st or st["step"] != "shot":
        return
    if not m.photo:
        return await m.reply("📸 Payment ka screenshot (photo) bhejo.")
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
    await m.reply("⏳ Screenshot admin ko bhej diya. Approve hote hi file mil jayegi.")


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
        await app.send_message(o["uid"], "❌ Aapka payment reject ho gaya. Sahi screenshot ke saath dobara try karo.")
