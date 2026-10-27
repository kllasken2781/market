import os
import re
import json
import time
import hmac
import base64
import hashlib
from contextlib import asynccontextmanager
from typing import List, Optional
from urllib.parse import parse_qsl

import telebot
from telebot import types
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import (
    BigInteger, Boolean, Column, Integer, MetaData, String, Table, Text,
    create_engine, delete, desc, insert, inspect, select, update,
)
from sqlalchemy import text as sql_text

# =====================  SOZLAMALAR (Render'da Environment orqali)  =====================
BASE = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if re.fullmatch(r"-?\d+", x)}
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "0") or 0)
CARD = os.environ.get("CARD", "8600 0000 0000 0000")
CARD_OWNER = os.environ.get("CARD_OWNER", "ISM FAMILIYA")
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
VERSION = str(int(time.time()))      # har deployda yangi: Telegram eski sahifani ushlab qolmasligi uchun
SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]

DB_URL = os.environ.get("DATABASE_URL") or ("sqlite:///" + os.path.join(BASE, "data.db"))
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)
if DB_URL.startswith("postgresql://"):
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg://", 1)   # psycopg 3 drayveri

CATS = {"kiyim", "elektr", "uy"}
MAX_IMAGES = 6


def parse_support(raw):
    """SUPPORT = "Elyor=@username, Sherik=@username2" """
    out = []
    for part in re.split(r"[;,\n]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        name, user = (part.split("=", 1) if "=" in part else ("", part))
        user = user.strip().replace("https://t.me/", "").lstrip("@")
        if re.fullmatch(r"[A-Za-z0-9_]{4,32}", user):
            out.append({"name": name.strip() or "@" + user, "user": user})
    return out


SUPPORT = parse_support(os.environ.get("SUPPORT", ""))

# Yetkazib berish bosqichlari (admin tugmalari). Yangi bosqich qo'shish: shu ro'yxatga bitta qator qo'shing.
STAGES = [
    {"k": "china",    "uz": "Buyurtmangiz Xitoyda",                         "ru": "Ваш заказ в Китае"},
    {"k": "cn_wh",    "uz": "Buyurtmangiz Xitoydagi omborga yetib keldi",   "ru": "Ваш заказ прибыл на склад в Китае"},
    {"k": "to_uz",    "uz": "Buyurtmangiz O'zbekistonga yuborildi",         "ru": "Ваш заказ отправлен в Узбекистан"},
    {"k": "tashkent", "uz": "Buyurtmangiz Toshkentga yetib keldi",          "ru": "Ваш заказ прибыл в Ташкент"},
    {"k": "uzpost",   "uz": "Buyurtmangiz Uzpost orqali sizga jo'natildi",  "ru": "Ваш заказ отправлен вам через Uzpost"},
    {"k": "pickup",   "uz": "Buyurtmangizni olib ketishingiz mumkin",       "ru": "Вы можете забрать ваш заказ"},
]

# =====================  BAZA  =====================
engine = create_engine(
    DB_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {},
)
md = MetaData()

products = Table(
    "products", md,
    Column("id", Integer, primary_key=True),
    Column("name_uz", String(200), nullable=False),
    Column("name_ru", String(200), nullable=False),
    Column("cat", String(20), nullable=False),
    Column("cost", BigInteger, nullable=False, default=0),     # Pinduoduo narxi (so'm)
    Column("price", BigInteger, nullable=False),               # sotuv narxi (so'm)
    Column("old_price", BigInteger),                           # eski narx (chegirma uchun)
    Column("image", Text),                                     # ESKI ustun: ishga tushganda galereyaga ko'chiriladi
    Column("has_img", Boolean, nullable=False, default=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("updated", Integer, nullable=False, default=0),
    Column("desc_uz", Text),
    Column("desc_ru", Text),
    Column("sizes", Text),                                     # "S, M, L" ko'rinishida
    Column("colors", Text),                                    # "Qora, Oq" ko'rinishida
    Column("sold_out", Boolean, default=False),                # "Tugadi" belgisi
)

product_images = Table(
    "product_images", md,
    Column("id", Integer, primary_key=True),
    Column("product_id", Integer, nullable=False, index=True),
    Column("data", Text, nullable=False),                      # data URL
    Column("pos", Integer, nullable=False, default=0),
)

orders = Table(
    "orders", md,
    Column("id", Integer, primary_key=True),
    Column("user_id", BigInteger, nullable=False),
    Column("username", String(100)),
    Column("name", String(200)),
    Column("phone", String(50)),
    Column("region", String(100)),
    Column("city", String(200)),
    Column("post", String(300)),
    Column("items", Text),
    Column("total", BigInteger),
    Column("status", String(20)),     # wait_receipt / checking / paid / rejected
    Column("lang", String(2)),
    Column("created", Integer),
    Column("ship", String(30)),       # yetkazib berish bosqichi (STAGES[].k)
    Column("ship_time", Integer),
)

profiles = Table(
    "profiles", md,
    Column("user_id", BigInteger, primary_key=True, autoincrement=False),
    Column("name", String(200)),
    Column("phone", String(50)),
    Column("region", String(20)),     # viloyat tartib raqami (0..13)
    Column("city", String(200)),
    Column("post", String(300)),
    Column("lang", String(2)),
    Column("theme", String(10)),
    Column("updated", Integer),
)

favorites = Table(
    "favorites", md,
    Column("id", Integer, primary_key=True),
    Column("user_id", BigInteger, nullable=False, index=True),
    Column("product_id", Integer, nullable=False),
)

cart_items = Table(
    "cart_items", md,
    Column("id", Integer, primary_key=True),
    Column("user_id", BigInteger, nullable=False, index=True),
    Column("product_id", Integer, nullable=False),
    Column("size", String(100), nullable=False, default=""),
    Column("color", String(100), nullable=False, default=""),
    Column("qty", Integer, nullable=False, default=1),
)

PRODUCT_COLS = [c for c in products.c if c.name != "image"]


def migrate():
    """Jadvallarni yaratadi, eski bazaga yangi ustunlarni qo'shadi, eski rasmni galereyaga ko'chiradi."""
    md.create_all(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("products")}
    with engine.begin() as c:
        for name in ("desc_uz", "desc_ru", "sizes", "colors"):
            if name not in cols:
                c.execute(sql_text(f"ALTER TABLE products ADD COLUMN {name} TEXT"))
        if "sold_out" not in cols:
            c.execute(sql_text("ALTER TABLE products ADD COLUMN sold_out BOOLEAN DEFAULT FALSE"))
    ocols = {c["name"] for c in inspect(engine).get_columns("orders")}
    with engine.begin() as c:
        if "ship" not in ocols:
            c.execute(sql_text("ALTER TABLE orders ADD COLUMN ship VARCHAR(30)"))
        if "ship_time" not in ocols:
            c.execute(sql_text("ALTER TABLE orders ADD COLUMN ship_time INTEGER"))
    with engine.begin() as c:
        rows = c.execute(select(products.c.id, products.c.image).where(products.c.image.isnot(None))).all()
        for pid, img in rows:
            if img:
                has = c.execute(select(product_images.c.id).where(product_images.c.product_id == pid).limit(1)).first()
                if not has:
                    c.execute(insert(product_images).values(product_id=pid, data=img, pos=0))
                c.execute(update(products).where(products.c.id == pid).values(image=None, has_img=True))
            else:
                c.execute(update(products).where(products.c.id == pid).values(image=None))


# =====================  BOT  =====================
bot = telebot.TeleBot(BOT_TOKEN or "0:local", threaded=False)

M = {
    "uz": {
        "got": "✅ Buyurtma #{n} qabul qilindi.\n\n{items}\n\n💰 Jami: {total}\n\nKartaga o'tkazing:\n{card}\n{owner}\n\nTo'lagach, shu yerga CHEK RASMINI yuboring 📸",
        "checking": "Chek qabul qilindi ✅ Buyurtma #{n} tekshirilmoqda.",
        "paid": "✅ Buyurtma #{n} uchun to'lov tasdiqlandi. Rahmat!",
        "rejected": "❌ Buyurtma #{n} uchun to'lov tasdiqlanmadi. To'g'ri chek rasmini qayta yuboring.",
        "noorder": "Avval do'kondan buyurtma bering 🛍",
        "welcome": "Xush kelibsiz! Mahsulotlarni ko'rish uchun tugmani bosing.",
        "open": "🛍 Do'konni ochish",
    },
    "ru": {
        "got": "✅ Заказ #{n} принят.\n\n{items}\n\n💰 Итого: {total}\n\nПереведите на карту:\n{card}\n{owner}\n\nПосле оплаты отправьте сюда ФОТО ЧЕКА 📸",
        "checking": "Чек получен ✅ Заказ #{n} проверяется.",
        "paid": "✅ Оплата заказа #{n} подтверждена. Спасибо!",
        "rejected": "❌ Оплата заказа #{n} не подтверждена. Отправьте правильное фото чека ещё раз.",
        "noorder": "Сначала оформите заказ в магазине 🛍",
        "welcome": "Добро пожаловать! Нажмите кнопку, чтобы открыть магазин.",
        "open": "🛍 Открыть магазин",
    },
}


def som(n):
    return f"{int(n):,}".replace(",", " ") + " so'm"


def safe_send(chat_id, text, **kw):
    try:
        return bot.send_message(chat_id, text, **kw)
    except Exception as e:
        print("send_message xato:", e)


def targets():
    return [ADMIN_GROUP_ID] if ADMIN_GROUP_ID else list(ADMIN_IDS)


def user_lang(u):
    return "ru" if (getattr(u, "language_code", "") or "").startswith("ru") else "uz"


@bot.message_handler(commands=["start"])
def h_start(m):
    L = M[user_lang(m.from_user)]
    safe_send(m.chat.id, "👋", reply_markup=types.ReplyKeyboardRemove())
    kb = types.InlineKeyboardMarkup()
    if PUBLIC_URL:
        kb.add(types.InlineKeyboardButton(L["open"], web_app=types.WebAppInfo(url=f"{PUBLIC_URL}/?v={VERSION}")))
    safe_send(m.chat.id, L["welcome"], reply_markup=kb)


@bot.message_handler(commands=["id"])
def h_id(m):
    safe_send(m.chat.id, f"Chat ID: {m.chat.id}\nSizning ID: {m.from_user.id}")


@bot.message_handler(commands=["admin"])
def h_admin(m):
    if m.from_user.id not in ADMIN_IDS:
        return safe_send(m.chat.id, "Ruxsat yo'q.")
    if m.chat.type != "private":
        return safe_send(m.chat.id, "Admin panelni bot bilan shaxsiy chatda oching.")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⚙️ Admin panel", web_app=types.WebAppInfo(url=f"{PUBLIC_URL}/admin?v={VERSION}")))
    safe_send(m.chat.id, "Admin panel:", reply_markup=kb)


@bot.message_handler(content_types=["photo"])
def h_photo(m):
    if m.chat.type != "private":
        return
    uid = m.from_user.id
    with engine.begin() as c:
        row = c.execute(
            select(orders)
            .where(orders.c.user_id == uid, orders.c.status.in_(["wait_receipt", "checking", "rejected"]))
            .order_by(desc(orders.c.id)).limit(1)
        ).mappings().first()
        if not row:
            return safe_send(m.chat.id, M[user_lang(m.from_user)]["noorder"])
        c.execute(update(orders).where(orders.c.id == row["id"]).values(status="checking"))
    n = row["id"]
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"ok:{n}"),
        types.InlineKeyboardButton("❌ Rad etish", callback_data=f"no:{n}"),
    )
    cap = f"🧾 Buyurtma #{n} cheki\n👤 {row['name']} · {row['phone']}\n💰 {som(row['total'])}"
    for t in targets():
        try:
            bot.send_photo(t, m.photo[-1].file_id, caption=cap, reply_markup=kb)
        except Exception as e:
            print("send_photo xato:", e)
    safe_send(m.chat.id, M[row["lang"] or "uz"]["checking"].format(n=n))


def apply_pay(oid, status):
    """To'lov holatini o'zgartiradi va mijozga xabar yuboradi. Buyurtma topilmasa None."""
    with engine.begin() as cn:
        row = cn.execute(select(orders).where(orders.c.id == oid)).mappings().first()
        if not row:
            return None
        cn.execute(update(orders).where(orders.c.id == oid).values(status=status))
    sent = safe_send(row["user_id"], M[row["lang"] or "uz"][status].format(n=oid))
    return bool(sent)


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data[:3] in ("ok:", "no:"))
def h_cb(c):
    if c.from_user.id not in ADMIN_IDS:
        return bot.answer_callback_query(c.id, "Ruxsat yo'q")
    act, oid = c.data.split(":")
    status = "paid" if act == "ok" else "rejected"
    if apply_pay(int(oid), status) is None:
        return bot.answer_callback_query(c.id, "Topilmadi")
    label = "✅ To'lov tasdiqlandi" if status == "paid" else "❌ Rad etildi"
    try:
        bot.edit_message_caption(
            caption=(c.message.caption or "") + f"\n\n{label}",
            chat_id=c.message.chat.id, message_id=c.message.message_id,
        )
    except Exception as e:
        print("edit xato:", e)
    bot.answer_callback_query(c.id, label)


# =====================  TELEGRAM AUTH  =====================
def check_init(init_data: str) -> dict:
    if not init_data:
        raise HTTPException(401, "Telegram ichida oching")
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    h = data.pop("hash", None)
    if not h:
        raise HTTPException(401, "hash yo'q")
    s = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, s.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, h):
        raise HTTPException(401, "imzo noto'g'ri")
    if time.time() - int(data.get("auth_date", "0") or 0) > 3 * 86400:
        raise HTTPException(401, "eskirgan, qayta oching")
    try:
        return json.loads(data["user"])
    except Exception:
        raise HTTPException(401, "user yo'q")


def require_admin(init_data: str) -> dict:
    u = check_init(init_data)
    if u["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Ruxsat yo'q")
    return u


# =====================  MODELLAR  =====================
class ImgIn(BaseModel):
    id: Optional[int] = None         # mavjud rasm
    data: Optional[str] = None       # yangi rasm (data URL)


class ProductIn(BaseModel):
    name_uz: str
    name_ru: str = ""
    cat: str
    cost: int = 0
    price: int
    old_price: Optional[int] = None
    active: bool = True
    sold_out: bool = False
    desc_uz: str = ""
    desc_ru: str = ""
    sizes: str = ""                  # "S, M, L"
    colors: str = ""                 # "Qora, Oq"
    images: Optional[List[ImgIn]] = None   # None = rasmlar o'zgarmaydi; ro'yxat = shu tartibda saqlanadi


class OrderItem(BaseModel):
    id: int
    qty: int
    size: str = ""
    color: str = ""


class OrderIn(BaseModel):
    name: str
    phone: str
    region: str
    region_i: str = ""
    city: str
    post: str
    lang: str = "uz"
    items: List[OrderItem]


class ProfileIn(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    post: Optional[str] = None
    lang: Optional[str] = None
    theme: Optional[str] = None


class CartLine(BaseModel):
    id: int
    qty: int
    size: str = ""
    color: str = ""


class CartIn(BaseModel):
    items: List[CartLine]


class ShipIn(BaseModel):
    stage: str


class PayIn(BaseModel):
    ok: bool


def split_opts(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def clean_image(s):
    if not s:
        return None
    if not s.startswith("data:image/") or "," not in s or len(s) > 900_000:
        raise HTTPException(400, "Rasm noto'g'ri yoki juda katta")
    return s


def check_product(p: ProductIn):
    if p.cat not in CATS:
        raise HTTPException(400, "Kategoriya noto'g'ri")
    if not p.name_uz.strip():
        raise HTTPException(400, "Nom kiritilmagan")
    if p.price <= 0 or p.cost < 0:
        raise HTTPException(400, "Narx noto'g'ri")


def prod_vals(p: ProductIn):
    return dict(
        name_uz=p.name_uz.strip(), name_ru=(p.name_ru or p.name_uz).strip(), cat=p.cat,
        cost=p.cost, price=p.price, old_price=p.old_price or None, active=p.active, sold_out=p.sold_out,
        desc_uz=p.desc_uz.strip(), desc_ru=p.desc_ru.strip(),
        sizes=", ".join(split_opts(p.sizes)), colors=", ".join(split_opts(p.colors)),
        updated=int(time.time()),
    )


def save_images(c, pid, imgs):
    existing = set(c.execute(select(product_images.c.id).where(product_images.c.product_id == pid)).scalars())
    keep = []
    for pos, im in enumerate(imgs[:MAX_IMAGES]):
        if im.id and im.id in existing:
            c.execute(update(product_images).where(product_images.c.id == im.id).values(pos=pos))
            keep.append(im.id)
        elif im.data:
            data = clean_image(im.data)
            r = c.execute(insert(product_images).values(product_id=pid, data=data, pos=pos))
            keep.append(r.inserted_primary_key[0])
    c.execute(delete(product_images).where(product_images.c.product_id == pid,
                                           product_images.c.id.notin_(keep)))
    c.execute(update(products).where(products.c.id == pid).values(has_img=bool(keep)))


def image_ids(c, pid):
    return list(c.execute(
        select(product_images.c.id).where(product_images.c.product_id == pid)
        .order_by(product_images.c.pos, product_images.c.id)
    ).scalars())


def pub(r):
    return {
        "id": r["id"], "name_uz": r["name_uz"], "name_ru": r["name_ru"] or r["name_uz"],
        "cat": r["cat"], "price": r["price"], "old_price": r["old_price"],
        "img": f"/img/{r['id']}?v={r['updated']}" if r["has_img"] else None,
        "has_var": bool(split_opts(r["sizes"]) or split_opts(r["colors"])),
        "sold_out": bool(r["sold_out"]),
    }


def serve_data_url(img):
    if not img or "," not in img:
        raise HTTPException(404)
    head, b64 = img.split(",", 1)
    mime = head[5:].split(";")[0] or "image/jpeg"
    return Response(base64.b64decode(b64), media_type=mime,
                    headers={"Cache-Control": "public, max-age=31536000"})


# =====================  ILOVA  =====================
@asynccontextmanager
async def lifespan(app):
    migrate()
    if BOT_TOKEN and PUBLIC_URL:
        try:
            bot.set_webhook(url=f"{PUBLIC_URL}/webhook/{SECRET}", allowed_updates=["message", "callback_query"])
            bot.set_chat_menu_button(menu_button=types.MenuButtonWebApp(
                type="web_app", text="🛍 Do'kon", web_app=types.WebAppInfo(f"{PUBLIC_URL}/?v={VERSION}")))
            print("Webhook o'rnatildi:", PUBLIC_URL)
        except Exception as e:
            print("Webhook xato:", e)
    yield


app = FastAPI(lifespan=lifespan)
NOCACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
def page_shop():
    return FileResponse(os.path.join(BASE, "index.html"), headers=NOCACHE)


@app.get("/admin")
def page_admin():
    return FileResponse(os.path.join(BASE, "admin.html"), headers=NOCACHE)


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return {"ok": True}


@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != SECRET:
        raise HTTPException(403)
    upd = telebot.types.Update.de_json((await request.body()).decode("utf-8"))
    await run_in_threadpool(bot.process_new_updates, [upd])
    return {"ok": True}


# ---------- ommaviy ----------
@app.get("/api/config")
def api_config():
    return {"card": CARD, "owner": CARD_OWNER, "support": SUPPORT, "stages": STAGES}


@app.get("/api/products")
def api_products():
    with engine.connect() as c:
        rows = c.execute(
            select(*PRODUCT_COLS).where(products.c.active == True).order_by(desc(products.c.id))  # noqa: E712
        ).mappings().all()
    return [pub(r) for r in rows]


@app.get("/api/products/{pid}")
def api_product(pid: int):
    with engine.connect() as c:
        r = c.execute(
            select(*PRODUCT_COLS).where(products.c.id == pid, products.c.active == True)  # noqa: E712
        ).mappings().first()
        if not r:
            raise HTTPException(404)
        ids = image_ids(c, pid)
    d = pub(r)
    d.update(
        desc_uz=r["desc_uz"] or "", desc_ru=r["desc_ru"] or r["desc_uz"] or "",
        sizes=split_opts(r["sizes"]), colors=split_opts(r["colors"]),
        images=[f"/pimg/{i}" for i in ids],
    )
    return d


@app.get("/img/{pid}")
def api_img(pid: int):
    """Mahsulotning asosiy (birinchi) rasmi."""
    with engine.connect() as c:
        img = c.execute(
            select(product_images.c.data).where(product_images.c.product_id == pid)
            .order_by(product_images.c.pos, product_images.c.id).limit(1)
        ).scalar()
    return serve_data_url(img)


@app.get("/pimg/{iid}")
def api_pimg(iid: int):
    with engine.connect() as c:
        img = c.execute(select(product_images.c.data).where(product_images.c.id == iid)).scalar()
    return serve_data_url(img)


@app.post("/api/order")
def api_order(o: OrderIn, x_init_data: str = Header("")):
    user = check_init(x_init_data)
    if not o.items or len(o.items) > 50:
        raise HTTPException(400, "Savat bo'sh")
    ids = [i.id for i in o.items]
    with engine.connect() as c:
        rows = {r["id"]: r for r in c.execute(
            select(*PRODUCT_COLS).where(products.c.id.in_(ids), products.c.active == True)  # noqa: E712
        ).mappings().all()}
    items, total = [], 0
    for i in o.items:
        p = rows.get(i.id)
        if not p or i.qty < 1 or i.qty > 99:
            raise HTTPException(400, "Mahsulot topilmadi")
        if p["sold_out"]:
            raise HTTPException(400, f"Tugagan: {p['name_uz']}")
        sizes, colors = split_opts(p["sizes"]), split_opts(p["colors"])
        size, color = i.size.strip(), i.color.strip()
        if sizes and size not in sizes:
            raise HTTPException(400, "O'lchamni tanlang")
        if colors and color not in colors:
            raise HTTPException(400, "Rangni tanlang")
        if not sizes:
            size = ""
        if not colors:
            color = ""
        var = ", ".join(x for x in (size, color) if x)
        name = p["name_uz"] + (f" ({var})" if var else "")
        name_ru = (p["name_ru"] or p["name_uz"]) + (f" ({var})" if var else "")
        items.append({"id": i.id, "name": name, "name_ru": name_ru, "qty": i.qty, "price": p["price"], "cost": p["cost"] or 0})
        total += p["price"] * i.qty
    lang = "ru" if o.lang == "ru" else "uz"
    username = user.get("username")
    with engine.begin() as c:
        r = c.execute(insert(orders).values(
            user_id=user["id"], username=username, name=o.name[:200], phone=o.phone[:50],
            region=o.region[:100], city=o.city[:200], post=o.post[:300],
            items=json.dumps(items, ensure_ascii=False), total=total,
            status="wait_receipt", lang=lang, created=int(time.time()),
        ))
        n = r.inserted_primary_key[0]
        c.execute(delete(cart_items).where(cart_items.c.user_id == user["id"]))   # savat tozalanadi
    upsert_profile(user["id"], name=o.name[:200], phone=o.phone[:50], region=o.region_i[:20],
                   city=o.city[:200], post=o.post[:300])

    lines = "\n".join(f"• {i['name']} × {i['qty']}" for i in items)
    who = f"@{username}" if username else f"id {user['id']}"
    admin_text = (
        f"🆕 Buyurtma #{n}\n\n{lines}\n\n💰 Jami: {som(total)}\n\n"
        f"👤 {o.name}\n📞 {o.phone}\n📍 {o.region}, {o.city}\n📮 Uzpost: {o.post}\n"
        f"Telegram: {who}\n\n⏳ To'lov cheki kutilmoqda"
    )
    for t in targets():
        safe_send(t, admin_text)
    safe_send(user["id"], M[lang]["got"].format(n=n, items=lines, total=som(total), card=CARD, owner=CARD_OWNER))
    return {"ok": True, "id": n}


# ---------- mijoz (har bir mijoz faqat o'z ma'lumotini ko'radi) ----------
def upsert_profile(uid, **vals):
    vals = {k: v for k, v in vals.items() if v is not None}
    vals["updated"] = int(time.time())
    with engine.begin() as c:
        has = c.execute(select(profiles.c.user_id).where(profiles.c.user_id == uid)).first()
        if has:
            c.execute(update(profiles).where(profiles.c.user_id == uid).values(**vals))
        else:
            c.execute(insert(profiles).values(user_id=uid, **vals))


@app.get("/api/profile")
def api_profile_get(x_init_data: str = Header("")):
    u = check_init(x_init_data)
    with engine.connect() as c:
        r = c.execute(select(profiles).where(profiles.c.user_id == u["id"])).mappings().first()
    d = {"name": "", "phone": "", "region": "", "city": "", "post": "", "lang": "", "theme": ""}
    if r:
        for k in d:
            d[k] = r[k] or ""
    if not d["name"]:
        d["name"] = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    d["tg_name"] = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    d["tg_user"] = u.get("username") or ""
    return d


@app.put("/api/profile")
def api_profile_put(p: ProfileIn, x_init_data: str = Header("")):
    u = check_init(x_init_data)
    vals = dict(
        name=None if p.name is None else p.name.strip()[:200],
        phone=None if p.phone is None else p.phone.strip()[:50],
        region=None if p.region is None else p.region.strip()[:20],
        city=None if p.city is None else p.city.strip()[:200],
        post=None if p.post is None else p.post.strip()[:300],
        lang=p.lang if p.lang in ("uz", "ru") else None,
        theme=p.theme if p.theme in ("light", "dark") else None,
    )
    upsert_profile(u["id"], **vals)
    return {"ok": True}


@app.get("/api/cart")
def api_cart_get(x_init_data: str = Header("")):
    u = check_init(x_init_data)
    with engine.connect() as c:
        rows = c.execute(select(cart_items).where(cart_items.c.user_id == u["id"]).order_by(cart_items.c.id)).mappings().all()
    return [{"id": r["product_id"], "size": r["size"] or "", "color": r["color"] or "", "qty": r["qty"]} for r in rows]


@app.put("/api/cart")
def api_cart_put(cart: CartIn, x_init_data: str = Header("")):
    u = check_init(x_init_data)
    merged = {}
    for l in cart.items[:100]:
        key = (l.id, l.size.strip()[:100], l.color.strip()[:100])
        merged[key] = min(99, merged.get(key, 0) + max(1, l.qty))
    with engine.begin() as c:
        ok_ids = set(c.execute(select(products.c.id).where(products.c.id.in_([k[0] for k in merged] or [0]))).scalars())
        c.execute(delete(cart_items).where(cart_items.c.user_id == u["id"]))
        for (pid, size, color), qty in list(merged.items())[:50]:
            if pid in ok_ids:
                c.execute(insert(cart_items).values(user_id=u["id"], product_id=pid, size=size, color=color, qty=qty))
    return {"ok": True}


class FavIn(BaseModel):
    ids: List[int]


@app.get("/api/favs")
def api_favs_get(x_init_data: str = Header("")):
    u = check_init(x_init_data)
    with engine.connect() as c:
        return list(c.execute(select(favorites.c.product_id).where(favorites.c.user_id == u["id"]).order_by(favorites.c.id)).scalars())


@app.put("/api/favs")
def api_favs_put(f: FavIn, x_init_data: str = Header("")):
    u = check_init(x_init_data)
    ids = list(dict.fromkeys(f.ids))[:200]
    with engine.begin() as c:
        c.execute(delete(favorites).where(favorites.c.user_id == u["id"]))
        for pid in ids:
            c.execute(insert(favorites).values(user_id=u["id"], product_id=pid))
    return {"ok": True}


@app.get("/api/my/orders")
def api_my_orders(x_init_data: str = Header("")):
    u = check_init(x_init_data)
    with engine.connect() as c:
        rows = c.execute(
            select(orders).where(orders.c.user_id == u["id"]).order_by(desc(orders.c.id)).limit(50)
        ).mappings().all()
        parsed = [json.loads(r["items"] or "[]") for r in rows]
        pids = {i.get("id") for items in parsed for i in items if i.get("id")}
        imgs = {}
        if pids:
            for pr in c.execute(select(products.c.id, products.c.has_img, products.c.updated).where(products.c.id.in_(pids))):
                if pr[1]:
                    imgs[pr[0]] = f"/img/{pr[0]}?v={pr[2]}"
    out = []
    for r, items in zip(rows, parsed):
        out.append({
            "id": r["id"], "created": r["created"], "total": r["total"],
            "status": r["status"], "ship": r["ship"] or "", "ship_time": r["ship_time"] or 0,
            "items": [{"name": i.get("name", ""), "name_ru": i.get("name_ru") or i.get("name", ""),
                       "qty": i.get("qty", 1), "price": i.get("price", 0), "img": imgs.get(i.get("id"))} for i in items],
        })
    return out


# ---------- admin ----------
@app.get("/api/admin/products")
def adm_list(x_init_data: str = Header("")):
    require_admin(x_init_data)
    with engine.connect() as c:
        rows = c.execute(select(*PRODUCT_COLS).order_by(desc(products.c.id))).mappings().all()
    out = []
    for r in rows:
        d = pub(r)
        d.update(cost=r["cost"], active=r["active"], profit=r["price"] - r["cost"])
        out.append(d)
    return out


@app.get("/api/admin/products/{pid}")
def adm_get(pid: int, x_init_data: str = Header("")):
    require_admin(x_init_data)
    with engine.connect() as c:
        r = c.execute(select(*PRODUCT_COLS).where(products.c.id == pid)).mappings().first()
        if not r:
            raise HTTPException(404)
        ids = image_ids(c, pid)
    return {
        "id": r["id"], "name_uz": r["name_uz"], "name_ru": r["name_ru"], "cat": r["cat"],
        "cost": r["cost"], "price": r["price"], "old_price": r["old_price"], "active": r["active"],
        "sold_out": bool(r["sold_out"]),
        "desc_uz": r["desc_uz"] or "", "desc_ru": r["desc_ru"] or "",
        "sizes": r["sizes"] or "", "colors": r["colors"] or "",
        "images": [{"id": i, "url": f"/pimg/{i}"} for i in ids],
    }


@app.post("/api/admin/products")
def adm_create(p: ProductIn, x_init_data: str = Header("")):
    require_admin(x_init_data)
    check_product(p)
    with engine.begin() as c:
        r = c.execute(insert(products).values(**prod_vals(p), image=None, has_img=False))
        pid = r.inserted_primary_key[0]
        if p.images:
            save_images(c, pid, p.images)
    return {"id": pid}


@app.put("/api/admin/products/{pid}")
def adm_update(pid: int, p: ProductIn, x_init_data: str = Header("")):
    require_admin(x_init_data)
    check_product(p)
    with engine.begin() as c:
        res = c.execute(update(products).where(products.c.id == pid).values(**prod_vals(p)))
        if res.rowcount == 0:
            raise HTTPException(404)
        if p.images is not None:
            save_images(c, pid, p.images)
    return {"ok": True}


@app.delete("/api/admin/products/{pid}")
def adm_delete(pid: int, x_init_data: str = Header("")):
    require_admin(x_init_data)
    with engine.begin() as c:
        c.execute(delete(product_images).where(product_images.c.product_id == pid))
        c.execute(delete(products).where(products.c.id == pid))
    return {"ok": True}


@app.get("/api/admin/orders")
def adm_orders(x_init_data: str = Header("")):
    require_admin(x_init_data)
    with engine.connect() as c:
        rows = c.execute(select(orders).order_by(desc(orders.c.id)).limit(100)).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items"] or "[]")
        out.append(d)
    return out


@app.post("/api/admin/orders/{oid}/ship")
def adm_ship(oid: int, s: ShipIn, x_init_data: str = Header("")):
    """Yetkazib berish bosqichini belgilaydi va aynan shu buyurtma egasiga xabar yuboradi."""
    require_admin(x_init_data)
    st = next((x for x in STAGES if x["k"] == s.stage), None)
    if not st:
        raise HTTPException(400, "Bosqich noto'g'ri")
    with engine.begin() as c:
        row = c.execute(select(orders).where(orders.c.id == oid)).mappings().first()
        if not row:
            raise HTTPException(404)
        c.execute(update(orders).where(orders.c.id == oid).values(ship=st["k"], ship_time=int(time.time())))
    lang = row["lang"] or "uz"
    head = "📦 Buyurtma" if lang == "uz" else "📦 Заказ"
    sent = safe_send(row["user_id"], f"{head} #{oid}\n\n{st[lang]}")
    return {"ok": True, "sent": bool(sent)}


@app.post("/api/admin/orders/{oid}/pay")
def adm_pay(oid: int, p: PayIn, x_init_data: str = Header("")):
    require_admin(x_init_data)
    sent = apply_pay(oid, "paid" if p.ok else "rejected")
    if sent is None:
        raise HTTPException(404)
    return {"ok": True, "sent": sent}


@app.get("/api/admin/stats")
def adm_stats(x_init_data: str = Header("")):
    """Foyda hisoboti: faqat to'lovi tasdiqlangan buyurtmalar. Foyda = (sotuv narxi - Pinduoduo narxi) x soni."""
    require_admin(x_init_data)
    now = int(time.time())
    tz = 5 * 3600                                   # Toshkent UTC+5
    day0 = ((now + tz) // 86400) * 86400 - tz       # bugun 00:00 (Toshkent)
    periods = [("today", "Bugun", day0), ("week", "7 kun", day0 - 6 * 86400),
               ("month", "30 kun", day0 - 29 * 86400), ("all", "Hammasi", 0)]
    with engine.connect() as c:
        rows = c.execute(select(orders.c["items"], orders.c.total, orders.c.status, orders.c.created)).all()
        cost_now = {r[0]: (r[1] or 0) for r in c.execute(select(products.c.id, products.c.cost))}
    out = {k: {"label": lb, "orders": 0, "revenue": 0, "profit": 0} for k, lb, _ in periods}
    pending = 0
    for items_json, total, status, created in rows:
        if status != "paid":
            if status in ("wait_receipt", "checking"):
                pending += 1
            continue
        try:
            items = json.loads(items_json or "[]")
        except Exception:
            items = []
        profit = sum((i.get("price", 0) - i.get("cost", cost_now.get(i.get("id"), 0))) * i.get("qty", 1) for i in items)
        for k, _, start in periods:
            if (created or 0) >= start:
                o = out[k]
                o["orders"] += 1
                o["revenue"] += total or 0
                o["profit"] += profit
    return {"periods": [out[k] for k, _, _ in periods], "pending": pending}
