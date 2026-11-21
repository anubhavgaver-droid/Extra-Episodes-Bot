"""Startup + web server (Render). Run: python main.py"""
import os, time, asyncio
from aiohttp import web, ClientSession, ClientTimeout
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import idle
import core as C
from core import app, ist, cfg, setcfg, log
import admin, gateway, file, webapp  # noqa: F401  (handlers register ho jate hain)


async def keep_alive():
    # Render free tier 15 min idle par sleep ho jata hai; yeh khud ko har 10 min ping karta hai.
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(600)
        try:
            async with ClientSession() as s:
                await s.get(url, timeout=ClientTimeout(total=20))
        except Exception as e:
            print("keep-alive error", e)


async def main():
    C.db = AsyncIOMotorClient(C.MONGO_URI)["paybot"]
    await app.start()
    me = await app.get_me()
    C.BOT_USERNAME = me.username or ""
    if not C.BOT_USERNAME:  # purane Pyrogram par kabhi username None aata hai
        try:
            async with ClientSession() as s:
                async with s.get(f"https://api.telegram.org/bot{C.BOT_TOKEN}/getMe") as resp:
                    C.BOT_USERNAME = (await resp.json())["result"].get("username", "")
        except Exception as e:
            print("getMe error", e)
    C.BOT_USERNAME = C.BOT_USERNAME or os.environ.get("BOT_USERNAME", "")

    port = int(os.environ.get("PORT", 10000))
    r = web.AppRunner(webapp.make_app())
    await r.setup()
    await web.TCPSite(r, "0.0.0.0", port).start()
    asyncio.create_task(keep_alive())

    # restart log (IST) + purane logs 30 din baad delete
    prev = (await cfg())["last_start"]
    await setcfg(last_start=time.time())
    await C.db.logs.delete_many({"ts": {"$lt": time.time() - 30 * 86400}})
    await log(f"🔄 Bot restarted\nNow: {ist()}\nPrevious start: {ist(prev) if prev else 'first run'}")
    print(f"Bot started as @{C.BOT_USERNAME} | port {port}")
    await idle()
    await app.stop()


app.run(main())
