"""Admin panel (/admin): settings buttons + step-by-step inputs."""
import asyncio, time
from pyrogram import filters, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup as IKM, InlineKeyboardButton as IKB
import core as C
from core import app, state, CMDS, OWNER_ID, fernet, cfg, setcfg, is_admin, tick, log, ist
from gateway import test_inbox, test_login

NOPARSE = enums.ParseMode.DISABLED


async def panel():
    c = await cfg()
    text = (
        "⚙️ **Admin Panel**\n\n"
        f"💳 Auto Payment: {tick(c['auto_on'])}\n"
        f"🛠 Under Maintenance: {tick(c['maint'])}\n"
        f"🧾 Manual Payment: {tick(c['manual_on'])}\n"
        f"📧 Gmail: {c['gmail'] or 'not set'} {tick(bool(c['app_pw']))}\n"
        f"💸 UPI: {c['upi_id'] or 'not set'}\n"
        f"📢 Log channel: {tick(bool(c['log_chat']))}\n"
        f"👤 Admins: {len(c['admins'])}\n"
        f"🔄 Last restart: {ist(c['last_start']) if c['last_start'] else '-'}"
    )
    kb = IKM([
        [IKB(f"⚡ Auto Payment: {tick(c['auto_on'])}", callback_data="a:auto")],
        [IKB(f"🛠 Under Maintenance: {tick(c['maint'])}", callback_data="a:maint")],
        [IKB(f"🧾 Manual Payment: {tick(c['manual_on'])}", callback_data="a:manual")],
        [IKB("📧 Gmail + App Password", callback_data="a:gmail")],
        [IKB("🧪 Test Gmail (Inbox Read)", callback_data="a:test")],
        [IKB("💸 UPI ID / Name", callback_data="a:upi")],
        [IKB("🖼 Welcome Photo", callback_data="a:wphoto"), IKB("📝 Welcome Text", callback_data="a:wtext")],
        [IKB("➕ Add Files", callback_data="a:add"), IKB("📦 Products", callback_data="a:items")],
        [IKB("📢 Log Channel", callback_data="a:logchat"), IKB("📜 Recent Logs", callback_data="a:logs")],
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


PROMPTS = {
    "upi": ("upi", "💸 Bhejo: `upi@bank | Naam`"),
    "gmail": ("gmail", "📧 Apna Gmail address bhejo."),
    "wphoto": ("wphoto", "🖼 Welcome message ke liye **photo** bhejo.\n\nPhoto hatane ke liye `0` bhejo."),
    "wtext": ("wtext", "📝 Naya welcome text bhejo. User ka naam dikhane ke liye `{name}` likho. "
                       "Bold ke liye `**text**`.\n\nDefault par wapas jaane ke liye `0` bhejo."),
    "addadmin": ("addadmin", "👤 Naye admin ki Telegram numeric ID bhejo."),
    "rmadmin": ("rmadmin", "🗑 Hatane wale admin ki numeric ID bhejo."),
    "logchat": ("logchat", "📢 Log channel set karne ke liye:\n1) Bot ko channel mein **admin** banao\n"
                           "2) Channel ka koi message yahan **forward** karo, ya channel ID bhejo "
                           "(jaise `-1001234567890`)\n\nBand karne ke liye `0` bhejo."),
}


@app.on_callback_query(filters.regex(r"^a:(auto|maint|manual|upi|gmail|wphoto|wtext|addadmin|rmadmin|test|logs|logchat)$"))
async def admin_cb(cl, q):
    uid = q.from_user.id
    if not await is_admin(uid):
        return await q.answer("Not allowed", show_alert=True)
    act = q.data.split(":")[1]
    c = await cfg()

    if act == "auto":
        if not c["auto_on"] and not (c["gmail"] and c["app_pw"] and c["upi_id"]):
            return await q.answer("Pehle Gmail+App Password aur UPI set karo.", show_alert=True)
        await setcfg(auto_on=not c["auto_on"])
        await q.answer()
        await log(f"⚙️ Auto payment {'ON' if not c['auto_on'] else 'OFF'} by {uid}")
        return await refresh_panel(q)
    if act == "maint":
        await setcfg(maint=not c["maint"])
        await q.answer()
        await log(f"⚙️ Maintenance {'ON' if not c['maint'] else 'OFF'} by {uid}")
        return await refresh_panel(q)
    if act == "manual":
        await setcfg(manual_on=not c["manual_on"])
        await q.answer()
        return await refresh_panel(q)

    if act == "test":
        if not (c["gmail"] and c["app_pw"]):
            return await q.answer("Pehle Gmail + App Password set karo.", show_alert=True)
        await q.answer("🔎 Inbox padh raha hoon...")
        try:
            res = await asyncio.to_thread(test_inbox, c)
        except Exception as e:
            res = (f"🧪 Gmail Test\n❌ Fail: {e}\n\nCheck karo:\n• Gmail Settings → Forwarding and POP/IMAP → IMAP Enable\n"
                   "• 2-Step Verification ON ho aur naya App Password banao\n• Gmail address sahi ho")
        return await app.send_message(uid, res[:4000], parse_mode=NOPARSE)

    if act == "logs":
        await q.answer()
        rows = await C.db.logs.find().sort("ts", -1).to_list(15)
        if not rows:
            return await app.send_message(uid, "Abhi koi log nahi hai.")
        txt = "📜 Recent Logs\n\n" + "\n\n".join(f"🕒 {ist(r['ts'])}\n{r['text']}" for r in rows)
        return await app.send_message(uid, txt[:4000], parse_mode=NOPARSE)

    if act in ("addadmin", "rmadmin") and uid != OWNER_ID:
        return await q.answer("Sirf owner kar sakta hai.", show_alert=True)
    step, prompt = PROMPTS[act]
    state[uid] = {"step": step}
    await q.answer()
    await q.message.reply(prompt + "\n\n(/cancel se band karo)")


@app.on_message(filters.private & ~filters.command(CMDS), group=1)
async def admin_flow(cl, m):
    uid = m.from_user.id
    st = state.get(uid)
    if not st or st["step"] not in PROMPTS and st["step"] != "apppw":
        return
    if not await is_admin(uid):
        return
    step, txt = st["step"], (m.text or "").strip()

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
            return await app.send_message(uid, f"❌ Login fail: {e}\nIMAP enable aur App Password check karo. /admin",
                                          parse_mode=NOPARSE)
        await setcfg(gmail=st["gmail"], app_pw=fernet.encrypt(pw.encode()).decode())
        state.pop(uid)
        return await app.send_message(uid, "✅ Gmail login test pass, save ho gaya. Ab 🧪 Test Gmail dabake inbox check karo. /admin")
    if step == "wphoto":
        if m.photo:
            await setcfg(welcome_photo=m.photo.file_id)
        elif txt == "0":
            await setcfg(welcome_photo="")
        else:
            return await m.reply("Photo bhejo (ya hatane ke liye 0).")
        state.pop(uid)
        return await m.reply("✅ Welcome photo update ho gaya. /start se preview dekho.")
    if step == "wtext":
        if not m.text:
            return await m.reply("Text bhejo.")
        await setcfg(welcome_text="" if txt == "0" else m.text.markdown)
        state.pop(uid)
        return await m.reply("✅ Welcome text update ho gaya. /start se preview dekho.")
    if step in ("addadmin", "rmadmin"):
        if not txt.isdigit():
            return await m.reply("Sirf numeric ID bhejo.")
        c = await cfg()
        ad = set(c["admins"])
        ad.add(int(txt)) if step == "addadmin" else ad.discard(int(txt))
        await setcfg(admins=list(ad))
        state.pop(uid)
        return await m.reply("✅ Done. /admin")
    if step == "logchat":
        chat = getattr(m, "forward_from_chat", None)
        if not chat:
            chat = getattr(getattr(m, "forward_origin", None), "chat", None)
        if chat:
            cid = chat.id
        elif txt.lstrip("-").isdigit():
            cid = int(txt)
        else:
            return await m.reply("Channel ka message forward karo ya numeric ID bhejo (band karne ke liye 0).")
        if cid == 0:
            await setcfg(log_chat=0)
            state.pop(uid)
            return await m.reply("✅ Log channel band kar diya. /admin")
        try:
            await app.send_message(cid, "✅ Log channel connected. Ab yahan bot ke logs aayenge.")
        except Exception as e:
            return await m.reply(f"❌ Channel mein message nahi bhej paya: {e}\n"
                                 "Bot ko channel mein admin banao aur dobara try karo.", parse_mode=NOPARSE)
        await setcfg(log_chat=cid)
        state.pop(uid)
        await m.reply("✅ Log channel set ho gaya. /admin")
