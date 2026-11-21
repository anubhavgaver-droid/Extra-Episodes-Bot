"""Web server: Mini App (/app/<item>), UPI app chooser (/pay/<order>) and their JSON API."""
import os, json, time, hmac, hashlib, asyncio
from urllib.parse import parse_qsl
from aiohttp import web
import core as C
from core import BOT_TOKEN, DEV, PAY_WINDOW, cfg, log
import gateway as G
from file import deliver

PAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payment")
NOSTORE = {"Cache-Control": "no-store"}


def j(data, status=200):
    return web.json_response(data, status=status, headers=NOSTORE)


def init_user(init_data):
    """Telegram Mini App initData verify karo (HMAC). Valid ho to user dict, warna None."""
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        got = parsed.pop("hash", None)
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not got or not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(parsed.get("auth_date", 0)) > 86400:
            return None
        return json.loads(parsed["user"])
    except Exception:
        return None


async def body_of(req):
    try:
        return await req.json()
    except Exception:
        return {}


async def order_json(o):
    it = await C.db.items.find_one({"_id": o["item"]}) or {}
    c = await cfg()
    return {"oid": o["_id"], "title": it.get("title", ""), "amount": float(o["amount"]),
            "upi_id": c["upi_id"], "upi_name": c["upi_name"], "status": o["status"],
            "left": max(0, int(o["ts"] + PAY_WINDOW - time.time())), "bot": C.BOT_USERNAME}


# ---------------- pages ----------------
async def health(_):
    return web.Response(text="ok")


async def page_app(req):
    return web.FileResponse(os.path.join(PAY_DIR, "app.html"), headers=NOSTORE)


async def page_pay(req):
    return web.FileResponse(os.path.join(PAY_DIR, "pay.html"), headers=NOSTORE)


# ---------------- api ----------------
async def api_item(req):
    it = await C.db.items.find_one({"_id": req.match_info["item"]})
    if not it:
        return j({"error": "This link is invalid or expired."}, 404)
    c = await cfg()
    return j({"id": it["_id"], "title": it["title"], "desc": it.get("desc", ""), "price": it["price"],
              "files": len(it["files"]),
              "auto": bool(c["auto_on"] and not c["maint"] and c["upi_id"]),
              "manual": bool(c["manual_on"] and c["upi_id"]),
              "bot": C.BOT_USERNAME, "dev": DEV})


async def api_order_get(req):
    o = await C.db.orders.find_one({"_id": req.match_info["oid"], "mode": "auto"})
    if not o:
        return j({"error": "Order not found."}, 404)
    return j(await order_json(o))


async def api_qr(req):
    o = await C.db.orders.find_one({"_id": req.match_info["oid"], "mode": "auto"})
    if not o:
        return web.Response(status=404)
    c = await cfg()
    png = G.make_qr(G.upi_link(c, float(o["amount"]), o["_id"])).getvalue()
    return web.Response(body=png, content_type="image/png", headers=NOSTORE)


async def api_order_create(req):
    b = await body_of(req)
    user = init_user(b.get("initData", ""))
    if not user:
        return j({"error": "Please open this store from inside Telegram."}, 401)
    it = await C.db.items.find_one({"_id": b.get("item", "")})
    if not it:
        return j({"error": "This link is invalid or expired."}, 404)
    c = await cfg()
    if c["maint"] or not c["auto_on"] or not c["upi_id"]:
        return j({"error": "Automatic payment is under maintenance."}, 403)
    o = await C.db.orders.find_one({"uid": user["id"], "item": it["_id"], "mode": "auto", "status": "pending",
                                    "ts": {"$gt": time.time() - PAY_WINDOW}})
    if not o:
        oid, _ = await G.create_auto_order(user["id"], it)
        o = await C.db.orders.find_one({"_id": oid})
        await log(f"🧾 Auto order {oid} (mini app): {user.get('first_name', '')} [{user['id']}] → "
                  f"{it['title']} ₹{o['amount']:g}")
    return j(await order_json(o))


async def api_verify(req):
    b = await body_of(req)
    user = init_user(b.get("initData", ""))
    if not user:
        return j({"status": "error", "error": "Please open this store from inside Telegram."}, 401)
    o = await C.db.orders.find_one({"_id": b.get("oid", "")})
    if not o or o["uid"] != user["id"]:
        return j({"status": "error", "error": "Order not found."}, 404)
    st = await G.precheck(o)
    if st:
        return j({"status": st})
    oid = o["_id"]
    if oid in G.verifying:
        return j({"status": "busy"})
    G.verifying.add(oid)
    try:
        c = await cfg()
        try:
            hits = await asyncio.wait_for(asyncio.to_thread(G.check_mail, c, o["ts"]), 30)
        except Exception as e:
            print("imap error", e)
            await log(f"⚠️ IMAP error on verify (mini app, order {oid}): {e}")
            return j({"status": "error", "error": "Verification error. Please try again."})
        if await G.claim_payment(o, hits):
            await deliver(o["uid"], o["item"])
            return j({"status": "paid"})
        return j({"status": "pending"})
    finally:
        G.verifying.discard(oid)


def make_app():
    a = web.Application()
    a.router.add_get("/", health)
    a.router.add_get("/app/{item}", page_app)
    a.router.add_get("/pay/{oid}", page_pay)
    a.router.add_get("/api/item/{item}", api_item)
    a.router.add_get("/api/order/{oid}", api_order_get)
    a.router.add_get("/api/qr/{oid}", api_qr)
    a.router.add_post("/api/order", api_order_create)
    a.router.add_post("/api/verify", api_verify)
    return a
