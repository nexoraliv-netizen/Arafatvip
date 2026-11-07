import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

from aiohttp import web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GROUP_ID = int(os.environ["GROUP_ID"])
AFFILIATE_LINK = os.environ["AFFILIATE_LINK"]  # may contain {tg_id}
POSTBACK_SECRET = os.environ["POSTBACK_SECRET"]
PORT = int(os.getenv("PORT", "8080"))
UID_PARAM = os.getenv("UID_PARAM", "uid")
CLICK_PARAM = os.getenv("CLICK_PARAM", "click_id")
AMOUNT_PARAM = os.getenv("AMOUNT_PARAM", "amount")
EVENT_PARAM = os.getenv("EVENT_PARAM", "event")
INVITE_TTL_HOURS = int(os.getenv("INVITE_TTL_HOURS", "24"))
DEFAULT_MIN_DEPOSIT = float(os.getenv("MIN_DEPOSIT", "10"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

UID_RE = re.compile(r"^[A-Za-z0-9]{3,20}$")

# ---------- DB ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS traders(
    uid TEXT PRIMARY KEY, tg_id INTEGER, registered_ts INTEGER, claimed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT, type TEXT,
    amount REAL DEFAULT 0, ts INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
)
db.commit()


def get_min() -> float:
    r = db.execute("SELECT value FROM settings WHERE key='min_deposit'").fetchone()
    return float(r[0]) if r else DEFAULT_MIN_DEPOSIT


def set_min(v: float):
    db.execute("INSERT OR REPLACE INTO settings VALUES('min_deposit', ?)", (str(v),))
    db.commit()


def total(uid: str, typ: str) -> float:
    r = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM events WHERE uid=? AND type=?", (uid, typ)
    ).fetchone()
    return float(r[0])


def norm_event(name: str) -> str:
    n = (name or "").lower()
    if "withdraw" in n or "payout" in n:
        return "withdrawal"
    if "deposit" in n or "ftd" in n or "redeposit" in n:
        return "deposit"
    if "reg" in n or "signup" in n or "lead" in n:
        return "registration"
    return n or "registration"


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


def affiliate_url(tg_id: int) -> str:
    return AFFILIATE_LINK.replace("{tg_id}", str(tg_id))


async def grant_access(bot, tg_id: int, uid: str) -> bool:
    """Atomic: gives the invite link only once per UID."""
    res = db.execute(
        "UPDATE traders SET claimed=1, tg_id=? WHERE uid=? AND claimed=0", (tg_id, uid)
    )
    db.commit()
    if not res.rowcount:
        return False
    link = await bot.create_chat_invite_link(
        chat_id=GROUP_ID,
        member_limit=1,
        expire_date=int(time.time()) + INVITE_TTL_HOURS * 3600,
        name=f"{uid}",
    )
    await bot.send_message(
        tg_id,
        f"✅ ভেরিফিকেশন সম্পন্ন!\n\nপ্রাইভেট গ্রুপে জয়েন করুন:\n{link.invite_link}\n\n"
        f"⚠️ লিঙ্কটি একবারই ব্যবহার করা যাবে এবং {INVITE_TTL_HOURS} ঘণ্টা পর মেয়াদ শেষ।",
    )
    return True


def status_text(uid: str) -> str:
    dep = total(uid, "deposit")
    need = get_min()
    return (
        f"📌 UID: {uid}\n💰 আপনার মোট ডিপোজিট: ${dep:.2f}\n"
        f"🎯 গ্রুপে ঢুকতে ন্যূনতম: ${need:.2f}\n"
        f"➡️ বাকি আছে: ${max(need - dep, 0):.2f}"
    )


CHECK_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🔄 আবার চেক করুন", callback_data="check")]]
)


async def evaluate(bot, tg_id: int, uid: str, silent_if_waiting=False):
    """Grant if eligible, otherwise tell user what is missing."""
    if total(uid, "deposit") >= get_min():
        ok = await grant_access(bot, tg_id, uid)
        if not ok:
            await bot.send_message(
                tg_id, "ℹ️ এই UID-এর লিঙ্ক ইতোমধ্যে দেওয়া হয়েছে। সমস্যা হলে অ্যাডমিনের সাথে যোগাযোগ করুন।"
            )
        return
    if not silent_if_waiting:
        await bot.send_message(
            tg_id,
            "⏳ রেজিস্ট্রেশন পাওয়া গেছে, কিন্তু ডিপোজিট এখনো যথেষ্ট নয়।\n\n"
            + status_text(uid),
            reply_markup=CHECK_KB,
        )


# ---------- User handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 অ্যাকাউন্ট খুলুন", url=affiliate_url(tg_id))],
            [InlineKeyboardButton("✅ আমি অ্যাকাউন্ট খুলেছি", callback_data="registered")],
        ]
    )
    await update.message.reply_text(
        "স্বাগতম! 👋\n\n"
        f"১) নিচের লিঙ্ক দিয়ে অ্যাকাউন্ট খুলুন\n"
        f"২) ন্যূনতম ${get_min():.0f} ডিপোজিট করুন\n"
        "৩) তারপর প্রাইভেট গ্রুপের লিঙ্ক পাবেন",
        reply_markup=kb,
    )


async def on_registered(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["awaiting_uid"] = True
    await q.message.reply_text("আপনার Quotex UID পাঠান:")


async def on_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg_id = q.from_user.id
    r = db.execute("SELECT uid FROM traders WHERE tg_id=?", (tg_id,)).fetchone()
    if not r:
        ctx.user_data["awaiting_uid"] = True
        await q.message.reply_text("আপনার Quotex UID পাঠান:")
        return
    await evaluate(ctx.bot, tg_id, r[0])


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_uid"):
        return
    tg_id = update.effective_user.id
    uid = update.message.text.strip()
    if not UID_RE.match(uid):
        await update.message.reply_text("❌ UID ফরম্যাট ঠিক নেই। আবার পাঠান।")
        return
    row = db.execute("SELECT tg_id FROM traders WHERE uid=?", (uid,)).fetchone()
    if not row:
        await update.message.reply_text(
            "⏳ এই UID এখনো আমাদের লিস্টে নেই।\n"
            "• আমার লিঙ্ক দিয়েই অ্যাকাউন্ট খুলেছেন কিনা নিশ্চিত করুন\n"
            "• রেজিস্ট্রেশনের পর কয়েক মিনিট লাগতে পারে — পরে আবার চেষ্টা করুন",
            reply_markup=CHECK_KB if False else None,
        )
        return
    if row[0] not in (None, tg_id):
        await update.message.reply_text("❌ এই UID অন্য একজন ব্যবহার করছেন।")
        return
    db.execute("UPDATE traders SET tg_id=? WHERE uid=?", (tg_id, uid))
    db.commit()
    ctx.user_data["awaiting_uid"] = False
    await evaluate(ctx.bot, tg_id, uid)


# ---------- Admin commands ----------
def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            return
        await fn(update, ctx)

    return wrapper


@admin_only
async def cmd_stats(update, ctx):
    n = db.execute("SELECT COUNT(*) FROM traders").fetchone()[0]
    c = db.execute("SELECT COUNT(*) FROM traders WHERE claimed=1").fetchone()[0]
    d = db.execute("SELECT COALESCE(SUM(amount),0) FROM events WHERE type='deposit'").fetchone()[0]
    w = db.execute("SELECT COALESCE(SUM(amount),0) FROM events WHERE type='withdrawal'").fetchone()[0]
    await update.message.reply_text(
        f"📊 মোট রেজিস্ট্রেশন: {n}\n✅ গ্রুপে অ্যাক্সেস: {c}\n"
        f"💰 মোট ডিপোজিট: ${d:.2f}\n💸 মোট উইথড্র: ${w:.2f}\n"
        f"🎯 বর্তমান ন্যূনতম ডিপোজিট: ${get_min():.2f}"
    )


@admin_only
async def cmd_setmin(update, ctx):
    try:
        v = float(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("ব্যবহার: /setmin 20")
        return
    set_min(v)
    await update.message.reply_text(f"✅ ন্যূনতম ডিপোজিট এখন ${v:.2f}")


async def _history(update, ctx, typ: str, title: str):
    uid = ctx.args[0] if ctx.args else None
    q = "SELECT uid, amount, ts FROM events WHERE type=?"
    p = [typ]
    if uid:
        q += " AND uid=?"
        p.append(uid)
    rows = db.execute(q + " ORDER BY ts DESC LIMIT 30", p).fetchall()
    if not rows:
        await update.message.reply_text("কোনো ডাটা নেই।")
        return
    lines = [f"{title} (সর্বশেষ {len(rows)}টি)"]
    lines += [f"• {u} — ${a:.2f} — {fmt_ts(t)}" for u, a, t in rows]
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_deposits(update, ctx):
    await _history(update, ctx, "deposit", "💰 ডিপোজিট হিস্টোরি")


@admin_only
async def cmd_withdrawals(update, ctx):
    await _history(update, ctx, "withdrawal", "💸 উইথড্র হিস্টোরি")


@admin_only
async def cmd_user(update, ctx):
    if not ctx.args:
        await update.message.reply_text("ব্যবহার: /user <uid>")
        return
    uid = ctx.args[0]
    t = db.execute(
        "SELECT tg_id, registered_ts, claimed FROM traders WHERE uid=?", (uid,)
    ).fetchone()
    if not t:
        await update.message.reply_text("এই UID পাওয়া যায়নি।")
        return
    await update.message.reply_text(
        f"👤 UID: {uid}\nTelegram ID: {t[0] or '—'}\nরেজিস্টার্ড: {fmt_ts(t[1])}\n"
        f"💰 মোট ডিপোজিট: ${total(uid,'deposit'):.2f}\n"
        f"💸 মোট উইথড্র: ${total(uid,'withdrawal'):.2f}\n"
        f"গ্রুপ লিঙ্ক দেওয়া হয়েছে: {'হ্যাঁ' if t[2] else 'না'}"
    )


@admin_only
async def cmd_add_deposit(update, ctx):
    """Manual fallback: /add_deposit <uid> <amount>"""
    try:
        uid, amt = ctx.args[0], float(ctx.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("ব্যবহার: /add_deposit <uid> <amount>")
        return
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO traders(uid, registered_ts) VALUES(?,?)", (uid, now))
    db.execute(
        "INSERT INTO events(uid,type,amount,ts,raw) VALUES(?,?,?,?,?)",
        (uid, "deposit", amt, now, "manual"),
    )
    db.commit()
    await update.message.reply_text(f"✅ {uid}-এ ${amt:.2f} ডিপোজিট যোগ হয়েছে।")
    t = db.execute("SELECT tg_id FROM traders WHERE uid=?", (uid,)).fetchone()
    if t and t[0]:
        await evaluate(ctx.bot, t[0], uid, silent_if_waiting=True)


@admin_only
async def cmd_resend(update, ctx):
    """/resend <uid> — reset claim so the user can get a fresh link."""
    if not ctx.args:
        return
    db.execute("UPDATE traders SET claimed=0 WHERE uid=?", (ctx.args[0],))
    db.commit()
    await update.message.reply_text("✅ রিসেট হয়েছে। ইউজার আবার 'চেক করুন' চাপলে নতুন লিঙ্ক পাবে।")


# ---------- Postback server ----------
async def postback(request: web.Request):
    if request.match_info["secret"] != POSTBACK_SECRET:
        return web.Response(status=403, text="forbidden")
    params = dict(request.query)
    if request.method == "POST":
        params.update(await request.post())
    log.info("postback: %s", params)

    uid = (params.get(UID_PARAM) or "").strip()
    if not uid:
        return web.Response(status=400, text="missing uid")
    click_id = (params.get(CLICK_PARAM) or "").strip()
    typ = norm_event(params.get(EVENT_PARAM))
    try:
        amount = float(params.get(AMOUNT_PARAM) or 0)
    except ValueError:
        amount = 0.0
    now = int(time.time())
    tg_id = int(click_id) if click_id.isdigit() else None

    db.execute(
        "INSERT OR IGNORE INTO traders(uid, tg_id, registered_ts) VALUES(?,?,?)",
        (uid, tg_id, now),
    )
    if tg_id:
        db.execute("UPDATE traders SET tg_id=? WHERE uid=? AND tg_id IS NULL", (tg_id, uid))
    db.execute(
        "INSERT INTO events(uid,type,amount,ts,raw) VALUES(?,?,?,?,?)",
        (uid, typ, amount, now, str(params)),
    )
    db.commit()

    bot = request.app["tg"].bot
    row = db.execute("SELECT tg_id FROM traders WHERE uid=?", (uid,)).fetchone()
    user = row[0] if row else None
    if user and typ in ("registration", "deposit"):
        try:
            if typ == "registration":
                await bot.send_message(
                    user,
                    f"🎉 রেজিস্ট্রেশন কনফার্ম!\nএখন ন্যূনতম ${get_min():.0f} ডিপোজিট করুন, "
                    "তারপর গ্রুপের লিঙ্ক অটো পেয়ে যাবেন।",
                    reply_markup=CHECK_KB,
                )
            else:
                await evaluate(bot, user, uid)
        except Exception:
            log.exception("notify failed for %s", user)
    return web.Response(text="OK")


async def main():
    tg = Application.builder().token(BOT_TOKEN).build()
    tg.add_handler(CommandHandler("start", start))
    tg.add_handler(CommandHandler("stats", cmd_stats))
    tg.add_handler(CommandHandler("setmin", cmd_setmin))
    tg.add_handler(CommandHandler("deposits", cmd_deposits))
    tg.add_handler(CommandHandler("withdrawals", cmd_withdrawals))
    tg.add_handler(CommandHandler("user", cmd_user))
    tg.add_handler(CommandHandler("add_deposit", cmd_add_deposit))
    tg.add_handler(CommandHandler("resend", cmd_resend))
    tg.add_handler(CallbackQueryHandler(on_registered, pattern="^registered$"))
    tg.add_handler(CallbackQueryHandler(on_check, pattern="^check$"))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    web_app = web.Application()
    web_app["tg"] = tg
    web_app.add_routes(
        [
            web.get("/postback/{secret}", postback),
            web.post("/postback/{secret}", postback),
            web.get("/health", lambda r: web.Response(text="ok")),
        ]
    )
    runner = web.AppRunner(web_app)
    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling()
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Bot running on port %s", PORT)
    try:
        await asyncio.Event().wait()
    finally:
        await tg.updater.stop()
        await tg.stop()
        await tg.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
