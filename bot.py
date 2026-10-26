import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from mcstatus import JavaServer

load_dotenv(encoding="utf-8-sig")
logging.basicConfig(level=logging.INFO)

# ───────────────────────── Конфиг ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

SERVER_HOST = os.getenv("SERVER_HOST", "epyc2.worldhosts.fun")
SERVER_PORT = int(os.getenv("SERVER_PORT", "22180"))

# Плагин авторизации на сервере (ServerAUTH): mysql | sqlite
AUTH_MODE = os.getenv("AUTH_MODE", "mysql")
AUTH_SQLITE_PATH = os.getenv("AUTH_SQLITE_PATH", "auth.db")
AUTH_TABLE = os.getenv("AUTH_TABLE", "auth_users")
AUTH_USER_COLUMN = os.getenv("AUTH_USER_COLUMN", "username_lower")
AUTH_HASH_COLUMN = os.getenv("AUTH_HASH_COLUMN", "password_hash")
AUTH_UUID_COLUMN = os.getenv("AUTH_UUID_COLUMN", "uuid")
MYSQL_HOST = os.getenv("MYSQL_HOST", "")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("MYSQL_DB", "")

# Статистика игрока (папка world/stats)
STATS_DIR = os.getenv("STATS_DIR", "stats")
SFTP_HOST = os.getenv("SFTP_HOST", "")
SFTP_PORT = int(os.getenv("SFTP_PORT", "22"))
SFTP_USER = os.getenv("SFTP_USER", "")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", "")
SFTP_STATS_DIR = os.getenv("SFTP_STATS_DIR", "world/stats")

# RCON для выдачи привилегий
RCON_HOST = os.getenv("RCON_HOST", SERVER_HOST)
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")
# {nick} и {group} подставляются автоматически (пример для LuckPerms)
RANK_COMMAND = os.getenv("RANK_COMMAND", "lp user {nick} parent set {group}")

PAYMENT_DETAILS = os.getenv("PAYMENT_DETAILS", "Реквизиты для оплаты: укажи в .env (PAYMENT_DETAILS)")
COMMUNITY_TEXT = os.getenv("COMMUNITY_TEXT", "Наше сообщество:")
COMMUNITY_LINKS = [x for x in os.getenv("COMMUNITY_LINKS", "").split(",") if x]  # "Название|https://url,..."
CREATORS_TEXT = os.getenv("CREATORS_TEXT", "Создатели сервера: укажи в .env (CREATORS_TEXT)")

# Привилегии: ключ -> (название, цена в рублях, группа на сервере)
RANKS = {
    "creative": ("Креатив", 10, os.getenv("GROUP_CREATIVE", "creative")),
    "moder": ("Модератор", 30, os.getenv("GROUP_MODER", "moder")),
    "admin": ("Админ", 20, os.getenv("GROUP_ADMIN", "admin")),
}

# ───────────────────────── База бота ─────────────────────────
DB_PATH = os.getenv("BOT_DB", "bot.db")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            "tg_id INTEGER PRIMARY KEY, nick TEXT NOT NULL, "
            "created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS donations ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, nick TEXT, "
            "rank TEXT, amount INTEGER, status TEXT DEFAULT 'pending', "
            "created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )


def get_user(tg_id: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


# ───────────────────── Авторизация сервера (ServerAUTH) ─────────────────────
def _fetch_auth_row(nick: str) -> tuple[str, str | None] | None:
    """Возвращает (хеш пароля, uuid игрока) из таблицы плагина или None, если игрока нет."""
    nick = nick.lower()
    query = (
        f"SELECT {AUTH_HASH_COLUMN}, {AUTH_UUID_COLUMN} "
        f"FROM {AUTH_TABLE} WHERE {AUTH_USER_COLUMN}=%s"
    )
    if AUTH_MODE == "mysql":
        import pymysql

        conn = pymysql.connect(
            host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
            password=MYSQL_PASSWORD, database=MYSQL_DB, connect_timeout=10,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(query, (nick,))
                row = cur.fetchone()
                return (row[0], row[1]) if row else None
        finally:
            conn.close()
    conn = sqlite3.connect(AUTH_SQLITE_PATH)
    try:
        row = conn.execute(query.replace("%s", "?"), (nick,)).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def _verify_password(password: str, stored: str) -> bool:
    """ServerAUTH хранит пароли в bcrypt ($2a$/$2b$/$2y$...)."""
    if stored.startswith("$2"):
        import bcrypt

        try:
            return bcrypt.checkpw(password.encode(), stored.encode())
        except ValueError:  # например, пароль длиннее 72 байт
            return False
    return False


LAST_ERROR = {"text": ""}


async def check_server_password(nick: str, password: str) -> str:
    """Возвращает: ok | no_player | wrong | error"""
    try:
        row = await asyncio.to_thread(_fetch_auth_row, nick)
        if row is None:
            return "no_player"
        ok = await asyncio.to_thread(_verify_password, password, row[0])
        return "ok" if ok else "wrong"
    except Exception as e:
        logging.exception("Auth check failed")
        LAST_ERROR["text"] = f"{type(e).__name__}: {e}"[:400]
        return "error"


# ───────────────────────── Статистика игрока ─────────────────────────
def offline_uuid(name: str) -> str:
    h = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(h)))


def _load_stats(nick: str) -> dict | None:
    # UUID берём из базы плагина (он совпадает с UUID сервера), иначе считаем offline-UUID
    try:
        row = _fetch_auth_row(nick)
    except Exception:
        row = None
    player_uuid = (row[1] if row and row[1] else None) or offline_uuid(nick)
    fname = f"{player_uuid}.json"
    if SFTP_HOST:
        import paramiko

        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        try:
            transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
            sftp = paramiko.SFTPClient.from_transport(transport)
            with sftp.open(f"{SFTP_STATS_DIR}/{fname}") as f:
                return json.loads(f.read().decode())
        except FileNotFoundError:
            return None
        finally:
            transport.close()
    path = Path(STATS_DIR) / fname
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


async def get_player_stats(nick: str) -> dict | None:
    try:
        data = await asyncio.to_thread(_load_stats, nick)
    except Exception:
        logging.exception("stats load failed")
        return None
    if not data:
        return None
    custom = data.get("stats", {}).get("minecraft:custom", {})
    cm = sum(custom.get(k, 0) for k in (
        "minecraft:walk_one_cm", "minecraft:sprint_one_cm", "minecraft:crouch_one_cm",
    ))
    ticks = custom.get("minecraft:play_time", custom.get("minecraft:play_one_minute", 0))
    return {
        "blocks": cm // 100,
        "hours": round(ticks / 20 / 3600, 1),
        "deaths": custom.get("minecraft:deaths", 0),
        "mob_kills": custom.get("minecraft:mob_kills", 0),
        "jumps": custom.get("minecraft:jump", 0),
    }


# ───────────────────────── Сервер / RCON ─────────────────────────
async def server_status() -> str:
    try:
        server = JavaServer(SERVER_HOST, SERVER_PORT)
        st = await server.async_status()
        players = ", ".join(p.name for p in (st.players.sample or [])) or "—"
        return (
            f"🟢 Сервер онлайн\n"
            f"Адрес: {SERVER_HOST}:{SERVER_PORT}\n"
            f"Версия: {st.version.name}\n"
            f"Онлайн: {st.players.online}/{st.players.max}\n"
            f"Игроки: {players}\n"
            f"Пинг: {round(st.latency)} мс"
        )
    except Exception:
        return f"🔴 Сервер недоступен\nАдрес: {SERVER_HOST}:{SERVER_PORT}"


def _rcon(cmd: str) -> str:
    from mcrcon import MCRcon

    with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as m:
        return m.command(cmd)


# ───────────────────────── Клавиатуры ─────────────────────────
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🖥 Сервер")],
        [KeyboardButton(text="💎 Донат"), KeyboardButton(text="👥 Сообщество")],
        [KeyboardButton(text="🛠 Создатели")],
    ],
    resize_keyboard=True,
)


class Reg(StatesGroup):
    nick = State()
    password = State()


router = Router()


# ───────────────────────── Хендлеры ─────────────────────────
@router.message(CommandStart())
async def start(m: Message):
    await m.answer(
        f"Привет! Это бот Minecraft-сервера\n<code>{SERVER_HOST}:{SERVER_PORT}</code>\n\n"
        "Здесь можно посмотреть профиль, статус сервера и купить донат.\n"
        "Для начала зарегистрируйся: /register",
        reply_markup=MENU,
        parse_mode="HTML",
    )


@router.message(Command("register"))
async def register(m: Message, state: FSMContext):
    if get_user(m.from_user.id):
        await m.answer("Ты уже зарегистрирован. Открой «👤 Профиль».")
        return
    await state.set_state(Reg.nick)
    await m.answer("Введи свой ник на сервере (ты должен уже зарегистрироваться в игре через /register):")


@router.message(Reg.nick)
async def reg_nick(m: Message, state: FSMContext):
    nick = (m.text or "").strip()
    if not (3 <= len(nick) <= 16) or not nick.replace("_", "").isalnum():
        await m.answer("Некорректный ник (3–16 символов, буквы/цифры/_). Попробуй ещё раз:")
        return
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE lower(nick)=?", (nick.lower(),)).fetchone():
            await m.answer("Этот ник уже привязан к другому аккаунту Telegram.")
            await state.clear()
            return
    await state.update_data(nick=nick)
    await state.set_state(Reg.password)
    await m.answer("Теперь введи пароль, который ты вводил на сервере. Сообщение с паролем я удалю.")


@router.message(Reg.password)
async def reg_password(m: Message, state: FSMContext):
    data = await state.get_data()
    nick = data["nick"]
    password = m.text or ""
    try:
        await m.delete()
    except Exception:
        pass
    res = await check_server_password(nick, password)
    if res == "ok":
        with db() as c:
            c.execute("INSERT OR REPLACE INTO users (tg_id, nick) VALUES (?, ?)", (m.from_user.id, nick))
        await state.clear()
        await m.answer(f"✅ Готово! Ник <b>{nick}</b> привязан.", parse_mode="HTML", reply_markup=MENU)
    elif res == "no_player":
        await state.clear()
        await m.answer("Такого игрока нет в базе сервера. Сначала зарегистрируйся в игре (/register), затем повтори /register здесь.")
    elif res == "wrong":
        await m.answer("❌ Неверный пароль. Попробуй ещё раз или нажми /start для отмены.")
    else:
        await state.clear()
        extra = f"\n\nПричина: {LAST_ERROR['text']}" if os.getenv("DEBUG_ERRORS") == "1" else ""
        await m.answer("Не удалось проверить пароль (ошибка связи с базой сервера). Сообщи администратору." + extra)


@router.message(F.text == "👤 Профиль")
@router.message(Command("profile"))
async def profile(m: Message):
    user = get_user(m.from_user.id)
    if not user:
        await m.answer("Сначала зарегистрируйся: /register")
        return
    nick = user["nick"]
    stats = await get_player_stats(nick)
    text = f"👤 <b>{nick}</b>\n"
    if stats:
        text += (
            f"🚶 Пройдено блоков: <b>{stats['blocks']:,}</b>\n"
            f"⏱ Наиграно: {stats['hours']} ч\n"
            f"💀 Смертей: {stats['deaths']}\n"
            f"⚔️ Убито мобов: {stats['mob_kills']}\n"
            f"🦘 Прыжков: {stats['jumps']:,}"
        ).replace(",", " ")
    else:
        text += "Статистика пока недоступна (игрок ещё не играл или не настроен доступ к stats)."
    await m.answer(text, parse_mode="HTML")


@router.message(F.text == "🖥 Сервер")
@router.message(Command("server"))
async def server(m: Message):
    await m.answer(await server_status())


@router.message(F.text == "💎 Донат")
@router.message(Command("donate"))
async def donate(m: Message):
    if not get_user(m.from_user.id):
        await m.answer("Сначала зарегистрируйся: /register")
        return
    kb = InlineKeyboardBuilder()
    for key, (title, price, _) in RANKS.items():
        kb.button(text=f"{title} — {price} ₽", callback_data=f"don:{key}")
    kb.adjust(1)
    await m.answer("💎 Выбери привилегию:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("don:"))
async def donate_pick(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    title, price, _ = RANKS[key]
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил", callback_data=f"paid:{key}")
    await cb.message.answer(
        f"<b>{title}</b> — {price} ₽\n\n{PAYMENT_DETAILS}\n\n"
        "После оплаты нажми «Я оплатил» — админ проверит платёж и выдаст привилегию.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("paid:"))
async def donate_paid(cb: CallbackQuery, bot: Bot):
    key = cb.data.split(":")[1]
    title, price, _ = RANKS[key]
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /register", show_alert=True)
        return
    with db() as c:
        cur = c.execute(
            "INSERT INTO donations (tg_id, nick, rank, amount) VALUES (?, ?, ?, ?)",
            (cb.from_user.id, user["nick"], key, price),
        )
        did = cur.lastrowid
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выдать", callback_data=f"ok:{did}")
    kb.button(text="❌ Отклонить", callback_data=f"no:{did}")
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"💰 Заявка #{did}\nИгрок: {user['nick']} (@{cb.from_user.username})\n"
                f"Привилегия: {title} — {price} ₽",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            logging.exception("notify admin failed")
    await cb.message.answer(f"Заявка #{did} отправлена админам. Ожидай подтверждения.")
    await cb.answer()


@router.callback_query(F.data.startswith(("ok:", "no:")))
async def donate_decide(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для админов", show_alert=True)
        return
    action, did = cb.data.split(":")
    with db() as c:
        row = c.execute("SELECT * FROM donations WHERE id=?", (int(did),)).fetchone()
        if not row or row["status"] != "pending":
            await cb.answer("Заявка уже обработана", show_alert=True)
            return
        if action == "no":
            c.execute("UPDATE donations SET status='rejected' WHERE id=?", (row["id"],))
            await bot.send_message(row["tg_id"], f"❌ Заявка #{did} отклонена. Если это ошибка — напиши админу.")
            await cb.message.edit_text(cb.message.text + "\n\n❌ Отклонено")
            await cb.answer()
            return
    title, _, group = RANKS[row["rank"]]
    try:
        await asyncio.to_thread(_rcon, RANK_COMMAND.format(nick=row["nick"], group=group))
    except Exception as e:
        await cb.answer(f"Ошибка RCON: {e}", show_alert=True)
        return
    with db() as c:
        c.execute("UPDATE donations SET status='done' WHERE id=?", (row["id"],))
    await bot.send_message(row["tg_id"], f"🎉 Привилегия «{title}» выдана на сервере! Спасибо за поддержку.")
    await cb.message.edit_text(cb.message.text + "\n\n✅ Выдано")
    await cb.answer()


@router.message(F.text == "👥 Сообщество")
@router.message(Command("community"))
async def community(m: Message):
    kb = InlineKeyboardBuilder()
    for item in COMMUNITY_LINKS:
        name, _, url = item.partition("|")
        kb.button(text=name, url=url)
    kb.adjust(1)
    await m.answer(COMMUNITY_TEXT, reply_markup=kb.as_markup() if COMMUNITY_LINKS else None)


@router.message(F.text == "🛠 Создатели")
@router.message(Command("creators"))
async def creators(m: Message):
    await m.answer(CREATORS_TEXT.replace("\\n", "\n"))


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в .env")
    init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
