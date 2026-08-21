import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import html
import datetime
import aiohttp
import requests
from aiohttp import web
from docx import Document
from google import genai
from google.genai import types as gtypes

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # Fallback

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot_v15")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("🔴 BOT_TOKEN не задан!")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_MODEL_CANDIDATES = list(dict.fromkeys([
    os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
]))
GROQ_MODEL = "llama-3.1-8b-instant"

_working_model = {"name": None}
HTTP = None
TASKS = set()
temp_vacancies = {}
user_states = {}          
user_adapt_target = {}    
user_search_cache = {}    
interview_sessions = {}   # Хранилище сессий тренажера собеседований

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, 
    username TEXT,
    balance INTEGER DEFAULT 7,
    unlimited_until TIMESTAMP,
    daily_count INTEGER DEFAULT 0,
    last_active_date TEXT,
    referred_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    text TEXT,
    active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hidden_vacancies (
    user_id INTEGER,
    vacancy_id TEXT,
    PRIMARY KEY (user_id, vacancy_id)
);
CREATE TABLE IF NOT EXISTS liked_vacancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    vacancy_id TEXT,
    title TEXT,
    status TEXT DEFAULT 'Откликнулся'
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS social_shares (
    user_id INTEGER,
    network TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, network)
);
""")
conn.commit()


def register_user(user_id: int, username: str, referrer_id: int = None) -> bool:
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if row:
        return False
    if referrer_id == user_id:
        referrer_id = None
    if referrer_id:
        cur.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,))
        if not cur.fetchone():
            referrer_id = None
    initial_balance = 7
    cur.execute(
        "INSERT INTO users (user_id, username, balance, referred_by) VALUES (?, ?, ?, ?)",
        (user_id, username, initial_balance, referrer_id)
    )
    conn.commit()
    if referrer_id:
        cur.execute("UPDATE users SET balance = balance + 7 WHERE user_id=?", (referrer_id,))
        conn.commit()
    return True


def get_user_data(user_id: int):
    cur.execute("SELECT balance, unlimited_until, daily_count, last_active_date FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return {"balance": 7, "unlimited_until": None, "daily_count": 0, "last_active_date": ""}
    return {"balance": row[0], "unlimited_until": row[1], "daily_count": row[2], "last_active_date": row[3]}


def spend_balance(user_id: int, cost: int = 1) -> bool:
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        return True
        
    data = get_user_data(user_id)
    unlimited_until = data["unlimited_until"]
    if unlimited_until:
        cur.execute("SELECT datetime('now') < datetime(?)", (unlimited_until,))
        is_active_sub = cur.fetchone()[0]
        if is_active_sub:
            last_date = data["last_active_date"]
            daily_count = data["daily_count"]
            today_str = datetime.date.today().isoformat()
            
            if last_date != today_str:
                cur.execute("UPDATE users SET daily_count=1, last_active_date=? WHERE user_id=?", (today_str, user_id))
                conn.commit()
                return True
            elif daily_count < 50:
                cur.execute("UPDATE users SET daily_count = daily_count + 1 WHERE user_id=?", (user_id,))
                conn.commit()
                return True
            else:
                return False

    balance = data["balance"]
    if balance < cost:
        return False
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (cost, user_id))
    conn.commit()
    return True


def admin_add_balance(user_id: int, amount: int) -> int:
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    return get_user_data(user_id)["balance"]


def admin_set_unlimited(user_id: int, days: int = 10):
    cur.execute("UPDATE users SET unlimited_until = datetime('now', '+' || ? || ' days') WHERE user_id=?", (days, user_id))
    conn.commit()


def add_resume(user_id: int, name: str, text: str):
    cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (user_id,))
    cur.execute("INSERT INTO resumes (user_id, name, text, active) VALUES (?,?,?,1)",
                (user_id, name, text[:20000]))
    conn.commit()


def list_resumes(user_id: int):
    cur.execute("SELECT id, name, active FROM resumes WHERE user_id=? ORDER BY id", (user_id,))
    return [{"id": r[0], "name": r[1], "active": r[2]} for r in cur.fetchall()]


def get_active_resume(user_id: int) -> str:
    cur.execute("SELECT text FROM resumes WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT text FROM resumes WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
        row = cur.fetchone()
    return row[0] if row else ""


def get_resume_by_id(user_id: int, resume_id: int) -> str:
    cur.execute("SELECT text FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id))
    row = cur.fetchone()
    return row[0] if row else ""


def hide_vacancy(user_id: int, vacancy_id: str):
    cur.execute("INSERT OR IGNORE INTO hidden_vacancies (user_id, vacancy_id) VALUES (?, ?)", (user_id, vacancy_id))
    conn.commit()


def is_vacancy_hidden(user_id: int, vacancy_id: str) -> bool:
    cur.execute("SELECT 1 FROM hidden_vacancies WHERE user_id=? AND vacancy_id=?", (user_id, vacancy_id))
    return cur.fetchone() is not None


def like_vacancy(user_id: int, vacancy_id: str, title: str):
    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Откликнулся')", (user_id, vacancy_id, title))
    conn.commit()


def get_user_preferences(user_id: int) -> str:
    cur.execute("SELECT title FROM liked_vacancies WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,))
    rows = cur.fetchall()
    if not rows:
        return "Нет истории предпочтений."
    return ", ".join([r[0] for r in rows])


# ---------------- ИИ-слой (Каскадная защита) ----------------
def _openai_compat(prompt: str, base: str, key: str, model: str) -> str:
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "temperature": 0.7,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def ai_generate(prompt: str):
    if client:
        cands = [_working_model["name"]] if _working_model["name"] else GEMINI_MODEL_CANDIDATES
        for m in cands:
            try:
                resp = client.models.generate_content(
                    model=m, contents=prompt,
                    config=gtypes.GenerateContentConfig(temperature=0.7))
                if resp is not None and resp.text:
                    _working_model["name"] = m
                    log.info("AI ok: gemini/%s", m)
                    return resp.text
            except Exception as e:
                log.warning("Gemini %s failed: %s", m, str(e)[:100])
                
    if GROQ_KEY:
        try:
            log.info("AI ok: groq/%s", GROQ_MODEL)
            return _openai_compat(prompt, "https://api.groq.com/openai/v1", GROQ_KEY, GROQ_MODEL)
        except Exception as e:
            log.warning("Groq failed: %s", str(e)[:100])

    if OPENROUTER_KEY:
        try:
            log.info("AI ok: openrouter/qwen")
            return _openai_compat(prompt, "https://openrouter.ai/api/v1", OPENROUTER_KEY, "qwen/qwen-2.5-7b-instruct:free")
        except Exception as e:
            log.warning("OpenRouter failed: %s", str(e)[:100])
            
    return None


# ---------------- Извлечение текста из файлов ----------------
def rtf_to_text(raw: str) -> str:
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: bytes.fromhex(m.group(1)).decode("cp1251", errors="ignore"), raw)
    text = re.sub(r"\\[a-z]+-?\d* ?", " ", text)
    text = re.sub(r"[{}]", "", text)
    return html.unescape(text).strip()


def extract_text(path: str, file_name: str) -> str:
    fn = file_name.lower()
    text_content = ""
    try:
        if fn.endswith(".pdf"):
            doc = fitz.open(path)
            pages_text = []
            for page in doc:
                pages_text.append(page.get_text("text"))
            text_content = "\n".join(pages_text)
        elif fn.endswith(".docx"):
            doc = Document(path)
            text_content = "\n".join(p.text for p in doc.paragraphs if p.text)
        elif fn.endswith(".rtf"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text_content = rtf_to_text(f.read())
        elif fn.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text_content = f.read()
    except Exception as e:
        log.error("extract_text failed for %s: %s", file_name, e)
    return text_content.strip()


# ---------------- Telegram helpers ----------------
def bg(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    
    def _handle_task_result(task):
        TASKS.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            coro_name = getattr(coro, '__name__', str(coro))
            log.error(f"Фоновая ошибка в задаче {coro_name}: {e}", exc_info=True)
            
    t.add_done_callback(_handle_task_result)
    return t


_seen_updates = set()


def is_duplicate(update_id) -> bool:
    if update_id is None:
        return False
    if update_id in _seen_updates:
        return True
    _seen_updates.add(update_id)
    if len(_seen_updates) > 5000:
        for uid in sorted(_seen_updates)[:-2500]:
            _seen_updates.discard(uid)
    return False


async def send_single_message(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with HTTP.post(f"{TELEGRAM_API}/sendMessage", json=payload) as resp:
        result = await resp.json()
    if not result.get("ok") and parse_mode:
        payload.pop("parse_mode", None)
        async with HTTP.post(f"{TELEGRAM_API}/sendMessage", json=payload) as resp:
            result = await resp.json()
    return result


async def send_telegram(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    if not text:
        return
    if len(text) <= 4000:
        return await send_single_message(chat_id, text, reply_markup, parse_mode)
    
    parts = []
    current_part = ""
    for paragraph in text.split("\n\n"):
        if len(current_part) + len(paragraph) + 2 < 3800:
            current_part += ("\n\n" if current_part else "") + paragraph
        else:
            if current_part:
                parts.append(current_part)
            current_part = paragraph
    if current_part:
        parts.append(current_part)
        
    for i, p in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await send_single_message(chat_id, p, markup, parse_mode)
        await asyncio.sleep(0.3)


async def send_document_bytes(chat_id, file_bytes: bytes, filename: str, caption: str = "", content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    if caption:
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "Markdown")
    form.add_field("document", file_bytes, filename=filename, content_type=content_type)
    async with HTTP.post(f"{TELEGRAM_API}/sendDocument", data=form) as resp:
        await resp.json()


async def http_edit_message_text(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with HTTP.post(f"{TELEGRAM_API}/editMessageText", json=payload) as resp:
        await resp.json()


async def answer_callback(cb_id: str):
    try:
        async with HTTP.post(f"{TELEGRAM_API}/answerCallbackQuery",
                             json={"callback_query_id": cb_id}) as resp:
            await resp.json()
    except Exception:
        pass


def get_keyboard(is_admin=False):
    kb = [
        [{"text": "📁 Мои резюме"}, {"text": "📥 Загрузить резюме"}],
        [{"text": "🔍 Поиск вакансий"}, {"text": "🛠 Адаптация резюме"}],
        [{"text": "📊 Анализ навыков (Skill Gap)"}, {"text": "📋 Аудит резюме"}],
        [{"text": "🎤 Тренажер собеседований"}, {"text": "📌 Трекер откликов"}],
        [{"text": "🎁 Бонусы (Репост & Друзья)"}, {"text": "💎 Оплата и Баланс"}],
        [{"text": "🚀 Запустить бота"}, {"text": "💬 Обратная связь"}],
        [{"text": "ℹ️ Помощь"}],
    ]
    if is_admin:
        kb.append([{"text": "👑 Админ-панель"}, {"text": "📩 Сообщения от пользователей"}])
    return {"keyboard": kb, "resize_keyboard": True}


# ---------------- hh.ru поиск и парсинг ----------------
async def hh_api_search(query: str):
    try:
        async with HTTP.get("https://api.hh.ru/vacancies",
                            params={"text": query, "area": "1", "per_page": "100"},
                            headers={"User-Agent": "LemusCareerBot/1.5"}) as resp:
            data = await resp.json()
        items = []
        for i in data.get("items", []):
            salary = i.get("salary")
            sal_str = ""
            if salary:
                frm = salary.get("from")
                to = salary.get("to")
                cur_s = salary.get("currency", "RUR")
                if frm and to: sal_str = f"💰 {frm} – {to} {cur_s}"
                elif frm: sal_str = f"💰 от {frm} {cur_s}"
                elif to: sal_str = f"💰 до {to} {cur_s}"
            
            items.append({
                "id": i.get("id"), 
                "name": i.get("name"),
                "company": (i.get("employer") or {}).get("name"),
                "salary": sal_str,
                "url": i.get("alternate_url") or f"https://hh.ru/vacancy/{i.get('id')}"
            })
        return items or None
    except Exception as e:
        log.warning("hh API failed: %s", str(e)[:150])
        return None


async def hh_scrape_search(query: str):
    try:
        async with HTTP.get("https://hh.ru/search/vacancy",
                            params={"text": query, "area": "1", "items_on_page": "100"},
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}) as resp:
            page = await resp.text()
        match = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', page, re.S)
        if not match:
            return None
        data = json.loads(html.unescape(match.group(1)))
        items = (data.get("vacancySearchResult") or {}).get("vacancies") or []
        out = []
        for it in items:
            vid = it.get("vacancyId") or it.get("id")
            sal = it.get("salary")
            sal_str = f"💰 {sal}" if sal else ""
            out.append({
                "id": vid, 
                "name": it.get("name"),
                "company": (it.get("company") or {}).get("name"),
                "salary": sal_str,
                "url": f"https://hh.ru/vacancy/{vid}"
            })
        return out or None
    except Exception as e:
        log.warning("hh scrape failed: %s", str(e)[:150])
        return None


async def get_vacancy_details(vacancy_id: str) -> str:
    try:
        async with HTTP.get(f"https://api.hh.ru/vacancies/{vacancy_id}",
                            headers={"User-Agent": "LemusCareerBot/1.5"}) as resp:
            data = await resp.json()
            
        description = re.sub(r'<[^>]+>', '', data.get("description", ""))
        skills = ", ".join([s.get("name", "") for s in data.get("key_skills", [])])
        return f"Требования и описание:\n{description}\n\nКлючевые навыки: {skills}"
    except Exception as e:
        log.error(f"Failed to fetch vacancy details {vacancy_id}: {e}")
        return ""


# ---------------- Вывод порции вакансий с быстрой оценкой Match Rate на лету ----------------
async def send_vacancies_page(chat_id: int, page: int = 0):
    cached = user_search_cache.get(chat_id)
    if not cached or not cached.get("items"):
        await send_telegram(chat_id, "💡 *Подсказка:* Список вакансий устарел. Нажмите «🔍 Поиск вакансий» в меню, чтобы запустить новый подбор.")
        return

    items = cached["items"]
    page_size = 15
    start = page * page_size
    end = start + page_size
    chunk = items[start:end]

    top_companies = ["сбер", "мтс", "яндекс", "т-банк", "тинькофф", "втб", "альфа", "билайн", "мегафон", "ростелеком", "первый бит"]

    if not chunk:
        await send_telegram(chat_id, "🏁 Больше нет новых вакансий в этой выдаче. Вы просмотрели все подходящие варианты!")
        return

    await send_telegram(chat_id, f"📄 Показаны вакансии с {start + 1} по min({end}, {len(items)}) из {len(items)} (всего найдено: {len(items)}):")

    active_resume = get_active_resume(chat_id)

    for v in chunk:
        vid = str(v["id"])
        name = v.get("name") or "Вакансия"
        comp = v.get("company") or "Компания"
        sal = v.get("salary") or ""
        temp_vacancies[vid] = {"title": name, "employer": comp}
        
        comp_lower = comp.lower()
        is_top = any(tc in comp_lower for tc in top_companies)
        badge = "⭐ *[ТОП-КОМПАНИЯ]*\n" if is_top else ""
        
        match_badge = ""
        if active_resume:
            quick_prompt = (
                f"Оцени соответствие резюме вакансии '{name}' в компанию '{comp}' кратко. "
                "Выдай ТОЛЬКО одну строку в формате: 'Соответствие: X% (краткая причина в 3-5 слов)'."
            )
            eval_res = await asyncio.to_thread(ai_generate, quick_prompt)
            if eval_res:
                match_badge = f"🎯 _{eval_res.strip()}_\n"
            else:
                match_badge = "🎯 Соответствие: 80% (релевантный опыт)\n"
        else:
            match_badge = "🎯 Соответствие: резюме не загружено\n"

        sal_line = f"{sal}\n" if sal else ""
        
        markup = {"inline_keyboard": [
            [
                {"text": "👍 Откликнулся", "callback_data": f"like_{vid}"},
                {"text": "✍️ Сопроводительное", "callback_data": f"gen_{vid}"}
            ],
            [
                {"text": "📊 Соответствие", "callback_data": f"match_{vid}"},
                {"text": "🗑 Мусор", "callback_data": f"hide_{vid}"}
            ]
        ]}
        await send_telegram(chat_id, f"{badge}🏢 *{comp}*\n💼 [{name}]({v.get('url')})\n{sal_line}{match_badge}", markup)
        await asyncio.sleep(0.2)

    if end < len(items):
        next_page = page + 1
        more_markup = {
            "inline_keyboard": [
                [{"text": "▶️ Далее (следующие 15)", "callback_data": f"page_{next_page}"}]
            ]
        }
        await send_telegram(chat_id, f"💡 *Онбординг:* Осталось еще {len(items) - end} вакансий. Листайте дальше кнопкой ниже.", more_markup)
    else:
        await send_telegram(chat_id, "🎉 Вы просмотрели всю выдачу! Используйте кнопки меню для дальнейших действий.")


# ---------------- Фичи бота ----------------
async def handle_search(chat_id: int, is_admin: bool):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ У вас закончились запросы! Пополните баланс через меню «💎 Оплата и Баланс» или пригласите друзей.", get_keyboard(is_admin))
        return

    await send_telegram(chat_id, "🔍 *Онбординг:* Собираю все доступные вакансии по рынку (до сотен позиций с учетом топ-компаний)...")
    
    queries = [
        "Руководитель направления", 
        "Директор по развитию", 
        "Руководитель проектов", 
        "Head of Business Development", 
        "Руководитель отдела продаж"
    ]
    
    all_items = []
    for q in queries:
        res = await hh_scrape_search(q) or await hh_api_search(q)
        if res:
            all_items.extend(res)

    if not all_items:
        await send_telegram(chat_id, "⚠️ Не удалось найти вакансии. Попробуйте повторить запрос чуть позже.", get_keyboard(is_admin))
        return

    stop_words = ["сборщик", "упаковщик", "кассир", "повар", "официант", "курьер", "продавец-консультант", "сотрудник ресторана"]
    top_companies = ["сбер", "мтс", "яндекс", "т-банк", "тинькофф", "втб", "альфа", "билайн", "мегафон", "ростелеком", "первый бит"]

    unique_items = {}
    for v in all_items:
        vid = str(v["id"])
        if vid in unique_items:
            continue
            
        name_lower = (v.get("name") or "").lower()
        if any(sw in name_lower for sw in stop_words):
            continue
        if is_vacancy_hidden(chat_id, vid):
            continue
            
        unique_items[vid] = v

    filtered_list = list(unique_items.values())

    def sort_priority(item):
        comp_lower = (item.get("company") or "").lower()
        is_top = any(tc in comp_lower for tc in top_companies)
        return (0 if is_top else 1)

    filtered_list.sort(key=sort_priority)

    if not filtered_list:
        await send_telegram(chat_id, "⚠️ Все подходящие вакансии скрыты или отфильтрованы.", get_keyboard(is_admin))
        return

    user_search_cache[chat_id] = {"items": filtered_list}

    await send_telegram(chat_id, f"🔥 Нашел огромную базу: {len(filtered_list)} вакансий! Вверху списка — предложения от топ-компаний:", get_keyboard(is_admin))
    await send_vacancies_page(chat_id, page=0)


async def run_skill_gap_analysis(chat_id: int):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов для анализа навыков!")
        return

    await send_telegram(chat_id, "📊 *Онбординг:* Провожу анализ навыков (Skill Gap) и формирую матрицу компетенций на основе вашего активного резюме...")
    resume = get_active_resume(chat_id)
    if not resume:
        await send_telegram(chat_id, "💡 *Подсказка:* Сначала загрузите резюме!\n\nОтправьте файл с вашим резюме прямо в этот чат.")
        return

    current_date = datetime.date.today().strftime("%d.%m.%Y")
    prompt = (
        f"Текущая дата: {current_date}. Ты — экспертный карьерный консультант уровня C-level. Проведи глубокий анализ навыков (Skill Gap Analysis) для этого кандидата, "
        "претендующего на позиции Head of / Директор по развитию / Руководитель направления.\n"
        "ВАЖНО: Оценивай гибридные роли как современное преимущество и широту компетенций.\n"
        "Выдели:\n"
        "1. Сильные управленческие и технические компетенции.\n"
        "2. Зоны роста и пробелы для топ-позиций в крупных экосистемах.\n"
        "3. Рекомендации по развитию на ближайшие 3–6 месяцев.\n\n"
        f"Резюме кандидата:\n{resume[:8000]}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен.")
        return
    await send_telegram(chat_id, f"📊 *Анализ навыков (Skill Gap):*\n\n{analysis}\n\n💡 *Совет:* Используйте эти рекомендации для подготовки к собеседованиям в Тренажере!")


async def run_ai_generation(chat_id: int, vac_info: dict):
    await send_telegram(chat_id, f"✍️ *Онбординг:* Готовлю профессиональное сопроводительное письмо для *{vac_info['employer']}* на позицию «{vac_info['title']}»...")
    resume = get_active_resume(chat_id) or "Опыт не указан."
    letter = await asyncio.to_thread(ai_generate,
        f"Напиши профессиональное сопроводительное письмо на позицию '{vac_info['title']}' "
        f"в '{vac_info['employer']}' на основе резюме:\n\n{resume}")
    if not letter:
        await send_telegram(chat_id, "⚠️ ИИ недоступен, письмо не получилось.")
        return
    await send_telegram(chat_id, f"📝 *Сопроводительное письмо готово:*\n\n{letter}\n\n💡 Скопируйте текст и используйте при отправке отклика работодателю.")


async def run_vacancy_match(chat_id: int, vac_info: dict):
    await send_telegram(chat_id, f"📊 *Онбординг:* Анализирую соответствие вашего резюме вакансии *{vac_info['title']}* в компании *{vac_info['employer']}*...")
    resume = get_active_resume(chat_id) or "Резюме не найдено."
    prompt = (
        f"Проанализируй, насколько резюме кандидата подходит под вакансию '{vac_info['title']}' в компанию '{vac_info['employer']}'. "
        "Дай оценку соответствия в процентах, перечисли сильные стороны кандидата, "
        f"а также укажи ключевые пробелы:\n\nРезюме:\n{resume}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен.")
        return
    await send_telegram(chat_id, f"📊 *Анализ соответствия вакансии:*\n\n{analysis}")


async def run_resume_adaptation(chat_id: int, resume_id: int, vacancy_text: str):
    await send_telegram(chat_id, "🛠 *Онбординг:* Адаптирую резюме под специфику вакансии и формирую чистый файл Word...")
    resume_text = get_resume_by_id(chat_id, resume_id) or get_active_resume(chat_id)
    
    if not resume_text:
        await send_telegram(chat_id, "💡 *Подсказка:* Сначала загрузите резюме в чат!")
        return

    match = re.search(r'hh\.ru/vacancy/(\d+)', vacancy_text)
    if match:
        vac_id = match.group(1)
        fetched_text = await get_vacancy_details(vac_id)
        if fetched_text:
            vacancy_text = fetched_text

    current_date = datetime.date.today().strftime("%d.%m.%Y")
    prompt_resume = (
        f"Текущая дата: {current_date}. Ты — элитный карьерный стратег. Перепиши и оптимизируй резюме кандидата строго под требования вакансии. "
        "Сохрани правдивость фактов, но полностью репозиционируй опыт так, чтобы он закрывал требования вакансии.\n"
        "ВАЖНО: Выдай ТОЛЬКО текст резюме, начиная сразу с ФИО. Без вступительных фраз.\n"
        "Структура: 1. ФИО и контакты 2. Summary 3. Ключевые навыки 4. Опыт работы 5. Образование\n\n"
        f"--- ТРЕБОВАНИЯ ВАКАНСИИ ---\n{vacancy_text[:3000]}\n\n"
        f"--- ИСХОДНОЕ РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:6000]}"
    )
    adapted_text = await asyncio.to_thread(ai_generate, prompt_resume)

    prompt_letter = (
        "Напиши короткое, емкое и профессиональное сопроводительное письмо к этой вакансии от лица кандидата (до 1000 знаков).\n\n"
        f"--- ТРЕБОВАНИЯ ВАКАНСИИ ---\n{vacancy_text[:2000]}\n\n"
        f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}"
    )
    cover_letter = await asyncio.to_thread(ai_generate, prompt_letter)

    if not adapted_text:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return

    if "---" in adapted_text:
        adapted_text = adapted_text.split("---")[-1].strip()

    if cover_letter:
        await send_telegram(chat_id, f"📝 *Сопроводительное письмо:*\n\n{cover_letter}")

    try:
        doc = Document()
        for p in adapted_text.split("\n"):
            clean_p = re.sub(r'[*#]', '', p).strip()
            if clean_p:
                doc.add_paragraph(clean_p)
        
        stream = io.BytesIO()
        doc.save(stream)
        file_bytes = stream.getvalue()

        await send_document_bytes(
            chat_id, file_bytes, filename="Adapted_Resume.docx", 
            caption="📄 *Готово! Ваше адаптированное резюме (Word) прикреплено выше. Смело отправляйте его работодателю!*",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    except Exception as e:
        log.error("DOCX generation failed: %s", e)
        await send_telegram(chat_id, "⚠️ Ошибка формирования файла.")


async def run_resume_audit(chat_id: int):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return

    await send_telegram(chat_id, "📋 *Онбординг:* Провожу жесткий C-Level аудит вашего резюме и формирую профессиональный Word-документ...")
    resume = get_active_resume(chat_id)
    if not resume:
        await send_telegram(chat_id, "💡 *Подсказка:* Сначала загрузите резюме в чат!")
        return

    current_date = datetime.date.today().strftime("%d.%m.%Y")
    audit_prompt = (
        f"Текущая дата: {current_date}. Сделай жесткий, глубокий аудит этого резюме с позиции C-level.\n"
        "Анализируй бизнес-результаты, метрики и подачу:\n\n"
        f"{resume[:8000]}"
    )
    audit_text = await asyncio.to_thread(ai_generate, audit_prompt)

    rewrite_prompt = (
        "Перепиши это резюме для позиций уровня Head / Director. Убери мелкую операционку, сделай упор на бизнес-результаты и метрики. "
        "Сохрани структуру (Контакты, Summary, Навыки, Опыт работы, Образование). Выдай ТОЛЬКО чистый текст без звездочек:\n\n"
        f"{resume[:8000]}"
    )
    improved_text = await asyncio.to_thread(ai_generate, rewrite_prompt)

    if audit_text and improved_text:
        await send_telegram(chat_id, f"📋 *C-Level Аудит:*\n\n{audit_text}")
        doc = Document()
        for p in improved_text.split("\n"):
            clean_p = re.sub(r'[*#]', '', p).strip()
            if clean_p:
                doc.add_paragraph(clean_p)
        stream = io.BytesIO()
        doc.save(stream)
        await send_document_bytes(chat_id, stream.getvalue(), "C_Level_Resume_Pro.docx", "✅ Ваше оптимизированное резюме (Word) готово!")
    else:
        await send_telegram(chat_id, "⚠️ Ошибка ИИ.")


async def handle_document(chat_id: int, document: dict, is_admin: bool):
    file_id = document["file_id"]
    file_name = document.get("file_name", "resume.pdf")
    try:
        async with HTTP.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}) as resp:
            file_info = await resp.json()
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            await send_telegram(chat_id, "⚠️ Не смог скачать файл.", get_keyboard(is_admin))
            return
        async with HTTP.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as f_resp:
            content = await f_resp.read()
    except Exception as e:
        log.error("download failed: %s", e)
        await send_telegram(chat_id, "⚠️ Ошибка скачивания файла.", get_keyboard(is_admin))
        return

    path = f"tmp_{chat_id}_{file_name}"
    with open(path, "wb") as f:
        f.write(content)
    text_content = await asyncio.to_thread(extract_text, path, file_name)
    if os.path.exists(path):
        os.remove(path)

    if not text_content or not text_content.strip():
        await send_telegram(chat_id, "⚠️ Не удалось извлечь текст из файла. Попробуйте формат PDF или DOCX.", get_keyboard(is_admin))
        return
        
    add_resume(chat_id, file_name, text_content)
    
    success_text = (
        f"✅ *Отлично! Резюме «{file_name}» успешно распознано и загружено.*\n\n"
        "💡 *Что можно сделать прямо сейчас (онбординг):*\n"
        "1️⃣ Нажмите **«📋 Аудит резюме»**, чтобы получить детальный разбор от C-level ментора.\n"
        "2️⃣ Нажмите **«🔍 Поиск вакансий»**, чтобы система подобрала для вас топовые позиции с расчетом совпадения.\n"
        "3️⃣ Нажмите **«🎤 Тренажер собеседований»**, чтобы потренироваться отвечать на каверзные вопросы."
    )
    await send_telegram(chat_id, success_text, get_keyboard(is_admin))


async def activate_resume(chat_id: int, rid: str):
    try:
        rid = int(rid)
    except ValueError:
        return
    cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (chat_id,))
    cur.execute("UPDATE resumes SET active=1 WHERE id=? AND user_id=?", (rid, chat_id))
    conn.commit()
    await send_telegram(chat_id, "✅ Резюме успешно переключено и сделано активным для всех ИИ-модулей.", get_keyboard(ADMIN_ID != 0 and chat_id == ADMIN_ID))


# ---------------- Тренажер собеседований (Интерактивный) ----------------
async def start_interview_simulator(chat_id: int):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов для запуска тренажера!")
        return
    resume = get_active_resume(chat_id)
    if not resume:
        await send_telegram(chat_id, "💡 *Подсказка:* Сначала загрузите резюме в чат!")
        return

    await send_telegram(chat_id, "🎤 *Онбординг в тренажер:* Сейчас ИИ изучит ваш профиль и выступит в роли строгого фаундера/HR-директора.\nВсего будет 3 вопроса. Отвечайте развернуто, как на реальном интервью.")
    
    prompt = (
        "Ты — строгий фаундер технологической компании или HR-директор крупной корпорации. "
        "На основе этого резюме задай кандидату первый каверзный управленческий или стратегический вопрос на собеседовании. "
        "Вопрос должен быть профессиональным, без воды.\n\nРезюме:\n" + resume[:5000]
    )
    first_question = await asyncio.to_thread(ai_generate, prompt)
    if not first_question:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен.")
        return

    interview_sessions[chat_id] = {"question_count": 1, "history": []}
    user_states[chat_id] = "interview_active"
    
    await send_telegram(chat_id, f"🎙 *Вопрос 1 из 3:*\n\n{first_question}\n\n_💡 Напишите ваш ответ ответным сообщением в этот чат._")


async def handle_interview_answer(chat_id: int, answer_text: str):
    session = interview_sessions.get(chat_id)
    if not session:
        user_states.pop(chat_id, None)
        await send_telegram(chat_id, "⚠️ Сессия тренажера завершена или устарела. Запустите новую через меню.")
        return

    q_count = session["question_count"]
    await send_telegram(chat_id, "🔎 Анализирую ваш ответ и формирую следующий шаг...")

    prompt = (
        f"Кандидат ответил на вопрос на управленческом собеседовании.\n"
        f"Ответ кандидата: {answer_text}\n\n"
        "Дай короткий фидбек по ответу (что сильного, чего не хватило) и задай следующий каверзный вопрос (это вопрос номер " + str(q_count + 1) + " из 3). "
        "Если это был 3-й вопрос, подведи общий итог собеседования и оцени готовность кандидата."
    )
    feedback_and_next = await asyncio.to_thread(ai_generate, prompt)

    if q_count >= 3:
        user_states.pop(chat_id, None)
        interview_sessions.pop(chat_id, None)
        await send_telegram(chat_id, f"🏁 *Итоги тренировки собеседования:*\n\n{feedback_and_next}\n\n🎉 Отличная тренировка! Вы можете пройти тренажер заново в любой момент через меню.")
    else:
        session["question_count"] += 1
        await send_telegram(chat_id, f"💡 *Разбор ответа и следующий вопрос:*\n\n{feedback_and_next}\n\n_💡 Жду ваш ответ на следующий вопрос._")


# ---------------- Система оплаты ----------------
async def send_stars_invoice(chat_id, amount_stars: int, title: str, payload: str):
    await HTTP.post(f"{TELEGRAM_API}/sendInvoice", json={
        "chat_id": chat_id,
        "title": title,
        "description": "Пополнение баланса карьерного агента",
        "payload": payload,
        "currency": "XTR",
        "prices": [{"label": "Stars", "amount": amount_stars}]
    })


# ---------------- Обработка сообщений ----------------
async def process_message(msg: dict):
    chat_id = msg["chat"]["id"]
    username = msg["chat"].get("username", "")
    text = (msg.get("text") or "").strip()
    document = msg.get("document")
    photo = msg.get("photo")

    referrer_id = None
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            try:
                referrer_id = int(parts[1])
            except ValueError:
                pass

    register_user(chat_id, username, referrer_id)
    is_admin = (ADMIN_ID != 0 and chat_id == ADMIN_ID)

    if is_admin and text.startswith("/reply"):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3:
            try:
                target_uid = int(parts[1])
                reply_text = parts[2]
                await send_telegram(target_uid, f"💬 *Сообщение от администратора:*\n\n{reply_text}")
                await send_telegram(chat_id, f"✅ Ответ успешно отправлен пользователю `{target_uid}`.")
            except ValueError:
                await send_telegram(chat_id, "⚠️ Ошибка в ID пользователя.")
        else:
            await send_telegram(chat_id, "⚠️ Формат: `/reply <user_id> <текст>`")
        return

    if user_states.get(chat_id) == "interview_active":
        bg(handle_interview_answer(chat_id, text))
        return

    if user_states.get(chat_id) == "waiting_for_feedback":
        user_states.pop(chat_id, None)
        cur.execute("INSERT INTO feedback (user_id, username, message) VALUES (?, ?, ?)", (chat_id, username, text))
        conn.commit()
        await send_telegram(chat_id, "✅ Спасибо! Ваше сообщение успешно отправлено администратору.")
        await send_telegram(
            ADMIN_ID,
            f"📩 *Новое сообщение обратной связи!*\nОт: @{username or 'нет'} (ID: `{chat_id}`)\n\n💬 Текст:\n{text}\n\n_Ответить:_ `/reply {chat_id} Текст`"
        )
        return

    if user_states.get(chat_id) == "waiting_for_repost":
        user_states.pop(chat_id, None)
        urls = re.findall(r'(https?://[^\s]+)', text) if text else []
        if urls:
            url = urls[0].lower()
            network = "Other"
            if "vk.com" in url: network = "VK"
            elif "linkedin" in url: network = "LinkedIn"
            elif "tenchat" in url: network = "TenChat"
            elif "setka" in url or "hh.ru" in url: network = "Сетка"
            elif "t.me" in url: network = "Telegram"
            
            cur.execute("SELECT 1 FROM social_shares WHERE user_id=? AND network=?", (chat_id, network))
            if cur.fetchone():
                await send_telegram(chat_id, f"⚠️ Вы уже получали бонус за репост в платформе {network}.")
            else:
                cur.execute("INSERT INTO social_shares (user_id, network) VALUES (?, ?)", (chat_id, network))
                conn.commit()
                admin_add_balance(chat_id, 20)
                await send_telegram(chat_id, f"🎉 Ссылка распознана! Вам начислено +20 запросов за пост в {network}.")
            return
        await send_telegram(chat_id, "⚠️ Ссылка не найдена. Отправьте прямую ссылку, начинающуюся с http:// или https://.")
        return

    if photo and user_states.get(chat_id) == "waiting_for_receipt":
        user_states.pop(chat_id, None)
        await HTTP.post(f"{TELEGRAM_API}/forwardMessage", json={
            "chat_id": ADMIN_ID, "from_chat_id": chat_id, "message_id": msg["message_id"]
        })
        await send_telegram(chat_id, "✅ Чек отправлен администратору. Ожидайте подтверждения и зачисления запросов!")
        admin_markup = {
            "inline_keyboard": [
                [{"text": "✅ +50 запросов", "callback_data": f"paycred_{chat_id}_50"}],
                [{"text": "⭐ Безлимит 10 дней", "callback_data": f"payunl_{chat_id}"}]
            ]
        }
        await send_telegram(ADMIN_ID, f"📸 *Новый чек на проверку!*\nОт: @{username or 'нет'} (ID: `{chat_id}`)", reply_markup=admin_markup)
        return

    if document:
        await handle_document(chat_id, document, is_admin)
        return
    if not text:
        return

    if is_admin and text.startswith("/add_credits"):
        parts = text.split()
        if len(parts) == 3:
            try:
                target_id = int(parts[1])
                amount = int(parts[2])
                new_bal = admin_add_balance(target_id, amount)
                await send_telegram(chat_id, f"✅ Пользователю `{target_id}` добавлено {amount} запросов. Баланс: `{new_bal}`")
            except ValueError:
                await send_telegram(chat_id, "⚠️ Формат: `/add_credits 123456789 50`")
        return

    if user_states.get(chat_id) == "waiting_for_adaptation_vacancy":
        rid = user_adapt_target.get(chat_id)
        user_states.pop(chat_id, None)
        user_adapt_target.pop(chat_id, None)

        if not spend_balance(chat_id, cost=1):
            await send_telegram(chat_id, "⚠️ Недостаточно запросов!", get_keyboard(is_admin))
            return
        bg(run_resume_adaptation(chat_id, rid, text))
        return

    if text.startswith("/start") or text == "🚀 Запустить бота":
        if is_admin:
            welcome_text = "👋 Привет, Антон! У тебя активирован бесконечный безлимитный доступ (Админ-режим).\nДля работы отправь файл резюме в чат."
        else:
            welcome_text = (
                "👋 Привет! Я — твой личный ИИ-карьерный агент (Версия 1.5).\n\n"
                "💡 *Быстрый старт (Онбординг):*\n"
                "1️⃣ Отправь файл резюме (*PDF или DOCX*) прямо в этот чат.\n"
                "2️⃣ Используй меню ниже для аудита, поиска вакансий с hh.ru и тренировки на собеседованиях.\n\n"
                "🎁 Тебе начислено *7 приветственных запросов*!"
            )
        await send_telegram(chat_id, welcome_text, get_keyboard(is_admin))

    elif text in ("👥 Пригласить друга", "🎁 Бонусы (Репост & Друзья)"):
        bot_info = await HTTP.get(f"{TELEGRAM_API}/getMe")
        bot_data = await bot_info.json()
        bot_username = bot_data.get("result", {}).get("username", "bot")
        ref_link = f"https://t.me/{bot_username}?start={chat_id}"
        
        bonus_text = (
            "🎁 *Программа лояльности и бонусы*\n\n"
            "👥 *1. Пригласить друга (+7 запросов)*\n"
            f"Ваша персональная ссылка:\n`{ref_link}`\n\n"
            "📢 *2. Поделиться в соцсетях (+20 запросов)*\n"
            "Опубликуйте пост о боте в LinkedIn, TenChat, VK, Сетке или Telegram и пришлите ссылку."
        )
        kb = {"inline_keyboard": [[{"text": "🔗 Отправить ссылку на репост", "callback_data": "send_repost_proof"}]]}
        await send_telegram(chat_id, bonus_text, kb)

    elif text == "💬 Обратная связь":
        user_states[chat_id] = "waiting_for_feedback"
        await send_telegram(chat_id, "💬 Напишите ваш отзыв или вопрос в следующем сообщении, и я передам его администратору.")

    elif text == "📩 Сообщения от пользователей":
        if not is_admin:
            return
        cur.execute("SELECT id, user_id, username, message, created_at FROM feedback ORDER BY id DESC LIMIT 10")
        rows = cur.fetchall()
        if not rows:
            await send_telegram(chat_id, "📭 Входящих сообщений пока нет.")
            return
        feedbacks_msg = "📩 *Последние сообщения от пользователей:*\n\n"
        for r in rows:
            feedbacks_msg += f"🆔 ID: `{r[1]}` (@{r[2] or 'нет'})\n💬 {r[3]}\n⏱ `{r[4]}`\n_Ответ:_ `/reply {r[1]} Текст`\n\n──────────────────\n\n"
        await send_telegram(chat_id, feedbacks_msg)

    elif text == "💎 Оплата и Баланс":
        if is_admin:
            status_str = "📊 Баланс: `∞ Безлимит` (Администратор — лимиты отключены)"
        else:
            data = get_user_data(chat_id)
            balance = data["balance"]
            unl = data["unlimited_until"]
            status_str = f"📊 Баланс: `{balance} запросов`"
            if unl:
                status_str = f"⭐ Активен безлимит до: `{unl}`"

        balance_text = (
            f"💎 *Оплата и Баланс*\n\n{status_str}\n\n"
            "💳 *Тарифы:*\n"
            "1️⃣ **Пакет «50 запросов»:** 100 ⭐ ИЛИ 200 руб.\n"
            "2️⃣ **Безлимит на 10 дней:** 500 ⭐ ИЛИ 500 руб.\n\n"
            "🏦 *Реквизиты СБП:* `2202208459089018`\n"
            "_После перевода отправьте скриншот чека в чат._"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "⭐ Оплатить 50 запросов (100 Звезд)", "callback_data": "buy_pack_stars"}],
                [{"text": "⭐ Безлимит 10 дней (500 Звезд)", "callback_data": "buy_unl_stars"}],
                [{"text": "📄 Отправить чек об оплате", "callback_data": "send_receipt"}]
            ]
        }
        await send_telegram(chat_id, balance_text, kb)

    elif text == "🛠 Адаптация резюме":
        rows = list_resumes(chat_id)
        if not rows:
            await send_telegram(chat_id, "💡 *Онбординг:* Сначала загрузите резюме в чат, чтобы бот знал, что адаптировать!")
            return
        kb = {"inline_keyboard": [[{"text": f"📄 {r['name']}", "callback_data": f"adaptsel_{r['id']}"}] for r in rows]}
        await send_telegram(chat_id, "🛠 *Шаг 1:* Выберите нужное резюме из списка ниже, а затем отправьте текст или прямую ссылку на вакансию с hh.ru.", kb)

    elif text == "📋 Аудит резюме":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "💡 *Онбординг:* Сначала отправьте файл с резюме в этот чат.")
            return
        bg(run_resume_audit(chat_id))

    elif text == "📊 Анализ навыков (Skill Gap)":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "💡 *Онбординг:* Сначала отправьте файл с резюме в этот чат.")
            return
        bg(run_skill_gap_analysis(chat_id))

    elif text == "🎤 Тренажер собеседований":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "💡 *Онбординг:* Для запуска тренировки боту нужно ваше резюме. Загрузите его файлом!")
            return
        bg(start_interview_simulator(chat_id))

    elif text == "📌 Трекер откликов":
        cur.execute("SELECT vacancy_id, title, status FROM liked_vacancies WHERE user_id=? ORDER BY id DESC LIMIT 15", (chat_id,))
        rows = cur.fetchall()
        if not rows:
            await send_telegram(chat_id, "📌 *Трекер откликов пуст.*\n\n💡 *Как сюда попадают вакансии?* При поиске вакансий нажимайте кнопку **«👍 Откликнулся»**, и бот автоматически занесет их в этот список для контроля.")
        else:
            tracker_msg = "📌 *Ваш трекер откликов:*\n\n"
            for r in rows:
                tracker_msg += f"• [{r[1]}]({f'https://hh.ru/vacancy/{r[0]}'})\nСтатус: `{r[2]}`\n\n"
            await send_telegram(chat_id, tracker_msg)

    elif text == "ℹ️ Помощь":
        help_text = (
            "ℹ️ *Справка и гид по боту (Версия 1.5):*\n\n"
            "• 📥 *Загрузка резюме* — отправьте PDF или DOCX файл.\n"
            "• 🔍 *Поиск вакансий* — подбор с зарплатами и расчетом Match Rate.\n"
            "• 🛠 *Адаптация* — переупаковка резюме под конкретное описание вакансии или ссылку с hh.ru.\n"
            "• 🎤 *Тренажер* — интерактивный раунд вопросов с ИИ.\n"
            "• 📌 *Трекер* — учет ваших откликов."
        )
        await send_telegram(chat_id, help_text, get_keyboard(is_admin))

    elif text in ("👑 Админ-панель", "/admin"):
        if not is_admin:
            return
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM resumes")
        total_resumes = cur.fetchone()[0]
        await send_telegram(chat_id, f"👑 *Админ-панель*\n\n👥 Пользователей: `{total_users}`\n📁 Резюме: `{total_resumes}`")

    elif text == "📁 Мои резюме":
        rows = list_resumes(chat_id)
        if not rows:
            await send_telegram(chat_id, "💡 У вас пока нет загруженных резюме. Отправьте файл в чат.")
        else:
            kb = {"inline_keyboard": [[{"text": f"{'✅ Активное' if r['active'] else '📄'} {r['name']}", "callback_data": f"act_{r['id']}"}] for r in rows]}
            await send_telegram(chat_id, "📁 *Ваши резюме:* (нажмите на нужное, чтобы сделать его активным для ИИ)", kb)

    elif text == "📥 Загрузить резюме":
        await send_telegram(chat_id, "📄 *Онбординг:* Отправьте файл вашего резюме (*PDF или DOCX*) прямо в этот чат.")

    elif text == "🔍 Поиск вакансий":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "💡 *Онбординг:* Сначала загрузите резюме, чтобы бот мог рассчитать процент соответствия (Match Rate) для каждой вакансии!")
        else:
            bg(handle_search(chat_id, is_admin))

    else:
        await send_telegram(chat_id, "ℹ️ Воспользуйтесь кнопками удобного меню ниже.", get_keyboard(is_admin))


# ---------------- Вебхук ----------------
async def telegram_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="OK")

    if is_duplicate(data.get("update_id")):
        return web.Response(text="OK")

    if "pre_checkout_query" in data:
        pcq = data["pre_checkout_query"]
        await HTTP.post(f"{TELEGRAM_API}/answerPreCheckoutQuery", json={"pre_checkout_query_id": pcq["id"], "ok": True})
        return web.Response(text="OK")

    if "message" in data:
        msg = data["message"]
        if msg.get("successful_payment"):
            uid = msg["chat"]["id"]
            payload = msg["successful_payment"].get("invoice_payload", "")
            if "unl" in payload:
                admin_set_unlimited(uid, 10)
                await send_telegram(uid, "🎉 Оплата прошла! Безлимит на 10 дней активирован.")
            else:
                admin_add_balance(uid, 50)
                await send_telegram(uid, "🎉 Оплата прошла! Начислено 50 запросов.")
        else:
            bg(process_message(msg))

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        message_id = (cb.get("message") or {}).get("message_id")
        data_str = cb.get("data", "") or ""
        bg(answer_callback(cb.get("id", "")))
        
        if chat_id:
            if data_str.startswith("page_"):
                bg(send_vacancies_page(chat_id, page=int(data_str.split("_")[1])))
            elif data_str == "buy_pack_stars":
                bg(send_stars_invoice(chat_id, 100, "Пакет 50 запросов", "credits_50"))
            elif data_str == "buy_unl_stars":
                bg(send_stars_invoice(chat_id, 500, "Безлимит на 10 дней", "unl_10d"))
            elif data_str == "send_receipt":
                user_states[chat_id] = "waiting_for_receipt"
                await send_telegram(chat_id, "📸 Отправьте скриншот чека в чат.")
            elif data_str == "send_repost_proof":
                user_states[chat_id] = "waiting_for_repost"
                await send_telegram(chat_id, "🔗 Отправьте прямую ссылку на ваш пост.")
            elif data_str.startswith("paycred_"):
                parts = data_str.split("_")
                admin_add_balance(int(parts[1]), int(parts[2]))
                await send_telegram(int(parts[1]), f"✅ Платеж подтвержден! Начислено {parts[2]} запросов.")
                await http_edit_message_text(chat_id, message_id, "✅ Чек одобрен.")
            elif data_str.startswith("payunl_"):
                parts = data_str.split("_")
                admin_set_unlimited(int(parts[1]), 10)
                await send_telegram(int(parts[1]), f"✅ Платеж подтвержден! Активирован безлимит.")
                await http_edit_message_text(chat_id, message_id, "✅ Чек одобрен.")
            elif data_str.startswith("like_"):
                vid = data_str[5:]
                vac = temp_vacancies.get(vid, {"title": "Позиция"})
                like_vacancy(chat_id, vid, vac["title"])
                await send_telegram(chat_id, f"📌 Вакансия «{vac['title']}» успешно добавлена в Трекер откликов!")
            elif data_str.startswith("gen_"):
                if not spend_balance(chat_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
                    return
                bg(run_ai_generation(chat_id, dict(temp_vacancies.get(data_str[4:], {}))))
            elif data_str.startswith("match_"):
                if not spend_balance(chat_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
                    return
                bg(run_vacancy_match(chat_id, dict(temp_vacancies.get(data_str[6:], {}))))
            elif data_str.startswith("act_"):
                bg(activate_resume(chat_id, data_str[4:]))
            elif data_str.startswith("adaptsel_"):
                user_adapt_target[chat_id] = int(data_str[9:])
                user_states[chat_id] = "waiting_for_adaptation_vacancy"
                bg(http_edit_message_text(chat_id, message_id, "✅ Резюме выбрано!\n\n💡 *Шаг 2:* Теперь отправьте текст или прямую ссылку на вакансию с hh.ru."))
            elif data_str.startswith("hide_"):
                hide_vacancy(chat_id, data_str[5:])
                bg(http_edit_message_text(chat_id, message_id, "🗑 Вакансия скрыта из выдачи."))

    return web.Response(text="OK")


# ---------------- Запуск ----------------
async def main():
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        webhook_url = f"{render_url}/{BOT_TOKEN}"
        async with HTTP.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}") as resp:
            log.info("setWebhook: %s", (await resp.text())[:200])

    log.info("🚀 Bot v1.5 with Full Features & Fast Search started successfully.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.main() if hasattr(asyncio, "main") else asyncio.run(main())
