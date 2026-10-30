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
    BufferedInputFile,
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
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
if not ADMIN_IDS:
    logging.warning("ADMIN_IDS пуст или не числовой: заявки на донат некому отправлять. Узнай ID командой /myid в боте.")

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
# Папки с файлами игроков (перебираются по очереди; у новых версий Minecraft путь другой)
STATS_DIRS = [x.strip() for x in os.getenv("SFTP_STATS_DIRS", f"{SFTP_STATS_DIR},world/players/stats").split(",") if x.strip()]
PLAYERDATA_DIRS = [x.strip() for x in os.getenv("SFTP_PLAYERDATA_DIRS", "world/playerdata,world/players/data").split(",") if x.strip()]
PLAYERDATA_LOCAL_DIR = os.getenv("PLAYERDATA_DIR", "playerdata")

# Выдача привилегий на сервере. Способ 1 (рекомендуется): API панели хостинга (Pterodactyl).
PTERO_URL = os.getenv("PTERO_URL", "").rstrip("/")  # например https://auth.worldhosts.fun
PTERO_API_KEY = os.getenv("PTERO_API_KEY", "")      # Аккаунт -> API Credentials
PTERO_SERVER_ID = os.getenv("PTERO_SERVER_ID", "")  # короткий ID сервера из адреса /server/xxxxxxxx
# Способ 2: RCON (если включён на сервере)
RCON_HOST = os.getenv("RCON_HOST", SERVER_HOST)
RCON_PORT = int(os.getenv("RCON_PORT", "25575"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")
# {nick} и {group} подставляются автоматически (LuckPerms: добавить игроку группу)
RANK_COMMAND = os.getenv("RANK_COMMAND", "lp user {nick} parent add {group}")
# Сообщение всему серверу после выдачи ({nick} и {title}); пусто — не отправлять
ANNOUNCE_COMMAND = os.getenv("ANNOUNCE_COMMAND", "say {nick} поддержал сервер и получил привилегию {title}!")
FONT_FILE = Path(__file__).with_name("font.ttf")

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


def _player_uuid(nick: str) -> str:
    # UUID берём из базы плагина (он совпадает с UUID сервера), иначе считаем offline-UUID
    try:
        row = _fetch_auth_row(nick)
    except Exception:
        row = None
    return (row[1] if row and row[1] else None) or offline_uuid(nick)


def _read_player_file(dirs: list[str], local_dir: str, fname: str) -> bytes | None:
    """Читает файл игрока по SFTP (перебирая папки) или из локальной папки."""
    if SFTP_HOST:
        import paramiko

        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        try:
            transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
            sftp = paramiko.SFTPClient.from_transport(transport)
            for d in dirs:
                try:
                    with sftp.open(f"{d}/{fname}") as f:
                        return f.read()
                except (FileNotFoundError, IOError):
                    continue
            return None
        finally:
            transport.close()
    path = Path(local_dir) / fname
    return path.read_bytes() if path.exists() else None


def _load_stats(nick: str) -> dict | None:
    raw = _read_player_file(STATS_DIRS, STATS_DIR, f"{_player_uuid(nick)}.json")
    return json.loads(raw.decode("utf-8")) if raw else None


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


# ───────────────────────── Инвентарь (файл игрока .dat, формат NBT) ─────────────────────────
def parse_nbt(data: bytes):
    """Минимальный разбор NBT (gzip или без сжатия). Возвращает корневой compound как dict."""
    import gzip
    import struct

    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    pos = 0

    def take(n):
        nonlocal pos
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    def string():
        (n,) = struct.unpack(">H", take(2))
        return take(n).decode("utf-8", "replace")

    def payload(tag):
        if tag == 1:
            return struct.unpack(">b", take(1))[0]
        if tag == 2:
            return struct.unpack(">h", take(2))[0]
        if tag == 3:
            return struct.unpack(">i", take(4))[0]
        if tag == 4:
            return struct.unpack(">q", take(8))[0]
        if tag == 5:
            return struct.unpack(">f", take(4))[0]
        if tag == 6:
            return struct.unpack(">d", take(8))[0]
        if tag == 7:
            (n,) = struct.unpack(">i", take(4))
            return list(take(n))
        if tag == 8:
            return string()
        if tag == 9:
            (t,) = struct.unpack(">b", take(1))
            (n,) = struct.unpack(">i", take(4))
            return [payload(t) for _ in range(n)]
        if tag == 10:
            out = {}
            while True:
                (t,) = struct.unpack(">b", take(1))
                if t == 0:
                    return out
                key = string()
                out[key] = payload(t)
        if tag == 11:
            (n,) = struct.unpack(">i", take(4))
            return list(struct.unpack(f">{n}i", take(4 * n)))
        if tag == 12:
            (n,) = struct.unpack(">i", take(4))
            return list(struct.unpack(f">{n}q", take(8 * n)))
        raise ValueError(f"unknown NBT tag {tag}")

    (root_tag,) = struct.unpack(">b", take(1))
    string()  # имя корня
    return payload(root_tag)


RU_ITEMS = {
    "diamond": "Алмаз", "emerald": "Изумруд", "gold_ingot": "Золотой слиток", "iron_ingot": "Железный слиток",
    "netherite_ingot": "Незеритовый слиток", "coal": "Уголь", "charcoal": "Древесный уголь", "stick": "Палка",
    "apple": "Яблоко", "golden_apple": "Золотое яблоко", "enchanted_golden_apple": "Зачарованное золотое яблоко",
    "bread": "Хлеб", "cooked_beef": "Жареная говядина", "cooked_porkchop": "Жареная свинина",
    "torch": "Факел", "arrow": "Стрела", "bow": "Лук", "crossbow": "Арбалет", "shield": "Щит",
    "elytra": "Элитры", "totem_of_undying": "Тотем бессмертия", "ender_pearl": "Жемчуг Эндера",
    "experience_bottle": "Пузырёк опыта", "firework_rocket": "Фейерверк", "water_bucket": "Ведро воды",
    "lava_bucket": "Ведро лавы", "bucket": "Ведро", "crafting_table": "Верстак", "furnace": "Печь",
    "chest": "Сундук", "ender_chest": "Сундук Края", "shulker_box": "Шалкер", "obsidian": "Обсидиан",
    "cobblestone": "Булыжник", "stone": "Камень", "dirt": "Земля", "sand": "Песок", "gravel": "Гравий",
    "oak_log": "Дубовое бревно", "oak_planks": "Дубовые доски", "glass": "Стекло", "tnt": "TNT",
    "diamond_sword": "Алмазный меч", "diamond_pickaxe": "Алмазная кирка", "diamond_axe": "Алмазный топор",
    "diamond_shovel": "Алмазная лопата", "netherite_sword": "Незеритовый меч",
    "netherite_pickaxe": "Незеритовая кирка", "iron_sword": "Железный меч", "iron_pickaxe": "Железная кирка",
    "diamond_helmet": "Алмазный шлем", "diamond_chestplate": "Алмазный нагрудник",
    "diamond_leggings": "Алмазные поножи", "diamond_boots": "Алмазные ботинки",
    "netherite_helmet": "Незеритовый шлем", "netherite_chestplate": "Незеритовый нагрудник",
    "netherite_leggings": "Незеритовые поножи", "netherite_boots": "Незеритовые ботинки",
    "iron_helmet": "Железный шлем", "iron_chestplate": "Железный нагрудник",
    "iron_leggings": "Железные поножи", "iron_boots": "Железные ботинки",
    "redstone": "Красная пыль", "lapis_lazuli": "Лазурит", "bone": "Кость", "string": "Нить",
    "leather": "Кожа", "wheat": "Пшеница", "carrot": "Морковь", "potato": "Картофель",
    "command_block": "Командный блок", "barrier": "Барьер", "name_tag": "Бирка", "saddle": "Седло",
}


def item_name(item_id: str) -> str:
    short = item_id.split(":", 1)[-1]
    return RU_ITEMS.get(short, short.replace("_", " ").capitalize())


# ───────────── Картинка инвентаря (иконки лежат в icons.zip рядом с bot.py) ─────────────
ICONS_ZIP = Path(__file__).with_name("icons.zip")
_icon_bytes: dict[str, bytes] | None = None
_icon_cache: dict[str, object] = {}


def _icon_for(item_id: str):
    """PIL-картинка 16x16 для предмета или None, если иконки нет."""
    global _icon_bytes
    from PIL import Image

    if _icon_bytes is None:
        _icon_bytes = {}
        if ICONS_ZIP.exists():
            import zipfile

            with zipfile.ZipFile(ICONS_ZIP) as z:
                _icon_bytes = {n[:-4]: z.read(n) for n in z.namelist() if n.endswith(".png")}
    short = item_id.split(":", 1)[-1]
    if short in _icon_cache:
        return _icon_cache[short]
    img = None
    for variant in (short, f"{short}_front", f"{short}_top", f"{short}_side", f"{short}_00"):
        if variant in _icon_bytes:
            import io

            img = Image.open(io.BytesIO(_icon_bytes[variant])).convert("RGBA")
            break
    _icon_cache[short] = img
    return img


def _font(size: int):
    from PIL import ImageFont

    try:
        if FONT_FILE.exists():
            return ImageFont.truetype(str(FONT_FILE), size)
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


RANK_COLORS = {"Креатив": (67, 160, 71), "Модератор": (30, 136, 229), "Админ": (229, 57, 53)}


def _draw_slot(img, d, x, y, item, cell=64):
    """Один слот в тёмном стиле с фаской; item = (id, count) или None."""
    from PIL import Image

    d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(62, 62, 70, 255))
    d.line([x, y, x + cell - 1, y], fill=(28, 28, 34, 255), width=3)
    d.line([x, y, x, y + cell - 1], fill=(28, 28, 34, 255), width=3)
    d.line([x, y + cell - 1, x + cell - 1, y + cell - 1], fill=(104, 104, 114, 255), width=2)
    d.line([x + cell - 1, y, x + cell - 1, y + cell - 1], fill=(104, 104, 114, 255), width=2)
    if item is None:
        return
    item_id, count = item
    icon_px = 48
    ic = _icon_for(item_id)
    ix, iy = x + (cell - icon_px) // 2, y + (cell - icon_px) // 2
    if ic is not None:
        img.alpha_composite(ic.resize((icon_px, icon_px), Image.NEAREST), (ix, iy))
    else:  # иконки нет — цветная плитка с буквами названия
        hsh = sum(ord(ch) for ch in item_id)
        color = (70 + hsh * 7 % 130, 70 + hsh * 13 % 130, 70 + hsh * 29 % 130, 255)
        d.rounded_rectangle([ix, iy, ix + icon_px, iy + icon_px], radius=8, fill=color)
        short = item_id.split(":", 1)[-1].replace("_", " ")
        d.text((ix + icon_px // 2, iy + icon_px // 2), short[:2].upper(), fill="white", font=_font(18), anchor="mm")
    if count > 1:
        d.text((x + cell - 5, y + cell - 3), str(count), fill="white", font=_font(18), anchor="rd",
               stroke_width=2, stroke_fill=(20, 20, 24, 255))


def render_card(nick: str, ranks: list[str], data: dict) -> bytes:
    """Одна красивая картинка: игрок, донаты, броня, инвентарь, хотбар, эндер-сундук, координаты."""
    import io

    from PIL import Image, ImageDraw

    cell, W, M = 64, 640, 32
    inv = {s: (i, c) for s, i, c in data["inv"]}
    ender = {s: (i, c) for s, i, c in data["ender"]}
    sections = [
        ("Броня и левая рука", [[103, 102, 101, 100, None, -106]], inv),
        ("Инвентарь", [[9 + r * 9 + c for c in range(9)] for r in range(3)], inv),
        ("Хотбар", [list(range(9))], inv),
        ("Эндер-сундук", [[r * 9 + c for c in range(9)] for r in range(3)], ender),
    ]
    header_h, label_h, gap, footer_h = 118, 36, 12, 112
    H = header_h + sum(label_h + len(rows) * cell + gap for _, rows, _ in sections) + footer_h

    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for yy in range(H):  # вертикальный градиент фона
        t = yy / H
        d.line([0, yy, W, yy], fill=(int(34 - 14 * t), int(36 - 14 * t), int(52 - 20 * t), 255))

    # шапка
    d.rounded_rectangle([M - 8, 16, W - M + 8, header_h - 12], radius=16, fill=(48, 50, 68, 255))
    d.text((M, 26), nick, font=_font(34), fill="white")
    px = M
    for name in (ranks or ["Игрок"]):
        color = RANK_COLORS.get(name, (110, 110, 120))
        fnt = _font(17)
        tw = int(d.textlength(name, font=fnt))
        d.rounded_rectangle([px, 72, px + tw + 24, 72 + 28], radius=14, fill=color + (255,))
        d.text((px + 12, 72 + 14), name, font=fnt, fill="white", anchor="lm")
        px += tw + 24 + 10

    # разделы
    y = header_h
    for title, rows, source in sections:
        d.rounded_rectangle([M, y + 8, M + 5, y + 28], radius=2, fill=(255, 193, 7, 255))
        d.text((M + 14, y + 18), title, font=_font(20), fill=(230, 230, 240, 255), anchor="lm")
        y += label_h
        for r, row in enumerate(rows):
            for c, slot in enumerate(row):
                if slot is None:
                    continue
                _draw_slot(img, d, M + c * cell, y + r * cell, source.get(slot), cell)
        y += len(rows) * cell + gap

    # подвал: координаты и счётчики
    main_items = [t for t in data["inv"] if t[0] not in (103, 102, 101, 100, -106)]
    total = sum(c for _, _, c in main_items)
    e_total = sum(c for _, _, c in data["ender"])
    d.line([M - 8, y + 6, W - M + 8, y + 6], fill=(80, 82, 100, 255), width=2)
    small = _font(17)
    d.text((M, y + 20), f"Спавн: {data['spawn'] or 'не задан'}", font=small, fill=(210, 210, 225, 255))
    d.text((M, y + 46), f"Позиция: {data['pos'] or 'неизвестна'}", font=small, fill=(210, 210, 225, 255))
    d.text((M, y + 72), f"Предметов в инвентаре: {total}   ·   в эндер-сундуке: {e_total}",
           font=small, fill=(255, 213, 79, 255))

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def _items_of(lst) -> list[tuple[int, str, int]]:
    """[(слот, id, количество)] — поддерживает старый (Count/byte) и новый (count/int) формат."""
    out = []
    for it in lst or []:
        if not isinstance(it, dict) or "id" not in it:
            continue
        count = it.get("count", it.get("Count", 1))
        out.append((it.get("Slot", 0), it["id"], int(count)))
    return out


def _spawn_and_pos(root: dict) -> tuple[str | None, str | None]:
    def fmt(x, y, z):
        return f"X {int(x)}, Y {int(y)}, Z {int(z)}"

    spawn = None
    resp = root.get("respawn")  # новые версии
    if isinstance(resp, dict):
        p = resp.get("pos") or resp.get("position")
        if isinstance(p, list) and len(p) >= 3:
            spawn = fmt(*p[:3]) + f" ({resp.get('dimension', 'minecraft:overworld').split(':')[-1]})"
    if spawn is None and "SpawnX" in root:  # старые версии
        spawn = fmt(root["SpawnX"], root.get("SpawnY", 0), root["SpawnZ"]) + \
            f" ({str(root.get('SpawnDimension', 'minecraft:overworld')).split(':')[-1]})"
    pos = root.get("Pos")
    where = None
    if isinstance(pos, list) and len(pos) >= 3:
        where = fmt(*pos[:3]) + f" ({str(root.get('Dimension', 'minecraft:overworld')).split(':')[-1]})"
    return spawn, where


def _load_inventory(nick: str) -> dict | None:
    raw = _read_player_file(PLAYERDATA_DIRS, PLAYERDATA_LOCAL_DIR, f"{_player_uuid(nick)}.dat")
    if not raw:
        return None
    root = parse_nbt(raw)
    inv = _items_of(root.get("Inventory"))
    ender = _items_of(root.get("EnderItems"))
    spawn, where = _spawn_and_pos(root)
    return {"inv": inv, "ender": ender, "spawn": spawn, "pos": where}


def _summarize(items: list[tuple[int, str, int]], limit: int = 40) -> tuple[str, int, int]:
    """Строка со списком предметов, всего предметов, занято слотов."""
    total = sum(c for _, _, c in items)
    agg: dict[str, int] = {}
    for _, iid, c in items:
        agg[iid] = agg.get(iid, 0) + c
    rows = sorted(agg.items(), key=lambda kv: -kv[1])
    lines = [f"• {item_name(i)} ×{c}" for i, c in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit} видов")
    return ("\n".join(lines) if lines else "пусто"), total, len(items)


def format_inventory(nick: str, d: dict) -> str:
    armor_slots = {103: "Шлем", 102: "Нагрудник", 101: "Поножи", 100: "Ботинки", -106: "Левая рука"}
    armor = [(s, i, c) for s, i, c in d["inv"] if s in armor_slots]
    main = [(s, i, c) for s, i, c in d["inv"] if s not in armor_slots]
    body, total, used = _summarize(main)
    ender_body, e_total, e_used = _summarize(d["ender"])
    parts = [
        f"🎒 <b>Инвентарь {nick}</b>",
        f"📍 Точка спавна: {d['spawn'] or 'не задана (нет кровати/якоря возрождения)'}",
        f"🧭 Последняя позиция: {d['pos'] or 'неизвестна'}",
        "",
        f"<b>Инвентарь</b> — всего предметов: <b>{total}</b>, занято слотов: {used}/36",
        body,
    ]
    if armor:
        parts += ["", "<b>Броня и левая рука</b>"]
        parts += [f"• {armor_slots[s]}: {item_name(i)}" + (f" ×{c}" if c > 1 else "") for s, i, c in sorted(armor, reverse=True)]
    parts += ["", f"<b>Эндер-сундук</b> — всего предметов: <b>{e_total}</b>, занято слотов: {e_used}/27", ender_body]
    return "\n".join(parts)


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


def _ptero_command(cmd: str) -> str:
    """Отправляет команду в консоль сервера через API панели Pterodactyl."""
    import urllib.request

    req = urllib.request.Request(
        f"{PTERO_URL}/api/client/servers/{PTERO_SERVER_ID}/command",
        data=json.dumps({"command": cmd}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {PTERO_API_KEY}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return f"HTTP {resp.status}"


def run_server_command(cmd: str) -> str:
    """Выполняет команду на Minecraft-сервере: через панель хостинга, иначе через RCON."""
    if PTERO_URL and PTERO_API_KEY and PTERO_SERVER_ID:
        return _ptero_command(cmd)
    if RCON_PASSWORD:
        return _rcon(cmd)
    raise RuntimeError("Не настроена выдача: заполни PTERO_URL/PTERO_API_KEY/PTERO_SERVER_ID или RCON_PASSWORD")


def get_user_ranks(tg_id: int) -> list[str]:
    """Названия купленных (выданных) привилегий пользователя."""
    with db() as c:
        rows = c.execute(
            "SELECT DISTINCT rank FROM donations WHERE tg_id=? AND status='done'", (tg_id,)
        ).fetchall()
    return [RANKS[r["rank"]][0] for r in rows if r["rank"] in RANKS]


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
    ranks = get_user_ranks(m.from_user.id)
    text += f"\n💎 Донат: <b>{', '.join(ranks) if ranks else 'нет'}</b>"
    kb = InlineKeyboardBuilder()
    kb.button(text="🎒 Инвентарь", callback_data="inv")
    if not ranks:
        kb.button(text="💎 Купить донат", callback_data="gotodonate")
    kb.adjust(1)
    await m.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "gotodonate")
async def goto_donate(cb: CallbackQuery):
    await cb.answer()
    await _show_donate(cb.message, cb.from_user.id)


@router.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


@router.callback_query(F.data == "inv")
async def inventory(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /register", show_alert=True)
        return
    await cb.answer()
    try:
        data = await asyncio.to_thread(_load_inventory, user["nick"])
    except Exception as e:
        logging.exception("inventory failed")
        extra = f"\n{type(e).__name__}: {e}"[:300] if os.getenv("DEBUG_ERRORS") == "1" else ""
        await cb.message.answer("Не удалось прочитать инвентарь с сервера." + extra)
        return
    if data is None:
        await cb.message.answer(
            "Файл игрока на сервере не найден. Зайди на сервер хотя бы раз; "
            "если уже заходил — администратору нужно проверить доступ по SFTP."
        )
        return
    try:  # картинка-карточка (если не получилось — остаётся текстовый список ниже)
        ranks = get_user_ranks(cb.from_user.id)
        png = await asyncio.to_thread(render_card, user["nick"], ranks, data)
        await cb.message.answer_photo(BufferedInputFile(png, "inventory.png"), caption=f"🎒 {user['nick']}")
    except Exception:
        logging.exception("render failed")
    text = format_inventory(user["nick"], data)
    for i in range(0, len(text), 4000):  # лимит Telegram — 4096 символов
        await cb.message.answer(text[i:i + 4000], parse_mode="HTML")
    await cb.message.answer("ℹ️ Данные обновляются, когда сервер сохраняет игрока (при выходе и раз в несколько минут).")


@router.message(F.text == "🖥 Сервер")
@router.message(Command("server"))
async def server(m: Message):
    await m.answer(await server_status())


ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@s1hum1")


async def _show_donate(message: Message, user_id: int):
    if not get_user(user_id):
        await message.answer("Сначала зарегистрируйся: /register")
        return
    owned = get_user_ranks(user_id)
    kb = InlineKeyboardBuilder()
    for key, (title, price, _) in RANKS.items():
        mark = "✅ " if title in owned else ""
        kb.button(text=f"{mark}{title} — {price} ₽", callback_data=f"don:{key}")
    kb.adjust(1)
    head = f"💎 Твой донат: <b>{', '.join(owned)}</b>\n\n" if owned else ""
    await message.answer(head + "Выбери привилегию:", parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(F.text == "💎 Донат")
@router.message(Command("donate"))
async def donate(m: Message):
    await _show_donate(m, m.from_user.id)


@router.callback_query(F.data.startswith("don:"))
async def donate_pick(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in RANKS:
        await cb.answer()
        return
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
    if key not in RANKS:
        await cb.answer()
        return
    title, price, _ = RANKS[key]
    user = get_user(cb.from_user.id)
    if not user:
        await cb.answer("Сначала /register", show_alert=True)
        return
    with db() as c:
        dup = c.execute(
            "SELECT id FROM donations WHERE tg_id=? AND rank=? AND status IN ('pending','processing')",
            (cb.from_user.id, key),
        ).fetchone()
        if dup:
            await cb.answer(f"Заявка #{dup['id']} уже отправлена, ожидай подтверждения.", show_alert=True)
            return
        cur = c.execute(
            "INSERT INTO donations (tg_id, nick, rank, amount) VALUES (?, ?, ?, ?)",
            (cb.from_user.id, user["nick"], key, price),
        )
        did = cur.lastrowid
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить и выдать", callback_data=f"ok:{did}")
    kb.button(text="❌ Отклонить", callback_data=f"no:{did}")
    kb.adjust(1)
    username = f"@{cb.from_user.username}" if cb.from_user.username else "без username"
    delivered = 0
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"💰 Новая заявка на донат #{did}\n\n"
                f"👤 Ник в игре: {user['nick']}\n"
                f"✈️ Telegram: {username} (id {cb.from_user.id})\n"
                f"💎 Привилегия: {title}\n"
                f"💵 Сумма: {price} ₽\n\n"
                "Проверь, что оплата пришла, и нажми «Подтвердить» — привилегия выдастся на сервере сама.",
                reply_markup=kb.as_markup(),
            )
            delivered += 1
        except Exception:
            logging.exception("notify admin failed (admin id %s)", admin)
    if delivered:
        await cb.message.answer(f"Заявка #{did} отправлена админу. Привилегия «{title}» будет выдана после проверки оплаты.")
    else:
        await cb.message.answer(
            f"Заявка #{did} создана, но уведомление админу не дошло. Напиши {ADMIN_CONTACT} и назови номер заявки."
        )
    await cb.answer()


@router.callback_query(F.data.startswith(("ok:", "no:")))
async def donate_decide(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Только для админов", show_alert=True)
        return
    action, did = cb.data.split(":")
    new_status = "rejected" if action == "no" else "processing"
    with db() as c:  # атомарно «занимаем» заявку, чтобы двойной клик не выдал донат дважды
        cur = c.execute("UPDATE donations SET status=? WHERE id=? AND status='pending'", (new_status, int(did)))
        claimed = cur.rowcount == 1
        row = c.execute("SELECT * FROM donations WHERE id=?", (int(did),)).fetchone()
    if not claimed or not row:
        await cb.answer("Заявка уже обработана", show_alert=True)
        return
    if action == "no":
        await bot.send_message(row["tg_id"], f"❌ Заявка #{did} отклонена. Если это ошибка — напиши {ADMIN_CONTACT}.")
        await cb.message.edit_text(cb.message.text + "\n\n❌ Отклонено")
        await cb.answer()
        return
    title, _, group = RANKS[row["rank"]]
    try:
        await asyncio.to_thread(run_server_command, RANK_COMMAND.format(nick=row["nick"], group=group))
    except Exception as e:
        logging.exception("grant failed")
        with db() as c:  # вернуть в очередь, чтобы можно было нажать ещё раз
            c.execute("UPDATE donations SET status='pending' WHERE id=?", (row["id"],))
        await cb.answer(f"Не удалось выдать на сервере: {type(e).__name__}: {e}"[:190], show_alert=True)
        return
    if ANNOUNCE_COMMAND:
        try:
            await asyncio.to_thread(run_server_command, ANNOUNCE_COMMAND.format(nick=row["nick"], title=title))
        except Exception:
            logging.exception("announce failed")
    with db() as c:
        c.execute("UPDATE donations SET status='done' WHERE id=?", (row["id"],))
    await bot.send_message(row["tg_id"], f"🎉 Привилегия «{title}» выдана на сервере! Спасибо за поддержку. Перезайди в игру, если права не появились сразу.")
    await cb.message.edit_text(cb.message.text + "\n\n✅ Выдано на сервере")
    await cb.answer("Выдано!")


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
