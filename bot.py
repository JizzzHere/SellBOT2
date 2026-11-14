"""
ربات فروش VPN — نسخه ۲
کیف پول • خرید آنی • اکانت تست • دعوت دوستان • کد تخفیف • پنل مدیریت داخل ربات
"""
import asyncio
import html
import io
import json
import logging
import os
import secrets
import time
import uuid

import aiohttp
import aiosqlite
import qrcode
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

VERSION = "2.0"

# ───────────────────────── تنظیمات (از Variables) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
BRAND = os.getenv("BRAND_NAME", "VPN Shop")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
CARD_NUMBER = os.getenv("CARD_NUMBER", "6037-0000-0000-0000")
CARD_HOLDER = os.getenv("CARD_HOLDER", "نام صاحب کارت")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "").strip()  # مثلا @mychannel (خالی = غیرفعال)
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "10000"))  # تومان
TEST_GB = float(os.getenv("TEST_GB", "1"))
TEST_DAYS = float(os.getenv("TEST_DAYS", "1"))
MIN_TOPUP = int(os.getenv("MIN_TOPUP", "10000"))

XUI_URL = os.environ["XUI_URL"].rstrip("/")
XUI_USER = os.environ["XUI_USER"]
XUI_PASS = os.environ["XUI_PASS"]
INBOUND_ID = int(os.environ["INBOUND_ID"])
SUB_URL = os.environ["SUB_URL"].rstrip("/")

DB_PATH = os.getenv("DB_PATH", "bot.db")

DIV = "┈" * 14
NUM = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


# ───────────────────────── دیتابیس ─────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, name TEXT, username TEXT,
                balance INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0,
                referrer INTEGER, ref_paid INTEGER DEFAULT 0,
                got_test INTEGER DEFAULT 0, created INTEGER);
            CREATE TABLE IF NOT EXISTS plans(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
                gb REAL, days REAL, price INTEGER, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS services(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                email TEXT, uuid TEXT, sub_id TEXT, plan_name TEXT,
                is_test INTEGER DEFAULT 0, created INTEGER);
            CREATE TABLE IF NOT EXISTS topups(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                amount INTEGER, status TEXT, receipt TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS tx(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                amount INTEGER, kind TEXT, note TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS discounts(
                code TEXT PRIMARY KEY, percent INTEGER, max_uses INTEGER DEFAULT 0,
                used INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS discount_uses(code TEXT, user_id INTEGER);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS ticket_map(admin_id INTEGER, msg_id INTEGER, user_id INTEGER);
            """
        )
        cur = await db.execute("SELECT COUNT(*) FROM plans")
        if (await cur.fetchone())[0] == 0:
            await db.executemany(
                "INSERT INTO plans(name,gb,days,price) VALUES(?,?,?,?)",
                [
                    ("۱ ماهه - ۳۰ گیگ", 30, 30, 150000),
                    ("۱ ماهه - ۶۰ گیگ", 60, 30, 250000),
                    ("۳ ماهه - ۱۰۰ گیگ", 100, 90, 400000),
                ],
            )
        await db.commit()


async def q_one(q, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, args)
        return await cur.fetchone()


async def q_all(q, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, args)
        return await cur.fetchall()


async def q_run(q, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, args)
        await db.commit()
        return cur.rowcount, cur.lastrowid


async def get_set(key, default="1"):
    r = await q_one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


async def set_set(key, val):
    await q_run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, val))


async def add_tx(uid, amount, kind, note=""):
    await q_run(
        "INSERT INTO tx(user_id,amount,kind,note,created) VALUES(?,?,?,?,?)",
        (uid, amount, kind, note, int(time.time())),
    )


async def get_balance(uid) -> int:
    r = await q_one("SELECT balance FROM users WHERE id=?", (uid,))
    return r["balance"] if r else 0


# ───────────────────────── کلاینت 3x-ui ─────────────────────────
class XUI:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()

    async def _login(self):
        if self.session:
            await self.session.close()
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        async with self.session.post(
            f"{XUI_URL}/login", data={"username": XUI_USER, "password": XUI_PASS}
        ) as r:
            data = await r.json(content_type=None)
            if not data.get("success"):
                raise RuntimeError(f"3x-ui login failed: {data}")

    async def request(self, method: str, path: str, **kw) -> dict:
        async with self.lock:
            if self.session is None:
                await self._login()
            for attempt in range(2):
                async with self.session.request(method, XUI_URL + path, **kw) as r:
                    if r.status == 404 and attempt == 0:  # سشن منقضی
                        await self._login()
                        continue
                    data = await r.json(content_type=None)
                    if not data.get("success"):
                        raise RuntimeError(f"3x-ui error: {data}")
                    return data
            raise RuntimeError("3x-ui request failed")

    @staticmethod
    def _payload(uid, email, sub_id, gb, days):
        expiry = int((time.time() + days * 86400) * 1000) if days else 0
        client = {
            "id": uid, "flow": "", "email": email, "limitIp": 0,
            "totalGB": int(gb * 1024**3), "expiryTime": expiry, "enable": True,
            "tgId": "", "subId": sub_id, "comment": "", "reset": 0,
        }
        return {"id": INBOUND_ID, "settings": json.dumps({"clients": [client]})}

    async def add_client(self, email, gb, days):
        uid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)
        await self.request(
            "POST", "/panel/api/inbounds/addClient",
            data=self._payload(uid, email, sub_id, gb, days),
        )
        return uid, sub_id

    async def renew_client(self, uid, email, sub_id, gb, days):
        await self.request(
            "POST", f"/panel/api/inbounds/updateClient/{uid}",
            data=self._payload(uid, email, sub_id, gb, days),
        )
        await self.request("POST", f"/panel/api/inbounds/{INBOUND_ID}/resetClientTraffic/{email}")

    async def traffic(self, email):
        data = await self.request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")
        return data.get("obj")


xui = XUI()


# ───────────────────────── ابزارها ─────────────────────────
def fa(x) -> str:
    return str(x).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def en(s: str) -> str:
    return s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def num(x) -> str:
    x = float(x)
    return fa(int(x) if x.is_integer() else round(x, 2))


def price_fmt(p: int) -> str:
    return f"{fa(f'{int(p):,}')} تومان"


def gb_fmt(b: float) -> str:
    return f"{fa(f'{b / 1024**3:.2f}')} GB"


def bar(percent: float, n: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * n)
    return "▰" * filled + "▱" * (n - filled)


def plan_volume(p) -> str:
    return "نامحدود ♾" if not p["gb"] else f"{num(p['gb'])} گیگ"


def plan_days(p) -> str:
    return f"{num(p['days'])} روز" if p["days"] >= 1 else f"{num(p['days'] * 24)} ساعت"


def sub_link(sub_id: str) -> str:
    return f"{SUB_URL}/{sub_id}"


def user_link(u) -> str:
    return f'<a href="tg://user?id={u.id}">{html.escape(u.full_name)}</a>'


async def show(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)


async def reply(target, text: str, markup=None):
    if isinstance(target, CallbackQuery):
        await show(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def notify_admins(bot: Bot, text: str, **kw):
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, **kw)
        except Exception:
            logging.exception("notify admin %s failed", admin)


async def main_menu(uid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 خرید سرویس", callback_data="buy")
    kb.button(text="📦 سرویس‌های من", callback_data="my")
    kb.button(text="💰 کیف پول", callback_data="wal")
    u = await q_one("SELECT got_test FROM users WHERE id=?", (uid,))
    show_test = (await get_set("test_enabled") == "1") and u is not None and not u["got_test"]
    if show_test:
        kb.button(text="🎁 اکانت تست", callback_data="test")
    kb.button(text="🤝 دعوت دوستان", callback_data="ref")
    kb.button(text="📚 آموزش", callback_data="help")
    kb.button(text="💬 پشتیبانی", callback_data="sup")
    sizes = [2, 2 if show_test else 1, 2, 1]
    if uid in ADMIN_IDS:
        kb.button(text="⚙️ پنل مدیریت", callback_data="adm:home")
        sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


async def home_text(u) -> str:
    bal = await get_balance(u.id)
    cnt = (await q_one("SELECT COUNT(*) c FROM services WHERE user_id=?", (u.id,)))["c"]
    return (
        f"🌐 <b>{html.escape(BRAND)}</b>\n{DIV}\n"
        f"سلام <b>{html.escape(u.first_name or 'دوست عزیز')}</b> 👋\n\n"
        f"💰 موجودی کیف پول: <b>{price_fmt(bal)}</b>\n"
        f"📦 سرویس‌های من: <b>{fa(cnt)}</b>\n{DIV}\n"
        "از منوی زیر انتخاب کن 👇"
    )


# ───────────────────────── State ها ─────────────────────────
class Buy(StatesGroup):
    confirm = State()
    code = State()


class Topup(StatesGroup):
    amount = State()
    receipt = State()


class Support(StatesGroup):
    msg = State()


class AdminSt(StatesGroup):
    plan_add = State()
    user_find = State()
    user_amount = State()
    disc_add = State()
    broadcast = State()


r = Router()  # کاربر
a = Router()  # ادمین
a.message.filter(F.from_user.id.in_(ADMIN_IDS))
a.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

_member_cache: dict[int, float] = {}


async def is_member(bot: Bot, uid: int) -> bool:
    now = time.time()
    if now - _member_cache.get(uid, 0) < 120:
        return True
    try:
        m = await bot.get_chat_member(FORCE_CHANNEL, uid)
        ok = m.status in ("member", "administrator", "creator")
    except Exception:
        logging.exception("membership check failed (ربات باید ادمین کانال باشه)")
        ok = True
    if ok:
        _member_cache[uid] = now
    return ok


class Guard(BaseMiddleware):
    """بلاک‌شده‌ها و عضویت اجباری در کانال"""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id in ADMIN_IDS:
            return await handler(event, data)
        row = await q_one("SELECT blocked FROM users WHERE id=?", (user.id,))
        if row and row["blocked"]:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔️ دسترسی شما مسدود شده است.", show_alert=True)
            else:
                await event.answer("⛔️ دسترسی شما مسدود شده است.")
            return
        is_chk = isinstance(event, CallbackQuery) and event.data == "chk"
        if FORCE_CHANNEL and not is_chk and not await is_member(data["bot"], user.id):
            kb = InlineKeyboardBuilder()
            kb.button(text="📢 عضویت در کانال", url=f"https://t.me/{FORCE_CHANNEL.lstrip('@')}")
            kb.button(text="✅ عضو شدم", callback_data="chk")
            kb.adjust(1)
            text = "🔒 برای استفاده از ربات اول باید توی کانال ما عضو بشی، بعد دکمه «عضو شدم» رو بزن."
            if isinstance(event, CallbackQuery):
                await event.message.answer(text, reply_markup=kb.as_markup())
                await event.answer()
            else:
                await event.answer(text, reply_markup=kb.as_markup())
            return
        return await handler(event, data)


r.message.middleware(Guard())
r.callback_query.middleware(Guard())


# ═════════════════════════ بخش کاربر ═════════════════════════
@r.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject, bot: Bot):
    await state.clear()
    u = m.from_user
    existing = await q_one("SELECT id FROM users WHERE id=?", (u.id,))
    if existing:
        await q_run("UPDATE users SET name=?, username=? WHERE id=?", (u.full_name, u.username, u.id))
    else:
        ref = None
        if command.args and command.args.startswith("ref_"):
            try:
                cand = int(command.args[4:])
                if cand != u.id and await q_one("SELECT id FROM users WHERE id=?", (cand,)):
                    ref = cand
            except ValueError:
                pass
        await q_run(
            "INSERT INTO users(id,name,username,referrer,created) VALUES(?,?,?,?,?)",
            (u.id, u.full_name, u.username, ref, int(time.time())),
        )
        if ref:
            try:
                await bot.send_message(ref, "🎉 یه نفر با لینک دعوت تو وارد ربات شد! بعد از اولین خریدش پاداشت به کیف پولت اضافه میشه.")
            except Exception:
                pass
    await m.answer(await home_text(u), reply_markup=await main_menu(u.id))


@r.message(Command("version"))
async def version(m: Message):
    await m.answer(f"🤖 نسخه ربات: <b>{VERSION}</b>")


@r.callback_query(F.data.in_({"menu", "chk"}))
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, await home_text(cb.from_user), await main_menu(cb.from_user.id))
    await cb.answer()


@r.callback_query(F.data == "help")
async def help_(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 بازگشت", callback_data="menu")
    await show(
        cb,
        "📚 <b>آموزش اتصال</b>\n"
        f"{DIV}\n"
        "<b>۱.</b> از «📦 سرویس‌های من» لینک اشتراک رو کپی کن (یا QR رو بگیر).\n\n"
        "<b>۲.</b> یکی از برنامه‌ها رو نصب کن:\n"
        "🤖 اندروید: Hiddify / v2rayNG\n"
        "🍏 آیفون: Streisand / Shadowrocket\n"
        "💻 ویندوز: Hiddify / v2rayN\n\n"
        "<b>۳.</b> توی برنامه «افزودن Subscription» یا «از کلیپ‌بورد» رو بزن.\n\n"
        "<b>۴.</b> وصل شو و لذت ببر ✅",
        kb.as_markup(),
    )
    await cb.answer()


# ───────── خرید / تمدید ─────────
async def valid_discount(uid, code):
    d = await q_one("SELECT * FROM discounts WHERE code=? AND active=1", (code.upper(),))
    if not d or (d["max_uses"] and d["used"] >= d["max_uses"]):
        return None
    if await q_one("SELECT 1 FROM discount_uses WHERE code=? AND user_id=?", (d["code"], uid)):
        return None
    return d


async def calc_price(uid, plan, code):
    price, disc = plan["price"], None
    if code:
        disc = await valid_discount(uid, code)
        if disc:
            price = price * (100 - disc["percent"]) // 100
    return price, disc


@r.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    if await get_set("shop_open") != "1" and cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("🔒 فروشگاه موقتاً بسته است.", show_alert=True)
    plans = await q_all("SELECT * FROM plans WHERE active=1 ORDER BY price")
    kb = InlineKeyboardBuilder()
    if not plans:
        kb.button(text="🔙 بازگشت", callback_data="menu")
        await show(cb, "فعلاً پلنی موجود نیست.", kb.as_markup())
        return await cb.answer()
    blocks = []
    for i, p in enumerate(plans):
        e = NUM[i] if i < len(NUM) else "🔹"
        blocks.append(f"{e} <b>{p['name']}</b>\n     📊 {plan_volume(p)}  •  ⏳ {plan_days(p)}\n     💰 {price_fmt(p['price'])}")
        kb.button(text=f"{e} {p['name']} • {price_fmt(p['price'])}", callback_data=f"p:{p['id']}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    await show(cb, f"🛍 <b>تعرفه‌ها</b>\n{DIV}\n\n" + "\n\n".join(blocks), kb.as_markup())
    await cb.answer()


async def send_confirm(target, state: FSMContext, uid: int):
    data = await state.get_data()
    plan = await q_one("SELECT * FROM plans WHERE id=? AND active=1", (data.get("plan_id"),))
    if not plan:
        return await reply(target, "این پلن دیگه موجود نیست. /start")
    price, disc = await calc_price(uid, plan, data.get("code"))
    bal = await get_balance(uid)
    kind = "🔄 تمدید سرویس" if data.get("service_id") else "🛒 خرید سرویس"
    lines = [
        f"{kind}\n{DIV}",
        f"📦 پلن: <b>{plan['name']}</b>",
        f"📊 حجم: {plan_volume(plan)}",
        f"⏳ مدت: {plan_days(plan)}",
    ]
    if disc:
        lines.append(f"🏷 تخفیف <code>{disc['code']}</code>: {fa(disc['percent'])}٪")
        lines.append(f"💵 قیمت اصلی: <s>{price_fmt(plan['price'])}</s>")
    lines += [f"💰 مبلغ نهایی: <b>{price_fmt(price)}</b>", DIV, f"👛 موجودی تو: {price_fmt(bal)}"]
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 پرداخت از کیف پول", callback_data="pay")
    kb.button(text="🏷 ثبت کد تخفیف", callback_data="code")
    kb.button(text="🔙 انصراف", callback_data="menu")
    kb.adjust(1)
    await reply(target, "\n".join(lines), kb.as_markup())


@r.callback_query(F.data.startswith("p:"))
async def pick_plan(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Buy.confirm)
    await state.update_data(plan_id=int(cb.data[2:]), service_id=None, code=None)
    await send_confirm(cb, state, cb.from_user.id)
    await cb.answer()


@r.callback_query(F.data.startswith("rp:"))
async def pick_renew_plan(cb: CallbackQuery, state: FSMContext):
    _, sid, pid = cb.data.split(":")
    await state.clear()
    await state.set_state(Buy.confirm)
    await state.update_data(plan_id=int(pid), service_id=int(sid), code=None)
    await send_confirm(cb, state, cb.from_user.id)
    await cb.answer()


@r.callback_query(F.data == "code")
async def ask_code(cb: CallbackQuery, state: FSMContext):
    if "plan_id" not in await state.get_data():
        return await cb.answer("نشست منقضی شد؛ دوباره از منو شروع کن.", show_alert=True)
    await state.set_state(Buy.code)
    await cb.message.answer("🏷 کد تخفیف رو بفرست:")
    await cb.answer()


@r.message(Buy.code, F.text)
async def got_code(m: Message, state: FSMContext):
    code = m.text.strip().upper()
    if await valid_discount(m.from_user.id, code):
        await state.update_data(code=code)
        await m.answer("✅ کد تخفیف اعمال شد.")
    else:
        await state.update_data(code=None)
        await m.answer("❌ کد نامعتبر، منقضی یا قبلاً استفاده شده.")
    await state.set_state(Buy.confirm)
    await send_confirm(m, state, m.from_user.id)


async def reward_referrer(bot: Bot, buyer_id: int):
    u = await q_one("SELECT referrer FROM users WHERE id=?", (buyer_id,))
    if not u or not u["referrer"] or REFERRAL_BONUS <= 0:
        return
    changed, _ = await q_run("UPDATE users SET ref_paid=1 WHERE id=? AND ref_paid=0", (buyer_id,))
    if not changed:
        return
    ref = u["referrer"]
    await q_run("UPDATE users SET balance=balance+? WHERE id=?", (REFERRAL_BONUS, ref))
    await add_tx(ref, REFERRAL_BONUS, "referral", f"invite {buyer_id}")
    try:
        await bot.send_message(ref, f"🎁 دوستی که دعوت کرده بودی خرید کرد!\n<b>{price_fmt(REFERRAL_BONUS)}</b> به کیف پولت اضافه شد.")
    except Exception:
        pass


async def provision_new(uid, name, gb, days, is_test=0):
    email = f"u{uid}_{secrets.token_hex(3)}"
    xid, sub_id = await xui.add_client(email, gb, days)
    _, sid = await q_run(
        "INSERT INTO services(user_id,email,uuid,sub_id,plan_name,is_test,created) VALUES(?,?,?,?,?,?,?)",
        (uid, email, xid, sub_id, name, is_test, int(time.time())),
    )
    return sid, sub_id


def result_kb(sid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 دریافت QR Code", callback_data=f"qr:{sid}")
    kb.button(text="📦 سرویس‌های من", callback_data="my")
    kb.button(text="🏠 منوی اصلی", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


@r.callback_query(F.data == "pay")
async def pay(cb: CallbackQuery, state: FSMContext, bot: Bot):
    uid = cb.from_user.id
    data = await state.get_data()
    if "plan_id" not in data:
        return await cb.answer("نشست منقضی شد؛ دوباره از منو شروع کن.", show_alert=True)
    if await get_set("shop_open") != "1" and uid not in ADMIN_IDS:
        return await cb.answer("🔒 فروشگاه موقتاً بسته است.", show_alert=True)
    plan = await q_one("SELECT * FROM plans WHERE id=? AND active=1", (data["plan_id"],))
    if not plan:
        return await cb.answer("این پلن دیگه موجود نیست.", show_alert=True)
    price, disc = await calc_price(uid, plan, data.get("code"))

    changed, _ = await q_run(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (price, uid, price)
    )
    if not changed:
        shortage = price - await get_balance(uid)
        topup_amt = max(MIN_TOPUP, -(-shortage // 1000) * 1000)
        kb = InlineKeyboardBuilder()
        kb.button(text=f"➕ شارژ {price_fmt(topup_amt)}", callback_data=f"ta:{topup_amt}")
        kb.button(text="🔙 بازگشت", callback_data="menu")
        kb.adjust(1)
        await show(cb, f"❗️ موجودی کافی نیست.\nکمبود: <b>{price_fmt(shortage)}</b>\n\nاول کیف پولت رو شارژ کن، بعد برگرد همین پلن رو بخر.", kb.as_markup())
        return await cb.answer()

    await cb.answer("⏳ در حال ساخت سرویس...")
    service_id = data.get("service_id")
    try:
        if service_id:
            svc = await q_one("SELECT * FROM services WHERE id=? AND user_id=?", (service_id, uid))
            if not svc:
                raise RuntimeError("service not found")
            await xui.renew_client(svc["uuid"], svc["email"], svc["sub_id"], plan["gb"], plan["days"])
            sid, sub_id = svc["id"], svc["sub_id"]
        else:
            sid, sub_id = await provision_new(uid, plan["name"], plan["gb"], plan["days"])
    except Exception:
        logging.exception("provisioning failed")
        await q_run("UPDATE users SET balance=balance+? WHERE id=?", (price, uid))
        await notify_admins(bot, f"⚠️ خطا در ساخت/تمدید سرویس برای کاربر <code>{uid}</code> (پنل 3x-ui رو چک کن). مبلغ به کیف پول برگشت داده شد.")
        return await show(cb, "❌ مشکلی در ساخت سرویس پیش اومد. مبلغ به کیف پولت برگشت. چند دقیقه دیگه دوباره امتحان کن.", await main_menu(uid))

    await add_tx(uid, -price, "renew" if service_id else "buy", plan["name"])
    if disc:
        await q_run("UPDATE discounts SET used=used+1 WHERE code=?", (disc["code"],))
        await q_run("INSERT INTO discount_uses(code,user_id) VALUES(?,?)", (disc["code"], uid))
    await state.clear()
    await show(
        cb,
        f"🎉 <b>{'تمدید' if service_id else 'خرید'} با موفقیت انجام شد!</b>\n{DIV}\n"
        f"📦 {plan['name']}\n💰 پرداختی: {price_fmt(price)}\n{DIV}\n"
        f"🔗 <b>لینک اشتراک</b> (برای کپی لمس کن):\n<code>{sub_link(sub_id)}</code>",
        result_kb(sid),
    )
    await reward_referrer(bot, uid)
    await notify_admins(bot, f"🛒 فروش جدید: {user_link(cb.from_user)} — {plan['name']} — {price_fmt(price)}")


# ───────── اکانت تست ─────────
@r.callback_query(F.data == "test")
async def test_acc(cb: CallbackQuery, bot: Bot):
    uid = cb.from_user.id
    if await get_set("test_enabled") != "1":
        return await cb.answer("اکانت تست فعلاً غیرفعاله.", show_alert=True)
    changed, _ = await q_run("UPDATE users SET got_test=1 WHERE id=? AND got_test=0", (uid,))
    if not changed:
        return await cb.answer("تو قبلاً اکانت تست گرفتی.", show_alert=True)
    await cb.answer("⏳ در حال ساخت...")
    try:
        sid, sub_id = await provision_new(uid, "🎁 اکانت تست", TEST_GB, TEST_DAYS, 1)
    except Exception:
        logging.exception("test provisioning failed")
        await q_run("UPDATE users SET got_test=0 WHERE id=?", (uid,))
        return await show(cb, "❌ ساخت اکانت تست با مشکل مواجه شد. بعداً دوباره امتحان کن.", await main_menu(uid))
    await show(
        cb,
        f"🎁 <b>اکانت تست ساخته شد</b>\n{DIV}\n"
        f"📊 حجم: {num(TEST_GB)} گیگ  •  ⏳ {plan_days({'days': TEST_DAYS})}\n{DIV}\n"
        f"🔗 لینک اشتراک:\n<code>{sub_link(sub_id)}</code>",
        result_kb(sid),
    )


# ───────── کیف پول ─────────
@r.callback_query(F.data == "wal")
async def wallet(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(cb.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ شارژ کیف پول", callback_data="top")
    kb.button(text="🧾 تاریخچه تراکنش‌ها", callback_data="hist")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    await show(cb, f"💰 <b>کیف پول</b>\n{DIV}\nموجودی: <b>{price_fmt(bal)}</b>", kb.as_markup())
    await cb.answer()


@r.callback_query(F.data == "hist")
async def history(cb: CallbackQuery):
    rows = await q_all("SELECT * FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 10", (cb.from_user.id,))
    names = {"topup": "شارژ", "buy": "خرید", "renew": "تمدید", "referral": "پاداش دعوت", "admin": "تغییر توسط مدیر"}
    lines = []
    for t in rows:
        sign = "🟢 +" if t["amount"] > 0 else "🔴 "
        date = time.strftime("%m/%d %H:%M", time.localtime(t["created"]))
        lines.append(f"{sign}{price_fmt(abs(t['amount']))} — {names.get(t['kind'], t['kind'])} <i>({fa(date)})</i>")
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 بازگشت", callback_data="wal")
    await show(cb, f"🧾 <b>۱۰ تراکنش اخیر</b>\n{DIV}\n" + ("\n".join(lines) if lines else "هنوز تراکنشی نداری."), kb.as_markup())
    await cb.answer()


@r.callback_query(F.data == "top")
async def topup_start(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for amt in (50000, 100000, 200000, 500000):
        kb.button(text=price_fmt(amt), callback_data=f"ta:{amt}")
    kb.button(text="✏️ مبلغ دلخواه", callback_data="ta:custom")
    kb.button(text="🔙 بازگشت", callback_data="wal")
    kb.adjust(2, 2, 1, 1)
    await show(cb, "➕ <b>شارژ کیف پول</b>\nمبلغ مورد نظرت رو انتخاب کن:", kb.as_markup())
    await cb.answer()


async def ask_receipt(target, state: FSMContext, amount: int):
    await state.set_state(Topup.receipt)
    await state.update_data(amount=amount)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ انصراف", callback_data="menu")
    await reply(
        target,
        f"💳 <b>پرداخت کارت به کارت</b>\n{DIV}\n"
        f"💰 مبلغ: <b>{price_fmt(amount)}</b>\n\n"
        f"<code>{CARD_NUMBER}</code>\n👤 به نام: <b>{CARD_HOLDER}</b>\n"
        "<i>(برای کپی شماره کارت روش بزن)</i>\n"
        f"{DIV}\n📸 بعد از واریز، <b>عکس رسید</b> رو همینجا بفرست.",
        kb.as_markup(),
    )


@r.callback_query(F.data.startswith("ta:"))
async def topup_amount_btn(cb: CallbackQuery, state: FSMContext):
    val = cb.data[3:]
    if val == "custom":
        await state.set_state(Topup.amount)
        await cb.message.answer(f"✏️ مبلغ رو به تومان بفرست (حداقل {price_fmt(MIN_TOPUP)}):")
        return await cb.answer()
    await ask_receipt(cb, state, int(val))
    await cb.answer()


@r.message(Topup.amount, F.text)
async def topup_amount_msg(m: Message, state: FSMContext):
    digits = "".join(ch for ch in en(m.text) if ch.isdigit())
    if not digits or int(digits) < MIN_TOPUP:
        return await m.answer(f"❌ مبلغ نامعتبره. یه عدد بفرست (حداقل {price_fmt(MIN_TOPUP)}).")
    await ask_receipt(m, state, int(digits))


@r.message(Topup.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    amount = (await state.get_data())["amount"]
    file_id = m.photo[-1].file_id
    _, tid = await q_run(
        "INSERT INTO topups(user_id,amount,status,receipt,created) VALUES(?,?,?,?,?)",
        (m.from_user.id, amount, "pending", file_id, int(time.time())),
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید", callback_data=f"tok:{tid}")
    kb.button(text="❌ رد", callback_data=f"tno:{tid}")
    kb.adjust(2)
    caption = (
        f"💳 <b>درخواست شارژ #{tid}</b>\n{DIV}\n"
        f"👤 {user_link(m.from_user)}\n🆔 <code>{m.from_user.id}</code>\n💰 {price_fmt(amount)}"
    )
    for admin in ADMIN_IDS:
        try:
            await bot.send_photo(admin, file_id, caption=caption, reply_markup=kb.as_markup())
        except Exception:
            logging.exception("notify admin failed")
    await state.clear()
    await m.answer(
        f"✅ <b>رسید دریافت شد</b>\n🧾 شماره درخواست: <b>#{fa(tid)}</b>\n"
        "⏳ بعد از تأیید ادمین، مبلغ به کیف پولت اضافه میشه.",
        reply_markup=await main_menu(m.from_user.id),
    )


@r.message(Topup.receipt)
async def topup_not_photo(m: Message):
    await m.answer("📸 لطفاً فقط <b>عکس رسید</b> رو بفرست (یا /start برای انصراف).")


# ───────── سرویس‌های من ─────────
@r.callback_query(F.data == "my")
async def my_services(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await q_all("SELECT * FROM services WHERE user_id=? ORDER BY id DESC", (cb.from_user.id,))
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="🛒 خرید سرویس", callback_data="buy")
        kb.button(text="🔙 بازگشت", callback_data="menu")
        kb.adjust(1)
        await show(cb, "📭 هنوز سرویسی نداری.", kb.as_markup())
        return await cb.answer()
    for s in rows:
        kb.button(text=f"📦 {s['plan_name']}", callback_data=f"svc:{s['id']}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    await show(cb, f"📦 <b>سرویس‌های من</b> ({fa(len(rows))})", kb.as_markup())
    await cb.answer()


@r.callback_query(F.data.startswith("svc:"))
async def service_detail(cb: CallbackQuery):
    sid = int(cb.data[4:])
    svc = await q_one("SELECT * FROM services WHERE id=? AND user_id=?", (sid, cb.from_user.id))
    if not svc:
        return await cb.answer("سرویس پیدا نشد", show_alert=True)
    try:
        t = await xui.traffic(svc["email"])
    except Exception:
        logging.exception("traffic fetch failed")
        t = None
    if t:
        used = t["up"] + t["down"]
        expired = t["expiryTime"] != 0 and t["expiryTime"] / 1000 < time.time()
        exhausted = t["total"] != 0 and used >= t["total"]
        active = t.get("enable", True) and not expired and not exhausted
        if t["total"] == 0:
            usage = f"♾ نامحدود\n   مصرف: {gb_fmt(used)}"
        else:
            pct = used / t["total"] * 100
            usage = f"{bar(pct)} {fa(round(pct))}٪\n   {gb_fmt(used)} از {gb_fmt(t['total'])}"
        if t["expiryTime"] == 0:
            exp = "♾ نامحدود"
        else:
            left = (t["expiryTime"] / 1000 - time.time()) / 86400
            exp = "⛔️ منقضی شده" if left <= 0 else f"{fa(int(left) + 1)} روز دیگر"
        info = f"📡 وضعیت: {'🟢 فعال' if active else '🔴 غیرفعال'}\n📊 مصرف: {usage}\n⏳ زمان باقی‌مانده: {exp}"
    else:
        info = "⚠️ اطلاعات مصرف الان در دسترس نیست."
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 تمدید", callback_data=f"renew:{sid}")
    kb.button(text="📱 QR Code", callback_data=f"qr:{sid}")
    kb.button(text="🔃 بروزرسانی", callback_data=f"svc:{sid}")
    kb.button(text="🔙 بازگشت", callback_data="my")
    kb.adjust(2, 1, 1)
    await show(
        cb,
        f"📦 <b>{svc['plan_name']}</b>\n{DIV}\n{info}\n{DIV}\n"
        f"🔗 <b>لینک اشتراک</b> (برای کپی لمس کن):\n<code>{sub_link(svc['sub_id'])}</code>",
        kb.as_markup(),
    )
    await cb.answer()


@r.callback_query(F.data.startswith("renew:"))
async def renew(cb: CallbackQuery):
    sid = cb.data.split(":")[1]
    plans = await q_all("SELECT * FROM plans WHERE active=1 ORDER BY price")
    kb = InlineKeyboardBuilder()
    for p in plans:
        kb.button(text=f"{p['name']} • {price_fmt(p['price'])}", callback_data=f"rp:{sid}:{p['id']}")
    kb.button(text="🔙 بازگشت", callback_data=f"svc:{sid}")
    kb.adjust(1)
    await show(cb, "🔄 <b>تمدید سرویس</b>\nپلن تمدید رو انتخاب کن.\n<i>با تمدید، حجم مصرف‌شده ریست میشه.</i>", kb.as_markup())
    await cb.answer()


@r.callback_query(F.data.startswith("qr:"))
async def qr_code(cb: CallbackQuery):
    svc = await q_one("SELECT * FROM services WHERE id=? AND user_id=?", (int(cb.data[3:]), cb.from_user.id))
    if not svc:
        return await cb.answer("سرویس پیدا نشد", show_alert=True)
    link = sub_link(svc["sub_id"])
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    await cb.message.answer_photo(
        BufferedInputFile(buf.getvalue(), filename="qr.png"),
        caption=f"📱 QR Code لینک اشتراک\n<code>{link}</code>",
    )
    await cb.answer()


# ───────── دعوت دوستان ─────────
_bot_username: str | None = None


@r.callback_query(F.data == "ref")
async def referral(cb: CallbackQuery, bot: Bot):
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.get_me()).username
    uid = cb.from_user.id
    count = (await q_one("SELECT COUNT(*) c FROM users WHERE referrer=?", (uid,)))["c"]
    earned = (await q_one("SELECT COALESCE(SUM(amount),0) s FROM tx WHERE user_id=? AND kind='referral'", (uid,)))["s"]
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 بازگشت", callback_data="menu")
    await show(
        cb,
        f"🤝 <b>دعوت دوستان</b>\n{DIV}\n"
        f"هر دوستی که با لینک تو وارد بشه و اولین خریدش رو انجام بده، <b>{price_fmt(REFERRAL_BONUS)}</b> به کیف پولت اضافه میشه.\n\n"
        f"👥 دعوت‌شده‌ها: <b>{fa(count)}</b>\n💵 درآمد از دعوت: <b>{price_fmt(earned)}</b>\n{DIV}\n"
        f"🔗 لینک اختصاصی تو:\n<code>https://t.me/{_bot_username}?start=ref_{uid}</code>",
        kb.as_markup(),
    )
    await cb.answer()


# ───────── پشتیبانی ─────────
@r.callback_query(F.data == "sup")
async def support(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Support.msg)
    kb = InlineKeyboardBuilder()
    if SUPPORT_USERNAME:
        kb.button(text="👤 پیام مستقیم به پشتیبان", url=f"https://t.me/{SUPPORT_USERNAME}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    await show(cb, "💬 <b>پشتیبانی</b>\nپیامت رو همینجا بنویس و بفرست (متن، عکس یا فایل)؛ جواب همینجا برات میاد.", kb.as_markup())
    await cb.answer()


@r.message(Support.msg)
async def support_msg(m: Message, state: FSMContext, bot: Bot):
    header = (
        f"📩 <b>پیام پشتیبانی</b>\n👤 {user_link(m.from_user)} | <code>{m.from_user.id}</code>\n"
        "<i>برای پاسخ، روی همین پیام یا پیام زیرش Reply بزن.</i>"
    )
    for admin in ADMIN_IDS:
        try:
            h = await bot.send_message(admin, header)
            c = await bot.copy_message(admin, m.chat.id, m.message_id)
            for mid in (h.message_id, c.message_id):
                await q_run("INSERT INTO ticket_map(admin_id,msg_id,user_id) VALUES(?,?,?)", (admin, mid, m.from_user.id))
        except Exception:
            logging.exception("support forward failed")
    await state.clear()
    await m.answer("✅ پیامت ارسال شد. به‌زودی جواب میدیم.", reply_markup=await main_menu(m.from_user.id))


# ═════════════════════════ بخش ادمین ═════════════════════════
def admin_home_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 آمار", callback_data="adm:stats")
    kb.button(text="📦 پلن‌ها", callback_data="adm:plans")
    kb.button(text="👤 مدیریت کاربر", callback_data="adm:user")
    kb.button(text="🏷 کدهای تخفیف", callback_data="adm:disc")
    kb.button(text="📢 پیام همگانی", callback_data="adm:bc")
    kb.button(text="⚙️ تنظیمات", callback_data="adm:set")
    kb.button(text="🏠 منوی کاربر", callback_data="menu")
    kb.adjust(2, 2, 2, 1)
    return kb.as_markup()


def back_admin():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 پنل مدیریت", callback_data="adm:home")
    return kb.as_markup()


@a.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(f"⚙️ <b>پنل مدیریت</b> — نسخه {VERSION}", reply_markup=admin_home_kb())


@a.callback_query(F.data == "adm:home")
async def admin_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, f"⚙️ <b>پنل مدیریت</b> — نسخه {VERSION}", admin_home_kb())
    await cb.answer()


@a.callback_query(F.data == "adm:stats")
async def admin_stats(cb: CallbackQuery):
    one = lambda q: q_one(q)
    users = (await one("SELECT COUNT(*) c FROM users"))["c"]
    svcs = (await one("SELECT COUNT(*) c FROM services WHERE is_test=0"))["c"]
    tests = (await one("SELECT COUNT(*) c FROM services WHERE is_test=1"))["c"]
    bal = (await one("SELECT COALESCE(SUM(balance),0) s FROM users"))["s"]
    sales = (await one("SELECT COALESCE(-SUM(amount),0) s FROM tx WHERE kind IN ('buy','renew')"))["s"]
    topups = (await one("SELECT COALESCE(SUM(amount),0) s FROM tx WHERE kind='topup'"))["s"]
    pend = (await one("SELECT COUNT(*) c FROM topups WHERE status='pending'"))["c"]
    await show(
        cb,
        f"📊 <b>آمار</b>\n{DIV}\n"
        f"👥 کاربران: <b>{fa(users)}</b>\n📦 سرویس‌های فروخته‌شده: <b>{fa(svcs)}</b>\n🎁 اکانت تست: <b>{fa(tests)}</b>\n{DIV}\n"
        f"💰 فروش کل: <b>{price_fmt(sales)}</b>\n➕ مجموع شارژها: <b>{price_fmt(topups)}</b>\n"
        f"👛 مجموع موجودی کاربران: <b>{price_fmt(bal)}</b>\n⏳ شارژهای در انتظار: <b>{fa(pend)}</b>",
        back_admin(),
    )
    await cb.answer()


# ── تأیید/رد شارژ ──
@a.callback_query(F.data.startswith("tok:"))
async def topup_ok(cb: CallbackQuery, bot: Bot):
    tid = int(cb.data[4:])
    changed, _ = await q_run("UPDATE topups SET status='done' WHERE id=? AND status='pending'", (tid,))
    if not changed:
        return await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
    t = await q_one("SELECT * FROM topups WHERE id=?", (tid,))
    await q_run("UPDATE users SET balance=balance+? WHERE id=?", (t["amount"], t["user_id"]))
    await add_tx(t["user_id"], t["amount"], "topup", f"topup #{tid}")
    try:
        await bot.send_message(
            t["user_id"],
            f"✅ <b>شارژ تأیید شد</b>\n{price_fmt(t['amount'])} به کیف پولت اضافه شد.\n"
            f"👛 موجودی جدید: <b>{price_fmt(await get_balance(t['user_id']))}</b>",
            reply_markup=await main_menu(t["user_id"]),
        )
    except Exception:
        pass
    await cb.message.edit_caption(caption=cb.message.html_text + "\n\n✅ تأیید شد", reply_markup=None)
    await cb.answer("انجام شد")


@a.callback_query(F.data.startswith("tno:"))
async def topup_no(cb: CallbackQuery, bot: Bot):
    tid = int(cb.data[4:])
    changed, _ = await q_run("UPDATE topups SET status='rejected' WHERE id=? AND status='pending'", (tid,))
    if not changed:
        return await cb.answer("این درخواست قبلاً رسیدگی شده.", show_alert=True)
    t = await q_one("SELECT * FROM topups WHERE id=?", (tid,))
    try:
        await bot.send_message(t["user_id"], f"❌ درخواست شارژ #{fa(tid)} تأیید نشد. اگه فکر می‌کنی اشتباهه به پشتیبانی پیام بده.")
    except Exception:
        pass
    await cb.message.edit_caption(caption=cb.message.html_text + "\n\n❌ رد شد", reply_markup=None)
    await cb.answer("رد شد")


# ── پلن‌ها ──
@a.callback_query(F.data == "adm:plans")
async def admin_plans(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    plans = await q_all("SELECT * FROM plans ORDER BY price")
    kb = InlineKeyboardBuilder()
    lines = []
    for p in plans:
        st = "✅" if p["active"] else "⛔️"
        lines.append(f"{st} <b>{p['name']}</b> — {plan_volume(p)} / {plan_days(p)} / {price_fmt(p['price'])}")
        kb.row(
            InlineKeyboardButton(text=f"{'غیرفعال' if p['active'] else 'فعال'} کردن: {p['name'][:18]}", callback_data=f"adm:pt:{p['id']}"),
            InlineKeyboardButton(text="🗑", callback_data=f"adm:pd:{p['id']}"),
        )
    kb.row(InlineKeyboardButton(text="➕ افزودن پلن", callback_data="adm:pa"))
    kb.row(InlineKeyboardButton(text="🔙 پنل مدیریت", callback_data="adm:home"))
    await show(cb, f"📦 <b>پلن‌ها</b>\n{DIV}\n" + ("\n".join(lines) if lines else "پلنی نیست."), kb.as_markup())
    await cb.answer()


@a.callback_query(F.data.startswith("adm:pt:"))
async def plan_toggle(cb: CallbackQuery, state: FSMContext):
    await q_run("UPDATE plans SET active=1-active WHERE id=?", (int(cb.data.split(":")[2]),))
    await admin_plans(cb, state)


@a.callback_query(F.data.startswith("adm:pd:"))
async def plan_delete(cb: CallbackQuery, state: FSMContext):
    await q_run("DELETE FROM plans WHERE id=?", (int(cb.data.split(":")[2]),))
    await admin_plans(cb, state)


@a.callback_query(F.data == "adm:pa")
async def plan_add_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.plan_add)
    await cb.message.answer(
        "➕ پلن جدید رو با این فرمت بفرست:\n<code>نام پلن | حجم گیگ | تعداد روز | قیمت تومان</code>\n\n"
        "مثال:\n<code>ماهانه ۵۰ گیگ | 50 | 30 | 200000</code>\n"
        "(حجم 0 = نامحدود)"
    )
    await cb.answer()


@a.message(AdminSt.plan_add, F.text)
async def plan_add_msg(m: Message, state: FSMContext):
    try:
        parts = [p.strip() for p in m.text.split("|")]
        name = parts[0]
        gb, days, price = float(en(parts[1])), float(en(parts[2])), int(en(parts[3]).replace(",", ""))
        assert name and gb >= 0 and days > 0 and price >= 0
    except Exception:
        return await m.answer("❌ فرمت اشتباهه. دوباره بفرست (یا /admin برای انصراف).")
    await q_run("INSERT INTO plans(name,gb,days,price) VALUES(?,?,?,?)", (name, gb, days, price))
    await state.clear()
    await m.answer(f"✅ پلن «{name}» اضافه شد.", reply_markup=back_admin())


# ── مدیریت کاربر ──
async def user_card(uid: int):
    u = await q_one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return None, None
    cnt = (await q_one("SELECT COUNT(*) c FROM services WHERE user_id=?", (uid,)))["c"]
    text = (
        f"👤 <b>{html.escape(u['name'] or '-')}</b>\n{DIV}\n"
        f"🆔 <code>{uid}</code>  {'@' + u['username'] if u['username'] else ''}\n"
        f"💰 موجودی: <b>{price_fmt(u['balance'])}</b>\n📦 سرویس‌ها: {fa(cnt)}\n"
        f"🚫 وضعیت: {'بلاک' if u['blocked'] else 'عادی'}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزایش موجودی", callback_data=f"adm:bal:{uid}:add")
    kb.button(text="➖ کاهش موجودی", callback_data=f"adm:bal:{uid}:sub")
    kb.button(text="✅ آنبلاک" if u["blocked"] else "🚫 بلاک", callback_data=f"adm:blk:{uid}")
    kb.button(text="🔙 پنل مدیریت", callback_data="adm:home")
    kb.adjust(2, 1, 1)
    return text, kb.as_markup()


@a.callback_query(F.data == "adm:user")
async def user_find_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.user_find)
    await cb.message.answer("🔎 آیدی عددی کاربر رو بفرست:")
    await cb.answer()


@a.message(AdminSt.user_find, F.text)
async def user_find_msg(m: Message, state: FSMContext):
    try:
        uid = int(en(m.text.strip()))
    except ValueError:
        return await m.answer("❌ فقط آیدی عددی بفرست.")
    text, kb = await user_card(uid)
    if not text:
        return await m.answer("کاربری با این آیدی توی ربات نیست.")
    await state.clear()
    await m.answer(text, reply_markup=kb)


@a.callback_query(F.data.startswith("adm:bal:"))
async def user_bal_ask(cb: CallbackQuery, state: FSMContext):
    _, _, uid, mode = cb.data.split(":")
    await state.set_state(AdminSt.user_amount)
    await state.update_data(uid=int(uid), mode=mode)
    await cb.message.answer(f"مبلغ رو به تومان بفرست ({'افزایش' if mode == 'add' else 'کاهش'}):")
    await cb.answer()


@a.message(AdminSt.user_amount, F.text)
async def user_bal_msg(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    digits = "".join(ch for ch in en(m.text) if ch.isdigit())
    if not digits:
        return await m.answer("❌ عدد معتبر بفرست.")
    amt = int(digits) * (1 if d["mode"] == "add" else -1)
    if amt < 0:
        changed, _ = await q_run("UPDATE users SET balance=balance+? WHERE id=? AND balance>=?", (amt, d["uid"], -amt))
        if not changed:
            return await m.answer("❌ موجودی کاربر کمتر از این مبلغه.")
    else:
        await q_run("UPDATE users SET balance=balance+? WHERE id=?", (amt, d["uid"]))
    await add_tx(d["uid"], amt, "admin", f"by {m.from_user.id}")
    await state.clear()
    try:
        await bot.send_message(d["uid"], f"💰 موجودی کیف پولت توسط مدیر {'افزایش' if amt > 0 else 'کاهش'} یافت: <b>{price_fmt(abs(amt))}</b>")
    except Exception:
        pass
    text, kb = await user_card(d["uid"])
    await m.answer("✅ انجام شد.\n\n" + text, reply_markup=kb)


@a.callback_query(F.data.startswith("adm:blk:"))
async def user_block(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    await q_run("UPDATE users SET blocked=1-blocked WHERE id=?", (uid,))
    text, kb = await user_card(uid)
    await show(cb, text, kb)
    await cb.answer("انجام شد")


# ── کد تخفیف ──
@a.callback_query(F.data == "adm:disc")
async def admin_disc(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await q_all("SELECT * FROM discounts")
    kb = InlineKeyboardBuilder()
    lines = []
    for d in rows:
        lim = "∞" if not d["max_uses"] else fa(d["max_uses"])
        lines.append(f"🏷 <code>{d['code']}</code> — {fa(d['percent'])}٪ — استفاده: {fa(d['used'])}/{lim}")
        kb.row(InlineKeyboardButton(text=f"🗑 حذف {d['code']}", callback_data=f"adm:dd:{d['code']}"))
    kb.row(InlineKeyboardButton(text="➕ ساخت کد جدید", callback_data="adm:da"))
    kb.row(InlineKeyboardButton(text="🔙 پنل مدیریت", callback_data="adm:home"))
    await show(cb, f"🏷 <b>کدهای تخفیف</b>\n{DIV}\n" + ("\n".join(lines) if lines else "کدی نیست."), kb.as_markup())
    await cb.answer()


@a.callback_query(F.data.startswith("adm:dd:"))
async def disc_delete(cb: CallbackQuery, state: FSMContext):
    await q_run("DELETE FROM discounts WHERE code=?", (cb.data.split(":", 2)[2],))
    await admin_disc(cb, state)


@a.callback_query(F.data == "adm:da")
async def disc_add_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.disc_add)
    await cb.message.answer(
        "🏷 کد جدید رو با این فرمت بفرست:\n<code>کد | درصد تخفیف | سقف تعداد استفاده</code>\n\n"
        "مثال:\n<code>OFF20 | 20 | 100</code>\n(سقف 0 = نامحدود)"
    )
    await cb.answer()


@a.message(AdminSt.disc_add, F.text)
async def disc_add_msg(m: Message, state: FSMContext):
    try:
        code, pct, mx = [p.strip() for p in m.text.split("|")]
        pct, mx = int(en(pct)), int(en(mx))
        assert code and 1 <= pct <= 100 and mx >= 0
    except Exception:
        return await m.answer("❌ فرمت اشتباهه. دوباره بفرست (یا /admin برای انصراف).")
    await q_run("INSERT OR REPLACE INTO discounts(code,percent,max_uses,used,active) VALUES(?,?,?,0,1)", (code.upper(), pct, mx))
    await state.clear()
    await m.answer(f"✅ کد <code>{code.upper()}</code> ساخته شد.", reply_markup=back_admin())


# ── پیام همگانی ──
@a.callback_query(F.data == "adm:bc")
async def bc_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSt.broadcast)
    await cb.message.answer("📢 پیامی که می‌خوای برای همه کاربرها بره رو بفرست (متن، عکس، ...). برای انصراف /admin")
    await cb.answer()


@a.message(AdminSt.broadcast)
async def bc_send(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = await q_all("SELECT id FROM users WHERE blocked=0")
    ok = 0
    note = await m.answer(f"⏳ در حال ارسال برای {fa(len(users))} کاربر...")
    for u in users:
        try:
            await bot.copy_message(u["id"], m.chat.id, m.message_id)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await note.edit_text(f"✅ برای {fa(ok)} از {fa(len(users))} کاربر ارسال شد.", reply_markup=back_admin())


# ── تنظیمات ──
async def settings_view(cb: CallbackQuery):
    shop = await get_set("shop_open") == "1"
    test = await get_set("test_enabled") == "1"
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🛒 فروشگاه: {'باز ✅' if shop else 'بسته ⛔️'}", callback_data="adm:tg:shop_open")
    kb.button(text=f"🎁 اکانت تست: {'فعال ✅' if test else 'غیرفعال ⛔️'}", callback_data="adm:tg:test_enabled")
    kb.button(text="🔙 پنل مدیریت", callback_data="adm:home")
    kb.adjust(1)
    await show(cb, f"⚙️ <b>تنظیمات</b>\n{DIV}\nبرای تغییر روی هر گزینه بزن.", kb.as_markup())


@a.callback_query(F.data == "adm:set")
async def admin_settings(cb: CallbackQuery):
    await settings_view(cb)
    await cb.answer()


@a.callback_query(F.data.startswith("adm:tg:"))
async def admin_toggle(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    await set_set(key, "0" if await get_set(key) == "1" else "1")
    await settings_view(cb)
    await cb.answer("انجام شد")


# ── پاسخ ادمین به تیکت پشتیبانی (Reply) ──
@a.message(F.reply_to_message)
async def admin_reply(m: Message, bot: Bot):
    row = await q_one(
        "SELECT user_id FROM ticket_map WHERE admin_id=? AND msg_id=?",
        (m.from_user.id, m.reply_to_message.message_id),
    )
    if not row:
        return
    try:
        await bot.send_message(row["user_id"], "💬 <b>پاسخ پشتیبانی:</b>")
        await bot.copy_message(row["user_id"], m.chat.id, m.message_id)
        await m.reply("✅ برای کاربر ارسال شد.")
    except Exception:
        await m.reply("❌ ارسال نشد (شاید کاربر ربات رو بلاک کرده).")


# ───────────────────────── اجرا ─────────────────────────
async def main():
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(r)
    dp.include_router(a)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot version %s started", VERSION)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
