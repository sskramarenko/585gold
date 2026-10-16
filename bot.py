# -*- coding: utf-8 -*-
"""
Бот отдела компенсаций и льгот (C&B 585).

Умеет:
  • поручения со сроками, напоминания и контроль просрочек
  • мотивационные карты KPI и расчёт процента премии
  • отпуска с напоминанием о передаче дел
  • табель, дни рождения
  • заявки на отгул с согласованием в один клик
  • благодарности коллегам и рейтинг
  • игру «5 букв» для разрядки
  • ИИ-помощника (если задан ключ API)

Запуск: переменные окружения BOT_TOKEN, ALLOWED_CHATS, ADMIN_IDS.
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, ErrorEvent,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

# ---------------------------------------------------------------- настройки

TASK_WARN_DAYS = 2          # за сколько дней напоминать о сроке
VACATION_WARN_DAYS = 14     # за сколько дней предупреждать об отпуске
BIRTHDAY_WARN_DAYS = 3      # за сколько дней напоминать о дне рождения
TIMESHEET_DAYS = (15,)      # числа месяца, когда напоминать про табель (плюс последний день)
KPI_REMIND_DAY = 1          # число месяца для напоминания о факте KPI
MORNING_HOUR = 10           # час утренних напоминаний (МСК)
THANKS_SUMMARY_WEEKDAY = 4  # пятница — итоги благодарностей
THANKS_SUMMARY_HOUR = 17

VERSION = "2026-10-16 · 18:45"   # видно в логах и по команде /version

MSK = timezone(timedelta(hours=3))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "cb_data.json"

ALLOWED_CHATS = {int(x) for x in os.getenv("ALLOWED_CHATS", "").replace(" ", "").split(",")
                 if x.lstrip("-").isdigit()}
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",")
             if x.isdigit()}
# Куда складывать резервные копии: личка с ботом у первого руководителя.
# Хостинг стирает файлы при каждой пересборке, поэтому память живёт в телеграме.
STORAGE_CHAT = int(os.getenv("STORAGE_CHAT", "0") or 0) or (min(ADMIN_IDS) if ADMIN_IDS else 0)
BACKUP_EVERY_SEC = 120        # как часто проверять, не пора ли сохраниться

# ИИ-помощник: работает, если задан ключ
AI_KEY = os.getenv("AI_API_KEY", "").strip()
AI_PROVIDER = os.getenv("AI_PROVIDER", "anthropic").strip().lower()
AI_MODEL = os.getenv("AI_MODEL", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "").strip()
AI_ADMINS_ONLY = os.getenv("AI_ADMINS_ONLY", "0") == "1"
AI_AUTH_SCHEME = os.getenv("AI_AUTH_SCHEME", "Bearer").strip()   # Яндексу нужен Api-Key
AI_SCOPE = os.getenv("AI_SCOPE", "GIGACHAT_API_PERS").strip()    # GigaChat: PERS / B2B / CORP
AI_INSECURE = os.getenv("AI_INSECURE_SSL", "0") == "1"           # если сервис ругается на сертификат

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cb-bot")

MONTHS = ("января февраля марта апреля мая июня июля августа сентября "
          "октября ноября декабря").split()

# ---------------------------------------------------------------- сотрудники

# ключ — как бот называет человека, значения — как его могут написать в команде
EMPLOYEES = {
    "Сергей":  ["сергей", "серёжа", "сережа", "sergey"],
    "Максим":  ["максим", "макс", "арбузов"],
    "Вика Г.": ["вика г", "викаг", "гандрабура", "виктория г",
                "viktoriia_gvv", "viktoriia", "viktoria_g", "vika_g"],
    "Вика К.": ["вика к", "викак", "кудряшова", "виктория к"],
    "Света":   ["света", "светлана", "сенькина"],
    "Лёша":    ["лёша", "леша", "алексей", "звягинцев"],
    "Оля":     ["оля", "ольга", "иванова", "оленька"],
}

# у кого ежемесячная премия по KPI
PREMIUM_STAFF = ["Вика К.", "Света", "Лёша", "Оля"]

# базовый набор KPI: код, название, вес, план, тип (больше/меньше — лучше)
KPI_TEMPLATE = [
    ("проекты",   "Прогресс по проектам",        25, 100, "больше"),
    ("обращения", "Закрытие обращений портала",  25, 100, "больше"),
    ("ошибки",    "Ошибки с фин. потерями",      25, 0,   "меньше"),
    ("smart",     "SMART-задачи",                25, 4,   "больше"),
]


SEPARATORS = "-–—:;,.!|/\\>«»\"'()[]@"


def _norm(t: str) -> str:
    """Имя без знаков препинания: «Вика Г.» и «- вика г:» дают одно и то же."""
    t = t.strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(f"[{re.escape(SEPARATORS)}]", " ", t)).strip()


def strip_seps(text: str) -> str:
    """Убирает ведущие разделители: «- Оля …», «: Оля …»."""
    return text.lstrip(SEPARATORS + " ").strip()


def employee_by_nick(nick: str) -> str | None:
    """Ищет сотрудника по телеграм-нику среди привязок."""
    nick = nick.strip().lstrip("@").lower()
    if not nick:
        return None
    pre = data.get("nicks", {}).get(nick)
    if pre in EMPLOYEES:
        return pre
    for uid, n in data.get("tg", {}).items():
        if n.lower() == nick:
            bound = data.get("bind", {}).get(uid)
            if bound in EMPLOYEES:
                return bound
    return None


def match_employees(text: str) -> list[str]:
    """Все сотрудники, подходящие под кусок имени или телеграм-ник."""
    raw = text.strip()
    by_nick = employee_by_nick(raw)
    if by_nick:
        return [by_nick]
    t = _norm(text)
    if not t:
        return []
    by_nick = employee_by_nick(t.replace(" ", "_"))
    if by_nick:
        return [by_nick]
    found = []
    for name, aliases in EMPLOYEES.items():
        if t == _norm(name):
            return [name]
        for a in aliases:
            a = _norm(a)
            if t == a or a.startswith(t):   # можно писать короче: «леш», «кудряш»
                found.append(name)
                break
    return found


def match_employee(text: str) -> str | None:
    """Один сотрудник, если имя однозначное."""
    found = match_employees(text)
    return found[0] if len(found) == 1 else None


def take_employee(text: str) -> tuple[str | None, str, list[str]]:
    """Отрывает имя от начала строки. Возвращает (кто, остаток, варианты при неоднозначности)."""
    text = strip_seps(text)
    words = text.split()
    if not words:
        return None, "", []
    for n in (2, 1):
        if len(words) < n:
            continue
        found = match_employees(" ".join(words[:n]))
        if len(found) == 1:
            return found[0], strip_seps(" ".join(words[n:])), []
        if len(found) > 1 and n == 1:
            return None, text, found
    return None, text, []


def take_employees(text: str) -> tuple[list[str], str, list[str]]:
    """Несколько имён подряд: «Оля,Лёша,Света текст» или «максим, Вика Г, Света текст»."""
    text = strip_seps(text)
    if not text:
        return [], "", []

    # список через запятую — последний кусок содержит имя и сам текст
    if "," in text:
        chunks = [c for c in text.split(",")]
        people, ok = [], True
        for piece in chunks[:-1]:
            emp = match_employee(piece)
            if not emp:
                ok = False
                break
            if emp not in people:
                people.append(emp)
        if ok and people:
            emp, rest, _ = take_employee(chunks[-1])
            if emp:
                if emp not in people:
                    people.append(emp)
                return people, strip_seps(rest), []

    # имена подряд без запятых
    people: list[str] = []
    rest = text
    while True:
        emp, tail, variants = take_employee(rest)
        if not emp:
            return people, rest, (variants if not people else [])
        if emp not in people:
            people.append(emp)
        rest = tail
        nxt, _, _ = take_employee(rest)
        if not nxt:
            return people, rest, []


PENDING: dict[tuple[int, int], dict] = {}
PENDING_TTL = 600          # сколько секунд ждём уточнения имени


def remember_pending(message, raw: str, kind: str) -> None:
    """Запоминаем команду, в которой имя оказалось неоднозначным."""
    PENDING[(message.chat.id, message.from_user.id)] = {
        "text": message.text or "", "raw": raw, "ts": time.time(),
    }


def ask_who(found: list[str], raw: str) -> str:
    if found:
        return (f"Уточните, кто именно: {', '.join(found)}\n"
                f"<i>Можно просто ответить именем — команду повторять не нужно.</i>")
    return f"Не нашёл сотрудника «{raw}». Известные: {', '.join(EMPLOYEES)}"


# ---------------------------------------------------------------- хранилище

DEFAULT_DATA = {
    "tasks": [],          # поручения
    "kpi": {},            # сотрудник -> {код: {name, weight, plan, fact, type}}
    "kpi_history": {},    # сотрудник -> {"2026-10": {...}}
    "vacations": [],      # отпуска
    "birthdays": {},      # сотрудник -> "ДД.ММ"
    "thanks": [],         # благодарности
    "dayoffs": [],        # заявки на отгул
    "scores": {},         # «5 букв»
    "users": {},          # user_id -> имя в телеграме
    "tg": {},             # user_id -> @ник
    "bind": {},           # user_id -> сотрудник из списка
    "nicks": {},          # @ник -> сотрудник (ждём, пока он напишет)
    "chats": [],          # где бот прописан
    "done": {},           # отметки «уже напоминал»
    "next_id": 1,
}
data: dict = dict(DEFAULT_DATA)


def now_msk() -> datetime:
    return datetime.now(MSK)


DATA_SOURCE = {"from": "empty"}


def load_data() -> None:
    global data
    for path in (DATA_FILE, BASE_DIR / "seed_data.json"):
        if path.exists():
            try:
                data = {**DEFAULT_DATA, **json.loads(path.read_text(encoding="utf-8"))}
                DATA_SOURCE["from"] = "disk" if path is DATA_FILE else "seed"
                log.info("Данные загружены из %s: поручений %s, карт KPI %s",
                         path.name, len(data.get("tasks", [])), len(data.get("kpi", {})))
                return
            except Exception as e:
                log.warning("Не прочитать %s: %s", path.name, e)
    data = dict(DEFAULT_DATA)
    log.info("Начинаю с пустыми данными")


DIRTY = {"flag": False}


def save_data() -> None:
    DIRTY["flag"] = True
    try:
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        log.warning("Не сохранить данные: %s", e)


def next_id() -> int:
    n = data.get("next_id", 1)
    data["next_id"] = n + 1
    return n


def uname(obj) -> str:
    u = obj.from_user
    return u.first_name or u.username or "Коллега"


def remember(obj) -> None:
    u = obj.from_user
    if u and not u.is_bot:
        uid = str(u.id)
        data["users"][uid] = u.first_name or u.username or "Коллега"
        if u.username:
            data["tg"][uid] = u.username
        if uid not in data["bind"]:         # пробуем узнать человека сами
            guess = (data.get("nicks", {}).get((u.username or "").lower())
                     or match_employee(u.username or "")
                     or match_employee(f"{u.first_name or ''} {u.last_name or ''}")
                     or match_employee(u.first_name or ""))
            if guess:
                data["bind"][uid] = guess
    chat = getattr(obj, "chat", None) or obj.message.chat
    if chat.id not in data["chats"] and chat.type in ("group", "supergroup"):
        data["chats"].append(chat.id)


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def parse_date(text: str) -> datetime | None:
    """Понимает 20.10, 20.10.2026, «завтра», «через 3 дня», «в пятницу»."""
    t = text.strip().lower()
    now = now_msk()
    if t in ("сегодня",):
        return now
    if t in ("завтра",):
        return now + timedelta(days=1)
    if t in ("послезавтра",):
        return now + timedelta(days=2)
    m = re.match(r"через\s+(\d+)\s*(?:дн|день|дня|дней)", t)
    if m:
        return now + timedelta(days=int(m.group(1)))
    m = re.match(r"^(\d{1,2})\s+([а-я]{3,})\.?(?:\s+(\d{4}))?$", t)
    if m:
        stem = m.group(2)[:3]
        months3 = ("янв фев мар апр мая июн июл авг сен окт ноя дек").split()
        if stem == "май":
            stem = "мая"
        if stem in months3:
            mo = months3.index(stem) + 1
            d = int(m.group(1))
            y = int(m.group(3) or now.year)
            try:
                dt = now.replace(year=y, month=mo, day=d, hour=18, minute=0, second=0, microsecond=0)
            except ValueError:
                return None
            if not m.group(3) and dt.date() < now.date():
                dt = dt.replace(year=y + 1)
            return dt

    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", t)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or now.year)
        if y < 100:
            y += 2000
        try:
            dt = now.replace(year=y, month=mo, day=d, hour=18, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
        if not m.group(3) and dt.date() < now.date():
            dt = dt.replace(year=y + 1)
        return dt
    return None


def parse_number(text: str, as_percent: bool = False) -> float | None:
    """«20», «20%», «0,2» → 20 при as_percent. Иначе просто число."""
    t = text.strip().replace(",", ".").replace(" ", "")
    had_percent = t.endswith("%")
    t = t.rstrip("%")
    try:
        value = float(t)
    except ValueError:
        return None
    if as_percent and not had_percent and 0 < value < 1:
        value *= 100          # 0,2 — это 20%
    return value


def fmt_date(dt: datetime | str) -> str:
    if isinstance(dt, str):
        dt = datetime.strptime(dt[:10], "%Y-%m-%d")
    return f"{dt.day} {MONTHS[dt.month - 1]}"

# ---------------------------------------------------------------- доступ

router = Router()


def extract_commands(text: str) -> list[str]:
    """Строки-команды из текста: и от человека, и из ответа ИИ."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().strip("`").strip()
        if line.startswith("/") and len(line) > 2:
            out.append(line)
    return out


async def run_commands(message: Message, lines: list[str], limit: int = 12) -> int:
    """Выполняет команды по очереди от имени написавшего."""
    done_count = 0
    for line in lines[:limit]:
        name = line.split()[0].lstrip("/").split("@")[0].lower()
        if name in ("ask", "спроси", "ai"):      # чтобы не зациклиться
            continue
        handler = COMMANDS.get(name)
        if not handler:
            await message.answer(f"⚠️ Команду <code>/{name}</code> не знаю, пропустил.")
            continue
        try:
            await handler(message.model_copy(update={"text": line, "entities": []}))
            done_count += 1
        except Exception as e:
            log.warning("Ошибка в команде %s: %s", line, e)
            await message.answer(f"⚠️ <code>{line[:60]}</code> — не получилось: {e}")
    if len(lines) > limit:
        await message.answer(f"<i>Выполнил первые {limit} команд, остальное пришлите отдельно.</i>")
    return done_count


async def access_guard(handler, event, ctx):
    """Пускает только свои чаты и своих людей, а пачку команд выполняет целиком."""
    message = event if isinstance(event, Message) else event.message
    chat, user = message.chat, event.from_user
    text = (message.text or "") if isinstance(event, Message) else ""

    # проверка доступа
    if not (text.startswith("/chatid") or text.startswith("/whoami") or not ALLOWED_CHATS):
        if chat.type in ("group", "supergroup"):
            if chat.id not in ALLOWED_CHATS:
                log.warning("Чужой чат %s — игнорирую", chat.id)
                return None
        elif str(user.id) not in data["users"]:
            log.warning("Чужая личка от %s — игнорирую", user.id)
            return None

    # несколько команд в одном сообщении — выполняем все по очереди
    if isinstance(event, Message) and text:
        lines = extract_commands(text)
        if len(lines) > 1:
            remember(event)
            await run_commands(event, lines)
            return None

    return await handler(event, ctx)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message):
    await message.answer(
        f"id чата: <code>{message.chat.id}</code>\n"
        f"ваш id: <code>{message.from_user.id}</code>\n\n"
        f"Первое — в переменную ALLOWED_CHATS, второе — в ADMIN_IDS."
    )


@router.message(Command("version", "версия"))
async def cmd_version(message: Message):
    """Какая сборка сейчас работает и что она умеет."""
    has_storage = "да" if STORAGE_CHAT else "нет (пуст ADMIN_IDS)"
    await message.answer(
        f"🔖 <b>Версия бота: {VERSION}</b>\n\n"
        f"Поручений в памяти: {len(data.get('tasks', []))}\n"
        f"Карт KPI: {len(data.get('kpi', {}))} · привязок: {len(data.get('bind', {}))}\n"
        f"Хранилище копий: {has_storage}\n"
        f"ИИ-помощник: {'подключён' if AI_KEY else 'выключен'}\n\n"
        f"<i>Есть /export, /import, /reopen, /closed, /addkpi — значит сборка свежая.</i>"
    )


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    remember(message)
    save_data()
    uid = message.from_user.id
    emp = my_name(uid)
    nick = data["tg"].get(str(uid))
    await message.answer(
        f"Вы <b>{uname(message)}</b>" + (f" (@{nick})" if nick else "") + f", id <code>{uid}</code>\n"
        f"В списке отдела: <b>{emp or 'не узнаю'}</b>\n"
        f"Права руководителя: {'да' if is_admin(uid) else 'нет'}"
        + ("" if emp else "\n\nПредставьтесь: <code>/iam Вика Г.</code> — "
                          "тогда сможете закрывать свои поручения.")
    )


@router.message(Command("iam", "это_я"))
async def cmd_iam(message: Message):
    """Привязка своего аккаунта к сотруднику из списка."""
    remember(message)
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        await message.answer(
            "Напишите, кто вы из списка: <code>/iam Вика Г.</code>\n"
            f"Известные: {', '.join(EMPLOYEES)}"
        )
        return
    emp, _, variants = take_employee(body[1])
    if not emp:
        await message.answer(ask_who(variants, body[1]))
        return
    data["bind"][str(message.from_user.id)] = emp
    save_data()
    await message.answer(f"Запомнил: вы <b>{emp}</b>. Теперь свои поручения можно закрывать.")


@router.message(Command("bind"))
async def cmd_bind(message: Message):
    """Привязка чужого аккаунта: ответом на сообщение, упоминанием, @ником или id."""
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Привязывать чужие аккаунты могут руководители.")
        return

    body = (message.text or "").split(maxsplit=1)
    target_id, target_label = None, ""

    # 1. команда отправлена ответом на сообщение человека — самый надёжный способ
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        target_id = u.id
        target_label = f"@{u.username}" if u.username else (u.first_name or str(u.id))
        data["users"][str(u.id)] = u.first_name or u.username or "Коллега"
        if u.username:
            data["tg"][str(u.id)] = u.username

    # 2. упоминание человека без ника — телеграм прикладывает его id
    if target_id is None:
        for ent in (message.entities or []):
            if ent.type == "text_mention" and ent.user:
                target_id = ent.user.id
                target_label = ent.user.first_name or str(ent.user.id)
                data["users"][str(target_id)] = ent.user.first_name or "Коллега"
                if ent.user.username:
                    data["tg"][str(target_id)] = ent.user.username
                break

    if len(body) < 2:
        await message.answer(
            "Три способа привязать человека:\n\n"
            "1. Ответьте этой командой на любое его сообщение: <code>/bind Лёша</code>\n"
            "2. <code>/bind @ник Лёша</code> — если у него есть ник\n"
            "3. <code>/bind 123456789 Лёша</code> — по id (его покажет /whoami)\n\n"
            "Либо он сам один раз напишет <code>/iam Лёша</code>."
        )
        return

    rest = body[1]
    # 3. первым словом может идти @ник или числовой id
    if target_id is None:
        first = rest.split()[0] if rest.split() else ""
        if first.lstrip("@").isdigit():
            target_id = int(first.lstrip("@"))
            target_label = first
            rest = rest[len(first):]
        elif first.startswith("@"):
            nick = first.lstrip("@")
            uid = next((u for u, n in data["tg"].items() if n.lower() == nick.lower()), None)
            rest = rest[len(first):]
            if uid:
                target_id, target_label = int(uid), f"@{nick}"
            else:
                emp_guess, _, variants = take_employee(rest)
                if not emp_guess:
                    await message.answer(ask_who(variants, rest.strip() or "?"))
                    return
                data["nicks"][nick.lower()] = emp_guess
                save_data()
                await message.answer(
                    f"Запомнил: @{nick} — это <b>{emp_guess}</b>.\n\n"
                    f"Привязка включится, как только он напишет в чат хоть что-нибудь "
                    f"(id по нику Telegram узнать не даёт).\n"
                    f"Хотите сразу — ответьте командой <code>/bind {emp_guess}</code> "
                    f"на любое его сообщение."
                )
                return
    else:
        # убираем упоминание из текста, чтобы осталось только имя сотрудника
        first = rest.split()[0] if rest.split() else ""
        if first.startswith("@") or first.lstrip("@").isdigit():
            rest = rest[len(first):]

    emp, _, variants = take_employee(rest)
    if not emp:
        await message.answer(ask_who(variants, rest.strip() or "?"))
        return

    data["bind"][str(target_id)] = emp
    save_data()
    await message.answer(f"{target_label or target_id} — это <b>{emp}</b>, запомнил.")


@router.message(Command("export", "бэкап"))
async def cmd_export(message: Message):
    """Выгрузить все данные бота одним файлом."""
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Выгрузку делают руководители.")
        return
    save_data()
    stored = await backup_to_telegram(message.bot, "копия по команде /export")
    payload = data_bytes()
    stamp = now_msk().strftime("%Y-%m-%d_%H%M")
    await message.answer_document(
        BufferedInputFile(payload, filename=f"cb_data_{stamp}.json"),
        caption=(f"💾 <b>Резервная копия</b>\n"
                 f"Поручений: {len(data.get('tasks', []))} · "
                 f"карт KPI: {len(data.get('kpi', {}))} · "
                 f"отпусков: {len(data.get('vacations', []))}\n\n"
                 + ("\n✅ Копия закреплена в хранилище — переживёт пересборку хостинга."
                    if stored else
                    "\n⚠️ В хранилище копию положить не вышло: откройте личку с ботом "
                    "и нажмите Start.")
                 + f"\n\nВосстановить: отправьте этот файл в чат и ответьте на него "
                   f"командой <code>/import</code>"),
    )


@router.message(Command("import", "восстановить"))
async def cmd_import(message: Message):
    """Восстановить данные из файла: команда ответом на присланный json."""
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Восстановление делают руководители.")
        return
    doc = message.reply_to_message.document if message.reply_to_message else None
    if not doc:
        await message.answer(
            "Отправьте в чат файл резервной копии и ответьте на него командой "
            "<code>/import</code>.\n\nСделать копию: /export"
        )
        return
    try:
        buf = await message.bot.download(doc)
        loaded = json.loads(buf.read().decode("utf-8"))
        if not isinstance(loaded, dict) or "tasks" not in loaded:
            raise ValueError("не похоже на копию данных бота")
    except Exception as e:
        await message.answer(f"Не смог прочитать файл: {e}")
        return

    global data
    before = len(data.get("tasks", []))
    data = {**DEFAULT_DATA, **loaded}
    save_data()
    await backup_to_telegram(message.bot, "после /import")
    await message.answer(
        f"♻️ <b>Данные восстановлены</b>\n"
        f"Было поручений: {before}, стало: {len(data.get('tasks', []))}\n"
        f"Карт KPI: {len(data.get('kpi', {}))} · отпусков: {len(data.get('vacations', []))}"
    )


@router.message(Command("team", "кто_есть"))
async def cmd_team(message: Message):
    """Кого бот узнаёт в лицо."""
    lines = ["👥 <b>Сотрудники отдела</b>\n"]
    for emp in EMPLOYEES:
        uid = next((u for u, e in data["bind"].items() if e == emp), None)
        if uid:
            nick = data["tg"].get(uid)
            lines.append(f"✅ <b>{emp}</b>" + (f" — @{nick}" if nick else f" — {data['users'].get(uid, '')}"))
        else:
            waiting = [n for n, e in data.get("nicks", {}).items() if e == emp]
            if waiting:
                lines.append(f"🕗 <b>{emp}</b> — @{waiting[0]}, ждём первого сообщения")
            else:
                lines.append(f"⬜ <b>{emp}</b> — аккаунт не привязан")
    lines.append("\n<i>Непривязанный не сможет сам закрыть своё поручение.</i>\n"
                 "Привязать: <code>/iam Вика Г.</code> или <code>/bind @ник Вика Г.</code>")
    await message.answer("\n".join(lines))

# ---------------------------------------------------------------- поручения


@router.message(Command("task", "поручение", "задача"))
async def cmd_task(message: Message):
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Поручения ставят только руководители.")
        return

    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await message.answer(
            "Как ставить поручение:\n"
            "<code>/task Оля подготовить справку по окладам до 20.10</code>\n\n"
            "Нескольким сразу: <code>/task Максим, Вика Г., Света помочь Оле с отчётом до 19 окт</code>"
        )
        return

    body = strip_seps(raw[1])
    deadline, body_wo = None, body
    date_re = (r"(\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?"
               r"|\d{1,2}\s+[а-я]{3,}\.?(?:\s+\d{4})?"
               r"|сегодня|завтра|послезавтра|через\s+\d+\s*дн\w*)")
    for marker in (r"\bдо\s+", r"\bк\s+", r"\bсрок[а-я]*\s*[:—-]?\s*"):
        m = re.search(marker + date_re, body, re.I)
        if m and parse_date(m.group(1)):
            deadline = parse_date(m.group(1))
            # срок в самом конце — убираем из текста, в середине — оставляем как есть
            if m.end() >= len(body.rstrip(". ")) - 1:
                body_wo = body[:m.start()].strip(" .,")
            break

    if deadline is None:
        # слова «сегодня», «завтра», «через 3 дня» без «до» — тоже срок
        m = re.search(r"\b(сегодня|завтра|послезавтра|через\s+\d+\s*дн\w*)\b", body, re.I)
        if m:
            deadline = parse_date(m.group(1))
        else:
            # дата в самом конце строки без предлога
            m = re.search(r"(\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?"
                          r"|\d{1,2}\s+[а-я]{3,}\.?)\s*\.?\s*$", body, re.I)
            if m and parse_date(m.group(1)):
                deadline = parse_date(m.group(1))
                body_wo = body[:m.start()].strip(" .,")

    assignees, what, variants = take_employees(body_wo)
    if not assignees:
        first = body_wo.split()[0] if body_wo.split() else "?"
        if first.startswith("@") or "_" in first:
            nick = first.lstrip("@")
            await message.answer(
                f"Не знаю, кто такой @{nick}.\n\n"
                f"Привяжите ник к сотруднику: <code>/bind @{nick} Света</code>\n"
                f"Или поставьте поручение по имени: <code>/task Света …</code>"
            )
            return
        remember_pending(message, first, "task")
        await message.answer(ask_who(variants, first))
        return
    if not what:
        await message.answer("Не понял, что поручаем. Пример: <code>/task Оля свести оклады до 20.10</code>")
        return
    if not deadline:
        await message.answer(
            "Не вижу срока. Добавьте в конце: <code>… до 20.10</code>, "
            "<code>… до 19 окт</code> или <code>… до завтра</code>"
        )
        return

    created = []
    for emp in assignees:                      # каждому — своё поручение
        tid = next_id()
        data["tasks"].append({
            "id": tid, "what": what, "who": [emp],
            "deadline": deadline.strftime("%Y-%m-%d"),
            "author": uname(message), "chat": message.chat.id,
            "created": now_msk().strftime("%Y-%m-%d"),
            "done": False, "warned": False, "overdue_at": "",
        })
        created.append((tid, emp))
    save_data()

    left = (deadline.date() - now_msk().date()).days
    if len(created) == 1:
        tid, emp = created[0]
        sent = await message.answer(
            f"📌 <b>Поручение №{tid}</b>\n\n{what}\n\n"
            f"Кому: <b>{emp}</b>\n"
            f"Срок: <b>{fmt_date(deadline)}</b> ({left} дн.)\n"
            f"Поставил: {uname(message)}\n\n"
            f"Выполнено: <code>/done {tid}</code>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Выполнено", callback_data=f"t:done:{tid}"),
                InlineKeyboardButton(text="📅 +3 дня", callback_data=f"t:post:{tid}"),
            ]]),
        )
    else:
        lines = "\n".join(f"<b>№{tid}</b> — {emp}" for tid, emp in created)
        rows = [[InlineKeyboardButton(text=f"✅ №{tid} {emp}", callback_data=f"t:done:{tid}")]
                for tid, emp in created]
        sent = await message.answer(
            f"📌 <b>Поручений создано: {len(created)}</b>\n\n{what}\n\n{lines}\n\n"
            f"Срок: <b>{fmt_date(deadline)}</b> ({left} дн.)\n"
            f"Поставил: {uname(message)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    try:
        await sent.pin()
    except Exception:
        pass  # нет прав на закрепление — не страшно


def task_line(t: dict) -> str:
    d = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
    left = (d - now_msk().date()).days
    if t["done"]:
        mark, tail = "✅", "выполнено"
    elif left < 0:
        mark, tail = "🔴", f"просрочено на {-left} дн."
    elif left == 0:
        mark, tail = "🟠", "сегодня"
    elif left <= TASK_WARN_DAYS:
        mark, tail = "🟡", f"осталось {left} дн."
    else:
        mark, tail = "⚪️", f"до {fmt_date(d)}"
    return f"{mark} <b>№{t['id']}</b> {t['what']}\n     <i>{', '.join(t['who'])} — {tail}</i>"


@router.message(Command("tasks", "задачи", "поручения"))
async def cmd_tasks(message: Message):
    """Все открытые поручения или только по одному сотруднику: /tasks Света"""
    remember(message)
    parts = (message.text or "").split(maxsplit=1)
    emp = None
    if len(parts) > 1:
        emp, _, variants = take_employee(parts[1])
        if not emp:
            await message.answer(ask_who(variants, parts[1].strip()))
            return

    open_tasks = [t for t in data["tasks"] if not t["done"]]
    if emp:
        open_tasks = [t for t in open_tasks if emp in t["who"]]
    if not open_tasks:
        await message.answer(
            f"У {emp} открытых поручений нет 👍" if emp else "Открытых поручений нет 🎉"
        )
        return

    open_tasks.sort(key=lambda t: t["deadline"])
    today = now_msk().date()
    overdue = [t for t in open_tasks
               if datetime.strptime(t["deadline"], "%Y-%m-%d").date() < today]
    head = (f"📋 <b>Поручения: {emp}</b> — {len(open_tasks)}" if emp
            else f"📋 <b>Открытые поручения: {len(open_tasks)}</b>")
    if overdue:
        head += f" · просрочено: {len(overdue)}"
    tail = ""
    if emp:
        closed_n = sum(1 for t in data["tasks"] if t["done"] and emp in t["who"])
        if closed_n:
            tail = f"\n\n<i>Закрытых у {emp}: {closed_n} — /closed {emp.split()[0]}</i>"
    else:
        tail = "\n\n<i>По одному человеку: /tasks Света</i>"
    await message.answer(head + "\n\n" + "\n".join(task_line(t) for t in open_tasks) + tail)


@router.message(Command("closed", "закрытые"))
async def cmd_closed(message: Message):
    done = [t for t in data["tasks"] if t["done"]]
    if not done:
        await message.answer("Закрытых поручений пока нет.")
        return
    parts = (message.text or "").split(maxsplit=1)
    emp_filter = None
    if len(parts) > 1:
        emp_filter, _, _ = take_employee(parts[1])
        if emp_filter:
            done = [t for t in done if emp_filter in t["who"]]
    if not done:
        await message.answer(f"У {emp_filter} закрытых поручений нет.")
        return

    done.sort(key=lambda t: t.get("closed_at", ""), reverse=True)
    in_time = 0
    lines = []
    for t in done[:30]:
        deadline = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        closed = datetime.strptime(t.get("closed_at", t["deadline"]), "%Y-%m-%d").date()
        late = (closed - deadline).days
        if late <= 0:
            in_time += 1
            mark, tail = "✅", "в срок"
        else:
            mark, tail = "🟠", f"+{late} дн."
        lines.append(f"{mark} <b>№{t['id']}</b> {t['what'][:60]}\n"
                     f"     <i>{', '.join(t['who'])} · закрыто {fmt_date(closed)} · {tail}</i>")
    head = f"📗 <b>Закрытые поручения" + (f": {emp_filter}" if emp_filter else "") + f"</b>\n"
    head += f"Всего {len(done)}, в срок {in_time} из {min(len(done), 30)} показанных\n"
    head += "<i>Вернуть в работу: /reopen номер</i>\n"
    await message.answer(head + "\n" + "\n".join(lines))


@router.message(Command("my", "мои"))
async def cmd_my_tasks(message: Message):
    me = match_employee(uname(message)) or uname(message)
    mine = [t for t in data["tasks"] if not t["done"] and me in t["who"]]
    if not mine:
        await message.answer(f"За {me} открытых поручений нет.")
        return
    mine.sort(key=lambda t: t["deadline"])
    await message.answer(f"📋 <b>Поручения: {me}</b>\n\n" + "\n".join(task_line(t) for t in mine))


def my_name(user_id: int) -> str | None:
    """Кто это из сотрудников: по ручной привязке, нику или имени в телеграме."""
    uid = str(user_id)
    bound = data.get("bind", {}).get(uid)
    if bound in EMPLOYEES:
        return bound
    nick = data.get("tg", {}).get(uid)
    if nick:
        emp = data.get("nicks", {}).get(nick.lower()) or match_employee(nick)
        if emp:
            return emp
    return match_employee(data["users"].get(uid, ""))


def may_touch(task: dict, user_id: int) -> bool:
    """Трогать поручение может руководитель или тот, кому оно поставлено."""
    return is_admin(user_id) or (my_name(user_id) in task["who"])


def close_task(tid: int, who: str) -> str:
    t = next((x for x in data["tasks"] if x["id"] == tid), None)
    if not t:
        return f"Поручения №{tid} не нашёл."
    if t["done"]:
        return f"Поручение №{tid} уже закрыто."
    t["done"] = True
    t["closed_by"] = who
    t["closed_at"] = now_msk().strftime("%Y-%m-%d")
    save_data()
    d = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
    late = (now_msk().date() - d).days
    tail = f"с опозданием на {late} дн." if late > 0 else "в срок 👍"
    return f"✅ <b>№{tid}</b> {t['what']}\nЗакрыл(а) {who}, {tail}"


@router.message(Command("done", "готово"))
async def cmd_done(message: Message):
    remember(message)
    parts = (message.text or "").split()
    nums = [p for p in parts[1:] if p.strip(SEPARATORS + "№").isdigit()]
    if not nums:
        await message.answer("Напишите так: <code>/done 3</code>")
        return
    out = []
    for n in nums:
        tid = int(n.strip(SEPARATORS + "№"))
        t = next((x for x in data["tasks"] if x["id"] == tid), None)
        if t and not may_touch(t, message.from_user.id):
            out.append(f"№{tid}: закрыть может только исполнитель ({', '.join(t['who'])}) или руководитель.")
        else:
            out.append(close_task(tid, uname(message)))
    await message.answer("\n\n".join(out))


@router.message(Command("extend", "продлить"))
async def cmd_postpone(message: Message):
    remember(message)
    m = re.match(r"^\S+\s+№?(\d+)\s+(?:до\s+)?(.+)$", (message.text or "").strip())
    if not m:
        await message.answer("Напишите так: <code>/extend 3 до 25.10</code>")
        return
    t = next((x for x in data["tasks"] if x["id"] == int(m.group(1))), None)
    if not t:
        await message.answer("Такого поручения нет.")
        return
    if not may_touch(t, message.from_user.id):
        await message.answer(f"Срок меняет исполнитель ({', '.join(t['who'])}) или руководитель.")
        return
    new_date = parse_date(m.group(2).strip())
    if not new_date:
        await message.answer("Не понял дату. Пример: <code>/extend 3 до 25.10</code>")
        return
    old = fmt_date(t["deadline"])
    t["deadline"] = new_date.strftime("%Y-%m-%d")
    t["warned"] = False
    t["overdue_at"] = ""
    save_data()
    await message.answer(f"📅 <b>№{t['id']}</b>: срок перенесён с {old} на <b>{fmt_date(new_date)}</b>")


@router.message(Command("reopen", "вернуть"))
async def cmd_reopen(message: Message):
    """Переоткрыть закрытое поручение: /reopen 3 [до 30.10]"""
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Возвращать поручения в работу могут руководители.")
        return
    m = re.match(r"^\S+\s+№?(\d+)(?:\s+(?:до|к)?\s*(.+))?$", (message.text or "").strip())
    if not m:
        await message.answer("Напишите так: <code>/reopen 3</code> или <code>/reopen 3 до 30.10</code>")
        return

    t = next((x for x in data["tasks"] if x["id"] == int(m.group(1))), None)
    if not t:
        await message.answer(f"Поручения №{m.group(1)} не нашёл.")
        return
    if not t["done"]:
        await message.answer(f"Поручение №{t['id']} и так в работе — срок {fmt_date(t['deadline'])}.")
        return

    t["done"] = False
    t["warned"] = False
    t["overdue_at"] = ""
    closed_by = t.pop("closed_by", None)
    t.pop("closed_at", None)

    tail = ""
    if m.group(2):
        new_date = parse_date(m.group(2).strip())
        if new_date:
            t["deadline"] = new_date.strftime("%Y-%m-%d")
            tail = f"\nНовый срок: <b>{fmt_date(new_date)}</b>"
        else:
            tail = "\n<i>Дату не понял, срок оставил прежним.</i>"
    save_data()

    left = (datetime.strptime(t["deadline"], "%Y-%m-%d").date() - now_msk().date()).days
    when = f"{left} дн." if left >= 0 else f"просрочено на {-left} дн."
    await message.answer(
        f"🔄 <b>Поручение №{t['id']} снова в работе</b>\n\n{t['what']}\n\n"
        f"Кому: <b>{', '.join(t['who'])}</b>\n"
        f"Срок: <b>{fmt_date(t['deadline'])}</b> ({when}){tail}\n"
        + (f"<i>Ранее закрывал(а): {closed_by}</i>\n" if closed_by else "")
        + f"\nВернул(а) в работу: {uname(message)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"t:done:{t['id']}"),
            InlineKeyboardButton(text="📅 +3 дня", callback_data=f"t:post:{t['id']}"),
        ]]),
    )


@router.message(Command("cancel", "снять"))
async def cmd_cancel_task(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Снять поручение может только руководитель.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Напишите так: <code>/cancel 3</code>")
        return
    tid = int(parts[1])
    before = len(data["tasks"])
    data["tasks"] = [t for t in data["tasks"] if t["id"] != tid]
    save_data()
    await message.answer(f"Поручение №{tid} снято." if len(data["tasks"]) < before else "Такого поручения нет.")


@router.callback_query(F.data.startswith("t:"))
async def on_task_button(call: CallbackQuery):
    remember(call)
    _, action, tid = call.data.split(":")
    tid = int(tid)
    t = next((x for x in data["tasks"] if x["id"] == tid), None)
    if not t:
        await call.answer("Поручение не найдено")
        return
    if not may_touch(t, call.from_user.id):
        await call.answer(f"Это поручение для {', '.join(t['who'])}", show_alert=True)
        return
    if action == "done":
        await call.message.answer(close_task(tid, uname(call)))
        await call.answer("Закрыто")
    else:
        new_date = datetime.strptime(t["deadline"], "%Y-%m-%d") + timedelta(days=3)
        t["deadline"] = new_date.strftime("%Y-%m-%d")
        t["warned"] = False
        t["overdue_at"] = ""
        save_data()
        await call.message.answer(f"📅 <b>№{tid}</b>: срок сдвинут на <b>{fmt_date(new_date)}</b>")
        await call.answer("Перенесено")

# ---------------------------------------------------------------- KPI


def kpi_card(emp: str) -> dict:
    """Карта сотрудника: создаётся из шаблона при первом обращении."""
    card = data["kpi"].get(emp)
    if card is None:
        card = {code: {"name": name, "weight": w, "plan": p, "fact": None, "type": tp}
                for code, name, w, p, tp in KPI_TEMPLATE}
        data["kpi"][emp] = card
        save_data()
    return card


def kpi_result(item: dict) -> float | None:
    """Процент выполнения одного KPI. Перевыполнение не ограничено."""
    if item["fact"] is None:
        return None
    plan, fact = float(item["plan"]), float(item["fact"])
    if item.get("type") == "меньше":
        if plan == 0:
            return 100.0 if fact == 0 else 0.0
        return 100.0 if fact <= plan else max(0.0, (1 - (fact - plan) / plan) * 100)
    if plan == 0:
        return 100.0 if fact > 0 else 0.0
    return fact / plan * 100


def premium_percent(emp: str) -> tuple[float, list[str], bool]:
    """Итоговый процент премии, расшифровка и признак полноты данных."""
    card = kpi_card(emp)
    total, lines, complete = 0.0, [], True
    for code, item in card.items():
        res = kpi_result(item)
        if res is None:
            complete = False
            lines.append(f"  ⬜ {item['name']} — вес {item['weight']}%, факт не внесён")
            continue
        contrib = item["weight"] * res / 100
        total += contrib
        mark = "🟢" if res >= 100 else ("🟡" if res >= 80 else "🔴")
        plan_txt = f"{item['plan']:g}"
        fact_txt = f"{item['fact']:g}"
        lines.append(
            f"  {mark} {item['name']}\n"
            f"       вес {item['weight']}% · план {plan_txt} · факт {fact_txt} · "
            f"выполнение {res:.0f}% → {contrib:.1f}%"
        )
    return total, lines, complete


@router.message(Command("card", "kpi", "карта"))
async def cmd_card(message: Message):
    remember(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        lines = ["📊 <b>Мотивационные карты</b>\n"]
        for emp in PREMIUM_STAFF:
            total, _, complete = premium_percent(emp)
            lines.append(f"<b>{emp}</b> — {total:.1f}%" + ("" if complete else " <i>(не всё внесено)</i>"))
        lines.append("\nПодробно: <code>/card Оля</code> · расчёт: <code>/bonuses</code>")
        await message.answer("\n".join(lines))
        return

    emp, _, variants = take_employee(parts[1])
    if not emp:
        await message.answer(ask_who(variants, parts[1]))
        return
    total, lines, complete = premium_percent(emp)
    period = now_msk().strftime("%m.%Y")
    await message.answer(
        f"📊 <b>Карта: {emp}</b> · {period}\n\n" + "\n".join(lines) +
        f"\n\n<b>Итого премия: {total:.1f}%</b>" + ("" if complete else "\n<i>Не все факты внесены</i>") +
        f"\n\n<code>/fact {emp.split()[0]} проекты 95</code>"
    )


def kpi_find(card: dict, text: str) -> str | None:
    """Находит KPI по коду или по названию, даже если написано несколькими словами."""
    t = _norm(text)
    if not t:
        return None
    if t in card:
        return t
    for code, item in card.items():
        if _norm(code).startswith(t) or t.startswith(_norm(code)):
            return code
    for code, item in card.items():
        name = _norm(item["name"])
        if name.startswith(t) or t.startswith(name) or t in name:
            return code
    first = t.split()[0]
    for code, item in card.items():
        if _norm(code).startswith(first) or _norm(item["name"]).startswith(first):
            return code
    return None


FIELD_RU = {"weight": "вес", "plan": "план", "fact": "факт"}


def _kpi_edit(message: Message, field_name: str) -> str:
    """Разбор команд вида /weight Оля обращения с портала 20%."""
    field_name = FIELD_RU.get(field_name, field_name)
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        return (f"Напишите так: <code>/{field_name} Оля проекты 40</code>\n"
                f"Коды KPI: {', '.join(c for c, *_ in KPI_TEMPLATE)}")

    emp, rest, variants = take_employee(body[1])
    if not emp:
        remember_pending(message, body[1].split()[0] if body[1].split() else "", field_name)
        return ask_who(variants, body[1].split()[0] if body[1].split() else "?")

    words = strip_seps(rest).split()
    if len(words) < 2:
        return (f"Не хватает KPI или значения. Пример: "
                f"<code>/{field_name} {emp.split()[0]} проекты 40</code>")

    card = kpi_card(emp)
    num = parse_number(words[-1], as_percent=(field_name == "вес"))
    if num is None:
        return ("Последним должно быть число: <code>20</code>, <code>20%</code> или <code>0,2</code>."
                if field_name == "вес" else "Последним должно быть число.")

    code = kpi_find(card, " ".join(words[:-1]))
    if not code:
        names = ", ".join(f"{c} ({i['name']})" for c, i in card.items())
        return (f"Нет такого KPI у {emp}.\nЕсть: {names}\n\n"
                f"Добавить новый: <code>/addkpi {emp.split()[0]} качество Качество данных</code>")

    if field_name == "вес":
        card[code]["weight"] = num
        total_w = sum(i["weight"] for i in card.values())
        save_data()
        tail = (f"\n⚠️ Сумма весов у {emp}: {total_w:g}% (обычно должно быть 100%)"
                if abs(total_w - 100) > 0.01 else "")
        return f"Вес «{card[code]['name']}» для {emp}: <b>{num:g}%</b>{tail}"
    if field_name == "план":
        card[code]["plan"] = num
        save_data()
        return f"План «{card[code]['name']}» для {emp}: <b>{num:g}</b>"

    card[code]["fact"] = num
    save_data()
    res = kpi_result(card[code])
    total, _, complete = premium_percent(emp)
    return (f"Факт «{card[code]['name']}» для {emp}: <b>{num:g}</b> "
            f"(выполнение {res:.0f}%)\nТекущая премия: <b>{total:.1f}%</b>"
            + ("" if complete else " <i>— не всё внесено</i>"))


@router.message(Command("addkpi", "новыйkpi"))
async def cmd_addkpi(message: Message):
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Новые KPI заводят руководители.")
        return
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        await message.answer(
            "Как добавить KPI:\n"
            "<code>/addkpi Оля качество Качество данных</code>\n\n"
            "Первое слово после имени — короткий код для команд, дальше название.\n"
            "Потом задайте вес и план: <code>/weight Оля качество 20</code>"
        )
        return
    emp, rest, variants = take_employee(body[1])
    if not emp:
        await message.answer(ask_who(variants, body[1].split()[0]))
        return
    words = strip_seps(rest).split()
    if not words:
        await message.answer("Не хватает кода KPI. Пример: <code>/addkpi Оля качество Качество данных</code>")
        return

    code = _norm(words[0]).replace(" ", "")
    title = " ".join(words[1:]).strip() or words[0].capitalize()
    card = kpi_card(emp)
    if code in card:
        await message.answer(f"KPI «{code}» у {emp} уже есть: {card[code]['name']}")
        return
    card[code] = {"name": title, "weight": 0, "plan": 100, "fact": None, "type": "больше"}
    save_data()
    total_w = sum(i["weight"] for i in card.values())
    await message.answer(
        f"➕ У <b>{emp}</b> новый KPI: <b>{title}</b> (код <code>{code}</code>)\n"
        f"Вес пока 0, план 100.\n\n"
        f"<code>/weight {emp.split()[0]} {code} 20</code>\n"
        f"<code>/plan {emp.split()[0]} {code} 150</code>\n"
        f"Сумма весов сейчас: {total_w:g}%"
    )


@router.message(Command("delkpi", "удалитьkpi"))
async def cmd_delkpi(message: Message):
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Удалять KPI могут руководители.")
        return
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        await message.answer("Напишите так: <code>/delkpi Оля качество</code>")
        return
    emp, rest, variants = take_employee(body[1])
    if not emp:
        await message.answer(ask_who(variants, body[1].split()[0]))
        return
    card = kpi_card(emp)
    code = kpi_find(card, strip_seps(rest))
    if not code:
        await message.answer(f"Нет такого KPI. Есть: {', '.join(card)}")
        return
    name = card.pop(code)["name"]
    save_data()
    await message.answer(f"➖ Убрал «{name}» у {emp}. Сумма весов: "
                         f"{sum(i['weight'] for i in card.values()):g}%")


@router.message(Command("weight", "вес"))
async def cmd_weight(message: Message):
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Веса KPI меняют руководители.")
        return
    await message.answer(_kpi_edit(message, "вес"))


@router.message(Command("plan", "план"))
async def cmd_plan(message: Message):
    remember(message)
    if not is_admin(message.from_user.id):
        await message.answer("Планы KPI ставят руководители.")
        return
    await message.answer(_kpi_edit(message, "план"))


@router.message(Command("fact", "факт"))
async def cmd_fact(message: Message):
    remember(message)
    await message.answer(_kpi_edit(message, "факт"))


@router.message(Command("type", "тип"))
async def cmd_kpi_type(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Настройку KPI меняют руководители.")
        return
    parts = (message.text or "").split()
    if len(parts) < 4 or parts[3].lower() not in ("больше", "меньше"):
        await message.answer(
            "Напишите так: <code>/type Оля ошибки меньше</code>\n\n"
            "Команда задаёт, в какую сторону считать выполнение:\n"
            "• <b>больше</b> — чем больше факт, тем лучше (обращения, проекты). "
            "Выполнение = факт ÷ план.\n"
            "• <b>меньше</b> — чем меньше, тем лучше (ошибки, просрочки). "
            "Факт в пределах плана даёт 100%, превышение снижает результат."
        )
        return
    emp, rest, variants = take_employee(" ".join(parts[1:-1]))
    if not emp:
        await message.answer(ask_who(variants, parts[1]))
        return
    card = kpi_card(emp)
    code = kpi_find(card, strip_seps(rest)) or ""
    if code not in card:
        await message.answer(f"Нет такого KPI. Есть: {', '.join(card)}")
        return
    card[code]["type"] = parts[3].lower()
    save_data()
    await message.answer(f"«{card[code]['name']}» для {emp}: теперь «{parts[3].lower()} — лучше»")


@router.message(Command("bonus", "премия"))
async def cmd_premium_one(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/bonus Оля</code>")
        return
    emp, _, variants = take_employee(parts[1])
    if not emp:
        await message.answer(ask_who(variants, parts[1]))
        return
    total, lines, complete = premium_percent(emp)
    await message.answer(f"💰 <b>{emp}</b> — премия <b>{total:.1f}%</b>\n\n" + "\n".join(lines))


@router.message(Command("bonuses", "премии"))
async def cmd_premiums(message: Message):
    period = now_msk().strftime("%m.%Y")
    rows, missing = [], []
    for emp in PREMIUM_STAFF:
        total, _, complete = premium_percent(emp)
        rows.append((emp, total, complete))
        if not complete:
            missing.append(emp)
    rows.sort(key=lambda r: -r[1])
    lines = [f"💰 <b>Премии за {period}</b>\n"]
    for emp, total, complete in rows:
        mark = "🟢" if total >= 100 else ("🟡" if total >= 80 else "🔴")
        lines.append(f"{mark} <b>{emp}</b> — {total:.1f}%" + ("" if complete else " <i>(неполно)</i>"))
    if missing:
        lines.append(f"\n⚠️ Нет всех фактов: {', '.join(missing)}")
    lines.append("\nПодробно: <code>/card Оля</code> · зафиксировать месяц: <code>/close</code>")
    await message.answer("\n".join(lines))


@router.message(Command("close", "закрыть"))
async def cmd_close_month(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Закрывать месяц может только руководитель.")
        return
    period = (now_msk().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    lines = [f"🔒 <b>Месяц {period} зафиксирован</b>\n"]
    for emp in PREMIUM_STAFF:
        total, _, _ = premium_percent(emp)
        card = kpi_card(emp)
        data["kpi_history"].setdefault(emp, {})[period] = {
            "percent": round(total, 1),
            "items": {c: {"plan": i["plan"], "fact": i["fact"], "weight": i["weight"]}
                      for c, i in card.items()},
        }
        lines.append(f"• {emp} — {total:.1f}%")
        for item in card.values():
            item["fact"] = None
    save_data()
    lines.append("\nФакты обнулены, можно вносить новый месяц.")
    await message.answer("\n".join(lines))


@router.message(Command("history", "история"))
async def cmd_history(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/history Оля</code>")
        return
    emp, _, _ = take_employee(parts[1])
    hist = data["kpi_history"].get(emp, {}) if emp else {}
    if not hist:
        await message.answer(f"По {emp or parts[1]} закрытых месяцев пока нет.")
        return
    lines = [f"📈 <b>{emp}: история премий</b>\n"]
    for period in sorted(hist, reverse=True)[:12]:
        lines.append(f"{period} — <b>{hist[period]['percent']}%</b>")
    await message.answer("\n".join(lines))

# ---------------------------------------------------------------- отпуска


@router.message(Command("vac", "отпуск"))
async def cmd_vacation(message: Message):
    remember(message)
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        await message.answer("Напишите так: <code>/vac Оля 10.11-24.11</code>")
        return
    dates = re.search(r"(\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)\s*[-–—]\s*"
                      r"(\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)", body[1])
    if not dates:
        await message.answer("Не вижу период. Пример: <code>/vac Оля 10.11-24.11</code>")
        return
    emp, _, variants = take_employee(body[1][:dates.start()])
    if not emp:
        await message.answer(ask_who(variants, body[1][:dates.start()].strip()))
        return
    start, end = parse_date(dates.group(1)), parse_date(dates.group(2))
    if not start or not end:
        await message.answer("Не понял даты. Пример: <code>/vac Оля 10.11-24.11</code>")
        return
    if end < start:
        end = end.replace(year=end.year + 1)

    data["vacations"] = [v for v in data["vacations"]
                         if not (v["who"] == emp and v["start"] == start.strftime("%Y-%m-%d"))]
    data["vacations"].append({
        "who": emp,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "warned": False,
        "chat": message.chat.id,
    })
    save_data()
    days = (end.date() - start.date()).days + 1
    await message.answer(
        f"🏖 <b>{emp}</b> в отпуске {fmt_date(start)} — {fmt_date(end)} ({days} дн.)\n"
        f"За {VACATION_WARN_DAYS} дней напомню про передачу дел."
    )


@router.message(Command("vacations", "отпуска"))
async def cmd_vacations(message: Message):
    today = now_msk().date()
    actual = [v for v in data["vacations"]
              if datetime.strptime(v["end"], "%Y-%m-%d").date() >= today]
    if not actual:
        await message.answer("Отпусков в планах нет. <code>/vac Оля 10.11-24.11</code>")
        return
    actual.sort(key=lambda v: v["start"])
    lines = ["🏖 <b>Отпуска</b>\n"]
    for v in actual:
        s = datetime.strptime(v["start"], "%Y-%m-%d").date()
        e = datetime.strptime(v["end"], "%Y-%m-%d").date()
        if s <= today <= e:
            tail = f"<b>в отпуске</b>, выходит {fmt_date(e + timedelta(days=1))}"
        else:
            tail = f"через {(s - today).days} дн."
        lines.append(f"• <b>{v['who']}</b>: {fmt_date(s)} — {fmt_date(e)} · {tail}")
    await message.answer("\n".join(lines))

# ---------------------------------------------------------------- дни рождения


@router.message(Command("bd", "др"))
async def cmd_birthday(message: Message):
    remember(message)
    parts = (message.text or "").split()
    if len(parts) >= 3:
        m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})$", parts[-1])
        emp, _, variants = take_employee(" ".join(parts[1:-1]))
        if not emp:
            await message.answer(ask_who(variants, " ".join(parts[1:-1])))
            return
        if not m:
            await message.answer("Напишите так: <code>/bd Оля 12.03</code>")
            return
        data["birthdays"][emp] = f"{int(m.group(1)):02d}.{int(m.group(2)):02d}"
        save_data()
        await message.answer(f"🎂 {emp} — {data['birthdays'][emp]}, записал.")
        return

    if not data["birthdays"]:
        await message.answer("Дни рождения не заполнены. <code>/bd Оля 12.03</code>")
        return
    today = now_msk().date()
    rows = []
    for emp, ddmm in data["birthdays"].items():
        d, mo = (int(x) for x in ddmm.split("."))
        for year in (today.year, today.year + 1):
            try:
                date = datetime(year, mo, d).date()
            except ValueError:
                continue
            if date >= today:
                rows.append(((date - today).days, emp, ddmm))
                break
    rows.sort()
    lines = ["🎂 <b>Дни рождения</b>\n"]
    for days, emp, ddmm in rows:
        when = "сегодня!" if days == 0 else f"через {days} дн."
        lines.append(f"• <b>{emp}</b> — {ddmm}, {when}")
    await message.answer("\n".join(lines))

# ---------------------------------------------------------------- благодарности


@router.message(Command("thanks", "спасибо"))
async def cmd_thanks(message: Message):
    remember(message)
    body = (message.text or "").split(maxsplit=1)
    if len(body) < 2:
        await message.answer("Напишите так: <code>/thanks Оля за помощь со справкой</code>")
        return
    emp, reason, variants = take_employee(body[1])
    if not emp:
        await message.answer(ask_who(variants, body[1].split()[0]))
        return
    reason = re.sub(r"^за\s+", "", reason, flags=re.I)
    author = uname(message)
    if match_employee(author) == emp:
        await message.answer("Себе спасибо не говорят 🙂")
        return

    data["thanks"].append({
        "to": emp, "from": author, "why": reason,
        "when": now_msk().strftime("%Y-%m-%d"), "chat": message.chat.id,
    })
    save_data()
    count = sum(1 for t in data["thanks"] if t["to"] == emp)
    await message.answer(
        f"🙏 <b>{author}</b> благодарит <b>{emp}</b>" + (f"\nЗа что: {reason}" if reason else "") +
        f"\n\nВсего благодарностей у {emp}: <b>{count}</b>"
    )


def thanks_board(period_days: int | None = None) -> str:
    items = data["thanks"]
    if period_days:
        since = (now_msk() - timedelta(days=period_days)).strftime("%Y-%m-%d")
        items = [t for t in items if t["when"] >= since]
    if not items:
        return "Благодарностей пока нет. <code>/thanks Оля за помощь</code>"
    counts: dict[str, int] = {}
    for t in items:
        counts[t["to"]] = counts.get(t["to"], 0) + 1
    rows = sorted(counts.items(), key=lambda x: -x[1])
    medals = ["🥇", "🥈", "🥉"]
    title = f"🙏 <b>Благодарности за {period_days} дн.</b>" if period_days else "🙏 <b>Благодарности: всего</b>"
    lines = [title, ""]
    for i, (emp, n) in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else '▫️'} <b>{emp}</b> — {n}")
    return "\n".join(lines)


@router.message(Command("rating", "рейтинг", "благодарности"))
async def cmd_thanks_board(message: Message):
    parts = (message.text or "").split()
    period = 30 if len(parts) > 1 and parts[1].lower().startswith("мес") else None
    await message.answer(thanks_board(period))

# ---------------------------------------------------------------- отгулы


@router.message(Command("dayoff", "отгул"))
async def cmd_dayoff(message: Message):
    remember(message)
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/dayoff 20.10 семейные дела</code>")
        return
    when = parse_date(parts[1])
    if not when:
        await message.answer("Не понял дату. Пример: <code>/dayoff 20.10 по семейным</code>")
        return
    reason = parts[2].strip() if len(parts) > 2 else "без указания причины"
    did = next_id()
    data["dayoffs"].append({
        "id": did, "who": uname(message), "user_id": message.from_user.id,
        "date": when.strftime("%Y-%m-%d"), "why": reason,
        "status": "ждёт", "chat": message.chat.id,
    })
    save_data()
    await message.answer(
        f"📝 <b>Заявка на отгул №{did}</b>\n\n"
        f"Кто: <b>{uname(message)}</b>\nКогда: <b>{fmt_date(when)}</b>\nПричина: {reason}\n\n"
        f"<i>Ждём решения руководителя</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Согласовать", callback_data=f"o:yes:{did}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"o:no:{did}"),
        ]]),
    )


@router.callback_query(F.data.startswith("o:"))
async def on_dayoff(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Решение принимает руководитель", show_alert=True)
        return
    _, verdict, did = call.data.split(":")
    req = next((d for d in data["dayoffs"] if d["id"] == int(did)), None)
    if not req or req["status"] != "ждёт":
        await call.answer("Заявка уже обработана")
        return
    req["status"] = "согласован" if verdict == "yes" else "отклонён"
    req["by"] = uname(call)
    save_data()
    mark = "✅" if verdict == "yes" else "❌"
    await call.message.edit_text(
        f"{mark} <b>Отгул №{req['id']} — {req['status']}</b>\n\n"
        f"Кто: <b>{req['who']}</b>\nКогда: <b>{fmt_date(req['date'])}</b>\n"
        f"Причина: {req['why']}\nРешение: {uname(call)}"
    )
    try:
        await call.bot.send_message(
            req["user_id"],
            f"{mark} Ваша заявка на отгул {fmt_date(req['date'])} — <b>{req['status']}</b> ({uname(call)})"
        )
    except Exception:
        pass
    await call.answer("Готово")


@router.message(Command("dayoffs", "отгулы"))
async def cmd_dayoffs(message: Message):
    today = now_msk().strftime("%Y-%m-%d")
    actual = [d for d in data["dayoffs"] if d["date"] >= today]
    if not actual:
        await message.answer("Заявок на отгул нет.")
        return
    lines = ["📝 <b>Отгулы</b>\n"]
    for d in sorted(actual, key=lambda x: x["date"]):
        mark = {"ждёт": "⏳", "согласован": "✅", "отклонён": "❌"}[d["status"]]
        lines.append(f"{mark} <b>№{d['id']}</b> {d['who']} — {fmt_date(d['date'])} · {d['status']}")
    await message.answer("\n".join(lines))

# ---------------------------------------------------------------- игра «5 букв»

WORD_RE = re.compile(r"^[а-яё]{5}$", re.IGNORECASE)
ALPHABET = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
RANK = {"⬛": 1, "🟨": 2, "🟩": 3}
MAX_ATTEMPTS = 8


def load_words(name: str) -> list[str]:
    path = BASE_DIR / name
    if not path.exists():
        return []
    return [w.strip().lower().replace("ё", "е") for w in path.read_text(encoding="utf-8").split()
            if len(w.strip()) == 5]


SECRET_WORDS = load_words("words_secret.txt")
ALLOWED_WORDS = set(load_words("words_all.txt")) | set(SECRET_WORDS)
if not SECRET_WORDS and ALLOWED_WORDS:      # нет отобранного списка — загадываем из общего
    SECRET_WORDS = sorted(ALLOWED_WORDS)
if not SECRET_WORDS:
    log.warning("Словари не найдены: положите words_secret.txt и words_all.txt рядом с bot.py")


@dataclass
class Game:
    secret: str
    guesses: list = field(default_factory=list)
    last_player: int | None = None
    finished: bool = False


games: dict[int, Game] = {}


def compare(guess: str, secret: str) -> str:
    result = ["⬛"] * 5
    rest: dict[str, int] = {}
    for i, ch in enumerate(guess):
        if ch == secret[i]:
            result[i] = "🟩"
        else:
            rest[secret[i]] = rest.get(secret[i], 0) + 1
    for i, ch in enumerate(guess):
        if result[i] == "🟩":
            continue
        if rest.get(ch, 0) > 0:
            result[i] = "🟨"
            rest[ch] -= 1
    return "".join(result)


def render_board(game: Game) -> str:
    lines = [f"{p}  <b>{w.upper()}</b>  <i>{n}</i>" for w, p, n in game.guesses]
    lines.append(f"\nОсталось попыток: <b>{MAX_ATTEMPTS - len(game.guesses)}</b>")
    return "\n".join(lines)


def render_letters(game: Game) -> str:
    status: dict[str, str] = {}
    for word, pattern, _ in game.guesses:
        for ch, mark in zip(word, pattern):
            if RANK[mark] > RANK.get(status.get(ch, ""), 0):
                status[ch] = mark
    return "\n".join(" ".join(f"{status.get(c, '⬜')}{c.upper()}" for c in ALPHABET[i:i + 8])
                     for i in range(0, len(ALPHABET), 8))


@router.message(Command("game", "игра"))
async def cmd_game(message: Message):
    remember(message)
    if not SECRET_WORDS:
        missing = [n for n in ("words_secret.txt", "words_all.txt") if not (BASE_DIR / n).exists()]
        await message.answer(
            "Словарь не загружен.\n\n"
            + (f"Нет файлов: <code>{', '.join(missing)}</code>\n" if missing else "")
            + "Их нужно положить в репозиторий рядом с <code>bot.py</code>."
        )
        return
    game = games.get(message.chat.id)
    if game and not game.finished:
        await message.answer("Игра уже идёт 🙂\n\n" + render_board(game))
        return
    games[message.chat.id] = Game(secret=random.choice(SECRET_WORDS))
    await message.answer(
        f"🎯 Слово загадано — пять букв, существительное.\n"
        f"Попыток: <b>{MAX_ATTEMPTS}</b>. Пишите варианты 👇"
    )


@router.message(Command("score", "топ", "счет"))
async def cmd_scores(message: Message):
    chat = data["scores"].get(str(message.chat.id), {})
    if not chat:
        await message.answer("Счёт пуст. /game — начать.")
        return
    rows = sorted(chat.values(), key=lambda r: -r["points"])
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Пять букв</b>\n"]
    for i, r in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else '▫️'} {r['name']} — <b>{r['points']}</b>")
    await message.answer("\n".join(lines))

# ---------------------------------------------------------------- ИИ-помощник

AI_SYSTEM = (
    "Ты помощник отдела компенсаций и льгот (C&B) российской розничной компании, "
    "встроенный в телеграм-бот. Отвечай кратко, по делу, на русском.\n\n"
    "Ты умеешь не только отвечать, но и ВЫПОЛНЯТЬ действия в боте. Если просьба "
    "сводится к действию — верни только команды, каждую с новой строки, без пояснений, "
    "без кавычек и без markdown. Бот выполнит их сам от имени написавшего.\n\n"
    "Команды бота:\n"
    "/task Имя текст поручения до ДД.ММ — поставить поручение\n"
    "  нескольким сразу: /task Оля, Лёша, Света текст до ДД.ММ (имена через запятую)\n"
    "  всем семерым — перечисли всех поимённо\n"
    "  срок обязателен: «до 25.10», «до 19 окт», «до завтра», «до сегодня»\n"
    "/done N — закрыть поручение · /extend N до ДД.ММ — продлить · /reopen N — вернуть в работу\n"
    "/tasks [Имя] — открытые поручения · /closed [Имя] — закрытые\n"
    "/addkpi Имя код Название — новый KPI (код — ОДНО слово латиницей или кириллицей)\n"
    "/weight Имя код 30% · /plan Имя код 200 · /fact Имя код 180 · /type Имя код меньше\n"
    "/delkpi Имя код · /card [Имя] · /bonuses · /close — зафиксировать месяц\n"
    "/vac Имя ДД.ММ-ДД.ММ — отпуск · /dayoff ДД.ММ причина — отгул\n"
    "/thanks Имя за что — благодарность · /rating · /bd Имя ДД.ММ\n\n"
    "Сотрудники: Сергей, Максим, Вика Г., Вика К., Света, Лёша, Оля.\n"
    "Важно: в /addkpi код — одно слово (например «обращения»), а название — остальное. "
    "Дальше в /weight, /plan, /fact используй именно этот код.\n\n"
    "Если вопрос требует объяснения, а не действия (нормы ТК, расчёты, формулировки) — "
    "просто ответь текстом, без команд. Если нужны точные нормы закона, называй статью "
    "и предупреждай, что решение стоит сверить с юристом."
)

ai_history: dict[int, list] = {}


_giga_token = {"value": "", "until": 0.0}


async def gigachat_token() -> str:
    """Токен GigaChat живёт 30 минут — держим его в памяти и обновляем по истечении."""
    import time
    if _giga_token["value"] and time.time() < _giga_token["until"] - 60:
        return _giga_token["value"]

    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {AI_KEY}",
    }
    conn = aiohttp.TCPConnector(ssl=False) if AI_INSECURE else None
    async with aiohttp.ClientSession(connector=conn) as s:
        async with s.post(url, headers=headers, data={"scope": AI_SCOPE},
                          timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.json()
    if "access_token" not in body:
        raise RuntimeError(f"GigaChat не выдал токен: {str(body)[:200]}")
    _giga_token["value"] = body["access_token"]
    _giga_token["until"] = body.get("expires_at", 0) / 1000 or (time.time() + 1500)
    return _giga_token["value"]


async def ask_ai(question: str, chat_id: int) -> str:
    history = ai_history.setdefault(chat_id, [])
    history.append({"role": "user", "content": question})
    del history[:-8]

    try:
        if AI_PROVIDER == "gigachat":
            token = await gigachat_token()
            url = (AI_BASE_URL or "https://api.giga.chat/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            payload = {"model": AI_MODEL or "GigaChat-2", "max_tokens": 800,
                       "messages": [{"role": "system", "content": AI_SYSTEM}] + history}
        elif AI_PROVIDER == "anthropic":
            url = (AI_BASE_URL or "https://api.anthropic.com") + "/v1/messages"
            headers = {"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
            payload = {"model": AI_MODEL or "claude-haiku-5-5", "max_tokens": 800,
                       "system": AI_SYSTEM, "messages": history}
        else:
            url = (AI_BASE_URL or "https://api.openai.com/v1") + "/chat/completions"
            headers = {"Authorization": f"{AI_AUTH_SCHEME} {AI_KEY}", "Content-Type": "application/json"}
            payload = {"model": AI_MODEL or "gpt-4o-mini", "max_tokens": 800,
                       "messages": [{"role": "system", "content": AI_SYSTEM}] + history}

        conn = aiohttp.TCPConnector(ssl=False) if AI_INSECURE else None
        async with aiohttp.ClientSession(connector=conn) as s:
            async with s.post(url, headers=headers, json=payload,
                              timeout=aiohttp.ClientTimeout(total=90)) as r:
                body = await r.json()
        if r.status >= 400:
            log.warning("ИИ вернул ошибку %s: %s", r.status, str(body)[:300])
            return (f"Сервис ответил ошибкой <b>{r.status}</b>.\n\n"
                    f"<code>{str(body)[:300]}</code>")

        if AI_PROVIDER == "anthropic":
            answer = "".join(b.get("text", "") for b in body.get("content", []))
        else:
            answer = body["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": answer})
        return answer.strip() or "Пустой ответ."
    except Exception as e:
        log.warning("ИИ недоступен: %s: %s", type(e).__name__, e)
        history.pop()
        detail = f"{type(e).__name__}: {e}"[:300]
        hint = ""
        low = detail.lower()
        if "dns" in low or "name or service" in low or "timeout" in low:
            hint = ("\n\nАдрес недоступен с хостинга: не резолвится или закрыт порт. "
                    "Попробуйте другого провайдера ИИ.")
        elif "certificate" in low or "certificate_verify" in low or "cert" in low:
            hint = ("\n\nПохоже на сертификат. Добавьте переменную "
                    "<code>AI_INSECURE_SSL=1</code> и перезапустите бота.")
        elif "токен" in low or "401" in low or "unauthorized" in low:
            hint = ("\n\nПохоже на ключ или scope. Проверьте <code>AI_API_KEY</code> "
                    "(нужен «ключ авторизации», а не client_id) и <code>AI_SCOPE</code>.")
        elif "connect" in low:
            hint = "\n\nСервис не отвечает — возможно, хостинг не пускает исходящие на этот адрес."
        return f"Не смог связаться с ИИ-сервисом.\n\n<code>{detail}</code>{hint}"


@router.message(Command("ask", "спроси", "ai"))
async def cmd_ai(message: Message):
    remember(message)
    if not AI_KEY:
        await message.answer(
            "ИИ-помощник не подключён.\nЗадайте переменные окружения "
            "<code>AI_API_KEY</code>, при необходимости <code>AI_PROVIDER</code> и <code>AI_MODEL</code>."
        )
        return
    if AI_ADMINS_ONLY and not is_admin(message.from_user.id):
        await message.answer("ИИ-помощник доступен руководителям.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напишите так: <code>/ask как считается средний заработок для отпускных</code>")
        return
    thinking = await message.answer("🤔 Думаю…")
    answer = await ask_ai(parts[1].strip(), message.chat.id)

    cmds = extract_commands(answer)
    if cmds:
        preview = "\n".join(f"<code>{c}</code>" for c in cmds[:12])
        try:
            await thinking.edit_text(f"🤖 Выполняю от вашего имени:\n{preview}")
        except Exception:
            await message.answer(f"🤖 Выполняю от вашего имени:\n{preview}")
        await run_commands(message, cmds)
        return

    try:
        await thinking.edit_text(answer[:4000])
    except Exception:
        await message.answer(answer[:4000])


@router.message(Command("reset", "забудь"))
async def cmd_ai_reset(message: Message):
    ai_history.pop(message.chat.id, None)
    await message.answer("Контекст разговора с помощником очищен.")

# ---------------------------------------------------------------- справка

HELP = (
    "🤖 <b>Бот отдела C&amp;B</b>\n\n"
    "<b>Поручения</b>\n"
    "/task — поставить: <code>/task Оля свести оклады до 20.10</code>\n"
    "    нескольким сразу: <code>/task Максим, Вика Г., Света отчёт до 19 окт</code>\n"
    "/tasks — открытые · <code>/tasks Света</code> — по человеку\n"
    "/my — свои · /closed — закрытые\n"
    "/done 3 — закрыть · /extend 3 до 25.10 · /cancel 3\n"
    "/reopen 3 — вернуть закрытое в работу (руководитель)\n"
    "<i>Закрыть и продлить может исполнитель или руководитель.</i>\n\n"
    "<b>Премии и KPI</b>\n"
    "/card — сводка · <code>/card Оля</code> — подробно\n"
    "/fact — внести факт: <code>/fact Оля проекты 95</code>\n"
    "/weight — вес: <code>/weight Оля проекты 20%</code>\n"
    "/plan — план: <code>/plan Оля обращения 150</code>\n"
    "/type — куда считать: <code>/type Оля ошибки меньше</code>\n"
    "/addkpi — новый KPI: <code>/addkpi Оля качество Качество данных</code>\n"
    "/delkpi — убрать KPI · /bonus Оля · /bonuses — по всем\n"
    "/close — зафиксировать месяц · /history Оля\n\n"
    "<b>Отсутствия</b>\n"
    "/vac — отпуск: <code>/vac Оля 10.11-24.11</code> · /vacations\n"
    "/dayoff — отгул: <code>/dayoff 20.10 семейные дела</code> · /dayoffs\n\n"
    "<b>Команда</b>\n"
    "/thanks — спасибо: <code>/thanks Оля за помощь</code> · /rating\n"
    "/bd — день рождения: <code>/bd Оля 12.03</code> · /bd — список\n\n"
    "<b>Разное</b>\n"
    "/game — «5 букв» · /score — счёт\n"
    "/ask — спросить помощника, он сам выполнит нужные команды\n"
    "/reset — очистить контекст помощника\n"
    "<i>Несколько команд можно слать одним сообщением, каждую с новой строки.</i>\n"
    "/whoami — мои права · /chatid — id чата\n"
    "/iam Вика Г. — представиться · /team — кого бот узнаёт\n"
    "/export — резервная копия данных · /import — восстановить\n\n"
    "<i>Имя можно писать как угодно: «Оля», «вика к», «кудряшова». "
    "Если неоднозначно — просто ответьте именем, команда повторится сама.</i>"
)


@router.message(CommandStart())
@router.message(Command("help", "помощь"))
async def cmd_help(message: Message):
    remember(message)
    save_data()
    await message.answer(HELP)

# ---------------------------------------------------------------- попытки в игре


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message):
    remember(message)
    text_raw = (message.text or "").strip()

    # 1. ждём уточнения имени после «Уточните, кто именно…»
    key = (message.chat.id, message.from_user.id)
    pend = PENDING.get(key)
    if pend:
        if time.time() - pend["ts"] > PENDING_TTL:
            PENDING.pop(key, None)
        else:
            emp = match_employee(text_raw)
            if emp:
                PENDING.pop(key, None)
                new_text = re.sub(re.escape(pend["raw"]), emp, pend["text"], count=1)
                cmd = new_text.split()[0].lstrip("/").split("@")[0].lower()
                handler = REDISPATCH.get(cmd)
                if handler:
                    await handler(message.model_copy(update={"text": new_text}))
                    return

    game = games.get(message.chat.id)
    text = (message.text or "").strip()
    if not game or game.finished or not WORD_RE.fullmatch(text):
        save_data()
        return

    word = text.lower().replace("ё", "е")
    name = uname(message)
    if any(g[0] == word for g in game.guesses):
        await message.reply("Это слово уже называли 🙂")
        return
    if word not in ALLOWED_WORDS:
        await message.reply("Такого слова нет в словаре.")
        return
    if game.last_player == message.from_user.id and game.guesses:
        await message.reply("Пусть кто-нибудь другой попробует 🙂")
        return

    pattern = compare(word, game.secret)
    game.guesses.append((word, pattern, name))
    game.last_player = message.from_user.id

    if word == game.secret:
        game.finished = True
        points = max(1, MAX_ATTEMPTS - len(game.guesses) + 1)
        chat = data["scores"].setdefault(str(message.chat.id), {})
        rec = chat.setdefault(str(message.from_user.id), {"name": name, "points": 0})
        rec["name"] = name
        rec["points"] += points
        save_data()
        await message.answer(
            f"{pattern}  <b>{word.upper()}</b>\n\n🎉 <b>{name}</b> угадал(а) "
            f"с {len(game.guesses)}-й попытки! +{points}, всего: {rec['points']}\n\n/game — ещё"
        )
        return
    if len(game.guesses) >= MAX_ATTEMPTS:
        game.finished = True
        await message.answer(render_board(game) +
                             f"\n\n😔 Попытки кончились. Слово: <b>{game.secret.upper()}</b>")
        return
    await message.answer(render_board(game) + "\n\n" + render_letters(game))

REDISPATCH = {
    "task": cmd_task, "поручение": cmd_task, "задача": cmd_task,
    "weight": cmd_weight, "вес": cmd_weight,
    "plan": cmd_plan, "план": cmd_plan,
    "fact": cmd_fact, "факт": cmd_fact,
}

# Все команды, которые можно выполнить пачкой или от имени ИИ
COMMANDS = {
    "task": cmd_task, "поручение": cmd_task, "задача": cmd_task,
    "tasks": cmd_tasks, "задачи": cmd_tasks, "поручения": cmd_tasks,
    "my": cmd_my_tasks, "мои": cmd_my_tasks,
    "closed": cmd_closed, "закрытые": cmd_closed,
    "done": cmd_done, "готово": cmd_done,
    "extend": cmd_postpone, "продлить": cmd_postpone,
    "reopen": cmd_reopen, "вернуть": cmd_reopen,
    "cancel": cmd_cancel_task, "снять": cmd_cancel_task,
    "card": cmd_card, "kpi": cmd_card, "карта": cmd_card,
    "weight": cmd_weight, "вес": cmd_weight,
    "plan": cmd_plan, "план": cmd_plan,
    "fact": cmd_fact, "факт": cmd_fact,
    "type": cmd_kpi_type, "тип": cmd_kpi_type,
    "addkpi": cmd_addkpi, "новыйkpi": cmd_addkpi,
    "delkpi": cmd_delkpi, "удалитьkpi": cmd_delkpi,
    "bonus": cmd_premium_one, "премия": cmd_premium_one,
    "bonuses": cmd_premiums, "премии": cmd_premiums,
    "close": cmd_close_month, "закрыть": cmd_close_month,
    "history": cmd_history, "история": cmd_history,
    "vac": cmd_vacation, "отпуск": cmd_vacation,
    "vacations": cmd_vacations, "отпуска": cmd_vacations,
    "dayoff": cmd_dayoff, "отгул": cmd_dayoff,
    "dayoffs": cmd_dayoffs, "отгулы": cmd_dayoffs,
    "thanks": cmd_thanks, "спасибо": cmd_thanks,
    "rating": cmd_thanks_board, "рейтинг": cmd_thanks_board,
    "bd": cmd_birthday, "др": cmd_birthday,
    "iam": cmd_iam, "bind": cmd_bind, "team": cmd_team,
    "export": cmd_export, "import": cmd_import,
    "game": cmd_game, "игра": cmd_game, "score": cmd_scores,
    "version": cmd_version, "whoami": cmd_whoami, "help": cmd_help,
}

# ---------------------------------------------------------------- память в телеграме


def data_bytes() -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


async def backup_to_telegram(bot: Bot, reason: str = "") -> bool:
    """Отправляет копию данных в хранилище и закрепляет её там."""
    if not STORAGE_CHAT:
        return False
    try:
        stamp = now_msk().strftime("%d.%m.%Y %H:%M")
        sent = await bot.send_document(
            STORAGE_CHAT,
            BufferedInputFile(data_bytes(), filename="cb_data.json"),
            caption=(f"💾 Память бота · {stamp}\n"
                     f"поручений {len(data.get('tasks', []))}, "
                     f"карт KPI {len(data.get('kpi', {}))}, "
                     f"привязок {len(data.get('bind', {}))}"
                     + (f"\n{reason}" if reason else "")),
            disable_notification=True,
        )
        try:
            await bot.unpin_all_chat_messages(STORAGE_CHAT)
        except Exception:
            pass
        await bot.pin_chat_message(STORAGE_CHAT, sent.message_id, disable_notification=True)
        DIRTY["flag"] = False
        log.info("Копия данных сохранена в телеграм")
        return True
    except Exception as e:
        log.warning("Копию в телеграм не отправить: %s", e)
        return False


async def restore_from_telegram(bot: Bot) -> bool:
    """Поднимает данные из закреплённой копии — после пересборки хостинга."""
    global data
    if not STORAGE_CHAT:
        return False
    try:
        chat = await bot.get_chat(STORAGE_CHAT)
        pinned = getattr(chat, "pinned_message", None)
        if not pinned or not pinned.document:
            log.info("В хранилище нет закреплённой копии")
            return False
        buf = await bot.download(pinned.document)
        loaded = json.loads(buf.read().decode("utf-8"))
        if not isinstance(loaded, dict) or "tasks" not in loaded:
            return False
        data = {**DEFAULT_DATA, **loaded}
        DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        log.info("Данные восстановлены из телеграма: поручений %s, карт KPI %s",
                 len(data.get("tasks", [])), len(data.get("kpi", {})))
        return True
    except Exception as e:
        log.warning("Не восстановить из телеграма: %s", e)
        return False


async def backup_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(BACKUP_EVERY_SEC)
        if DIRTY["flag"]:
            await backup_to_telegram(bot)


# ---------------------------------------------------------------- расписание


def target_chats() -> list[int]:
    return list(ALLOWED_CHATS) or list(data["chats"])


async def say(bot: Bot, text: str, chat_id: int | None = None) -> None:
    for cid in ([chat_id] if chat_id else target_chats()):
        try:
            await bot.send_message(cid, text)
        except Exception as e:
            log.warning("Не отправить в %s: %s", cid, e)


async def daily_checks(bot: Bot) -> None:
    now = now_msk()
    today = now.date()
    today_str = today.strftime("%Y-%m-%d")

    # 1. поручения: скорый срок, сегодня, просрочка
    soon, due, overdue = [], [], []
    for t in data["tasks"]:
        if t["done"]:
            continue
        d = datetime.strptime(t["deadline"], "%Y-%m-%d").date()
        left = (d - today).days
        if left < 0:
            if t.get("overdue_at") != today_str:
                t["overdue_at"] = today_str
                overdue.append(t)
        elif left == 0:
            due.append(t)
        elif left <= TASK_WARN_DAYS and not t.get("warned"):
            t["warned"] = True
            soon.append(t)
    if soon or due or overdue:
        save_data()
        blocks = []
        if due:
            blocks.append("🟠 <b>Срок сегодня</b>\n" + "\n".join(task_line(t) for t in due))
        if soon:
            blocks.append("🟡 <b>Скоро срок</b>\n" + "\n".join(task_line(t) for t in soon))
        if overdue:
            blocks.append("🔴 <b>Просрочено</b>\n" + "\n".join(task_line(t) for t in overdue))
        await say(bot, "📋 <b>Поручения</b>\n\n" + "\n\n".join(blocks) +
                       "\n\nЗакрыть: <code>/done номер</code>")

    # 2. отпуска: за две недели — передача дел
    for v in data["vacations"]:
        start = datetime.strptime(v["start"], "%Y-%m-%d").date()
        if not v.get("warned") and 0 < (start - today).days <= VACATION_WARN_DAYS:
            v["warned"] = True
            save_data()
            open_tasks = [t for t in data["tasks"] if not t["done"] and v["who"] in t["who"]]
            tail = ("\n\nОткрытые поручения:\n" + "\n".join(task_line(t) for t in open_tasks)) if open_tasks else ""
            await say(bot,
                      f"🏖 <b>Через {(start - today).days} дн. {v['who']} уходит в отпуск</b>\n"
                      f"({fmt_date(v['start'])} — {fmt_date(v['end'])})\n\n"
                      f"Пора решить, кто подхватывает дела.{tail}")

    # 3. дни рождения
    for emp, ddmm in data["birthdays"].items():
        d, mo = (int(x) for x in ddmm.split("."))
        try:
            date = datetime(today.year, mo, d).date()
        except ValueError:
            continue
        left = (date - today).days
        key = f"bd:{emp}:{today.year}"
        if left == 0 and data["done"].get(key) != "today":
            data["done"][key] = "today"
            save_data()
            await say(bot, f"🎂 Сегодня день рождения у <b>{emp}</b>! Поздравляем 🎉")
        elif left == BIRTHDAY_WARN_DAYS and data["done"].get(key) != "warned":
            data["done"][key] = "warned"
            save_data()
            await say(bot, f"🎂 Через {BIRTHDAY_WARN_DAYS} дня день рождения у <b>{emp}</b> ({ddmm}).")

    # 4. табель
    last_day = monthrange(today.year, today.month)[1]
    if (today.day in TIMESHEET_DAYS or today.day == last_day) and data["done"].get("ts") != today_str:
        data["done"]["ts"] = today_str
        save_data()
        half = "первую половину месяца" if today.day in TIMESHEET_DAYS else "месяц"
        await say(bot, f"🗓 <b>Напоминание про табель</b>\nПора закрыть {half}.")

    # 5. KPI первого числа
    if today.day == KPI_REMIND_DAY and data["done"].get("kpi") != today_str:
        data["done"]["kpi"] = today_str
        save_data()
        period = (now.replace(day=1) - timedelta(days=1)).strftime("%m.%Y")
        missing = [emp for emp in PREMIUM_STAFF if not premium_percent(emp)[2]]
        await say(bot,
                  f"📊 <b>Пора закрыть KPI за {period}</b>\n\n"
                  f"Внести факт: <code>/fact Оля проекты 95</code>\n"
                  f"Посмотреть: /bonuses\n\n" +
                  (f"Ждут заполнения: <b>{', '.join(missing)}</b>" if missing else "Все факты уже внесены 👍"))


async def weekly_thanks(bot: Bot) -> None:
    today_str = now_msk().strftime("%Y-%m-%d")
    if data["done"].get("thanks") == today_str:
        return
    data["done"]["thanks"] = today_str
    save_data()
    week = [t for t in data["thanks"]
            if t["when"] >= (now_msk() - timedelta(days=7)).strftime("%Y-%m-%d")]
    if not week:
        return
    counts: dict[str, int] = {}
    for t in week:
        counts[t["to"]] = counts.get(t["to"], 0) + 1
    rows = sorted(counts.items(), key=lambda x: -x[1])
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🙏 <b>Спасибо недели</b>\n"]
    for i, (emp, n) in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else '▫️'} <b>{emp}</b> — {n}")
    reasons = [f"• {t['from']} → {t['to']}: {t['why']}" for t in week if t["why"]][-5:]
    if reasons:
        lines.append("\n<i>За что благодарили:</i>\n" + "\n".join(reasons))
    await say(bot, "\n".join(lines))


async def scheduler(bot: Bot) -> None:
    while True:
        try:
            now = now_msk()
            if now.hour == MORNING_HOUR and data["done"].get("daily") != now.strftime("%Y-%m-%d"):
                data["done"]["daily"] = now.strftime("%Y-%m-%d")
                save_data()
                await daily_checks(bot)
            if now.weekday() == THANKS_SUMMARY_WEEKDAY and now.hour == THANKS_SUMMARY_HOUR:
                await weekly_thanks(bot)
        except Exception as e:
            log.warning("Ошибка расписания: %s", e)
        await asyncio.sleep(300)

# ---------------------------------------------------------------- запуск


def get_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        f = BASE_DIR / "token.txt"
        if f.exists():
            token = f.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("Не найден токен: задайте BOT_TOKEN или положите token.txt.")
    return token


async def on_error(event: ErrorEvent) -> None:
    if isinstance(event.exception, TelegramNetworkError):
        log.error("Нет связи с Telegram.")
    else:
        log.exception("Ошибка: %s", event.exception)


async def main() -> None:
    load_data()
    proxy = os.getenv("TG_PROXY", "").strip() or None
    session = AiohttpSession(proxy=proxy) if proxy else None
    bot = Bot(token=get_token(), session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    router.message.outer_middleware(access_guard)
    router.callback_query.outer_middleware(access_guard)
    dp.include_router(router)
    dp.errors.register(on_error)

    # Своя память на диске — главная. Если её нет (хостинг пересобрался),
    # берём свежую копию из телеграма, и только если и там пусто — подложку из репозитория.
    if DATA_SOURCE["from"] != "disk":
        restored = await restore_from_telegram(bot)
        if not restored and DATA_SOURCE["from"] == "seed":
            save_data()
            log.info("Копии в телеграме нет — работаем с подложкой из репозитория")
    asyncio.create_task(backup_loop(bot))
    log.info("Хранилище копий: %s", STORAGE_CHAT or "не задано (ADMIN_IDS пуст)")
    log.info("ВЕРСИЯ %s", VERSION)
    log.info("Сотрудников: %s · с премией: %s · слов: %s",
             len(EMPLOYEES), len(PREMIUM_STAFF), len(SECRET_WORDS))
    log.info("Разрешённые чаты: %s · руководители: %s",
             ALLOWED_CHATS or "(пока все — задайте ALLOWED_CHATS)",
             ADMIN_IDS or "(пока все — задайте ADMIN_IDS)")
    log.info("ИИ-помощник: %s", f"{AI_PROVIDER}, модель {AI_MODEL or 'по умолчанию'}" if AI_KEY else "выключен")
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
