"""Products / files: save, link, delivery, /start storefront."""
import secrets
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
import core as C
from core import app, state, CMDS, cfg, is_admin, fmt, fid, log, user_line, sc, dev_row


async def deliver(uid, item_id):
    it = await C.db.items.find_one({"_id": item_id})
    if not it:
        return await app.send_message(uid, "⚠️ " + sc("product not found. please contact the admin."))
    await app.send_message(uid, "✅ " + sc("payment verified! sending your files..."))
    for f in it["files"]:
        try:
            await app.send_cached_media(uid, f)
        except Exception as e:
            print("send error", e)
            await log(f"⚠️ File send error to {uid}: {e}")
    await app.send_message(uid, "🎉 " + sc("thank you for your purchase! enjoy."),
                           reply_markup=IKM([dev_row()]))


DEFAULT_WELCOME = (
    "✨ " + sc("welcome") + " {name} ✨\n\n"
    "📚 **" + sc("extra episode store") + "**\n"
    + sc("this bot is perfect for selling extra episodes of your favourite stories.") + "\n\n"
    "⚡ " + sc("instant auto verification") + "\n"
    "🔒 " + sc("safe & secure upi payments") + "\n"
    "📩 " + sc("instant file delivery") + "\n\n"
    "👇 " + sc("open a link shared by the owner to get started.")
)


async def send_welcome(m):
    c = await cfg()
    name = m.from_user.first_name or "friend"
    text = (c["welcome_text"] or DEFAULT_WELCOME).replace("{name}", name)
    kb = IKM([dev_row()])
    if c["welcome_photo"]:
        try:
            if len(text) <= 1000:
                return await m.reply_photo(c["welcome_photo"], caption=text, reply_markup=kb)
            await m.reply_photo(c["welcome_photo"])
        except Exception as e:
            print("welcome photo error", e)
    await m.reply(text, reply_markup=kb)


@app.on_message(filters.command("start") & filters.private)
async def start(cl, m):
    u = m.from_user
    arg = m.command[1] if len(m.command) > 1 else ""
    r = await C.db.users.update_one({"_id": u.id}, {"$set": {"name": user_line(u)}}, upsert=True)
    if r.upserted_id is not None:  # log sirf pehli baar (naya user register hone par)
        await log("🆕 New user registered\n" + user_line(u) + (f"\nLink: {arg}" if arg else ""))
    if arg.startswith("item_"):
        it = await C.db.items.find_one({"_id": arg[5:]})
        if not it:
            return await m.reply("❌ " + sc("this link is invalid or expired."))
        kb = IKM([[IKB("⚡ " + sc("automatic"), callback_data=f"u:auto:{it['_id']}"),
                   IKB("🧾 " + sc("manual"), callback_data=f"u:man:{it['_id']}")], dev_row()])
        return await m.reply(
            "✦ " + sc("extra episode sharing") + " ✦\n\n"
            f"📦 **{it['title']}**\n"
            f"💰 {sc('price')}: **₹{fmt(it['price'])}**\n"
            f"📁 {sc('files')}: {len(it['files'])}\n\n"
            + sc("share extra episodes of any story — get any link & channel access, delivered instantly.")
            + "\n\n" + sc("choose your payment method") + " 👇", reply_markup=kb)
    await send_welcome(m)


@app.on_message(filters.command("done") & filters.private)
async def done_cmd(cl, m):
    st = state.get(m.from_user.id)
    if not st or st["step"] != "files" or not st["files"]:
        return await m.reply("Pehle kam se kam ek file bhejo.")
    st["step"] = "price"
    await m.reply("💰 Price kitna rakhna hai? (₹, sirf number)")


@app.on_callback_query(filters.regex(r"^a:(add|items|del:.+)$"))
async def file_cb(cl, q):
    uid = q.from_user.id
    if not await is_admin(uid):
        return await q.answer("Not allowed", show_alert=True)
    act = q.data.split(":")[1]
    if act == "add":
        state[uid] = {"step": "files", "files": []}
        await q.answer()
        return await q.message.reply("📁 Ek ya kai files bhejo. Ho jaye to /done likho. (/cancel se band)")
    if act == "items":
        await q.answer()
        items = await C.db.items.find().sort("_id", -1).to_list(20)
        if not items:
            return await q.message.reply("Koi product nahi hai.")
        txt = "\n".join(f"• {i['title']} — ₹{fmt(i['price'])} ({len(i['files'])} files)\n"
                        f"  https://t.me/{C.BOT_USERNAME}?start=item_{i['_id']}" for i in items)
        kb = IKM([[IKB(f"🗑 {i['title'][:30]}", callback_data=f"a:del:{i['_id']}")] for i in items])
        return await q.message.reply(txt, reply_markup=kb, disable_web_page_preview=True)
    if act == "del":
        await C.db.items.delete_one({"_id": q.data.split(":")[2]})
        await q.answer("Deleted ✅", show_alert=True)
        await q.message.delete()


@app.on_message(filters.private & ~filters.command(CMDS), group=3)
async def file_flow(cl, m):
    uid = m.from_user.id
    st = state.get(uid)
    if not st or st["step"] not in ("files", "price", "title") or not await is_admin(uid):
        return
    step, txt = st["step"], (m.text or "").strip()
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
        await C.db.items.insert_one({"_id": iid, "title": txt, "price": st["price"], "files": st["files"]})
        state.pop(uid, None)
        await log(f"📦 Product added: {txt} (₹{fmt(st['price'])}, {len(st['files'])} files) by {uid}")
        await m.reply(f"✅ Product ban gaya!\n\n🔗 https://t.me/{C.BOT_USERNAME}?start=item_{iid}",
                      disable_web_page_preview=True)
