import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import html
import aiohttp
import requests
from aiohttp import web
from docx import Document
from google import genai
from google.genai import types as gtypes
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("🔴 BOT_TOKEN не задан!")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")
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

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, 
    username TEXT,
    balance INTEGER DEFAULT 30,
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
    title TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    initial_balance = 30
    cur.execute(
        "INSERT INTO users (user_id, username, balance, referred_by) VALUES (?, ?, ?, ?)",
        (user_id, username, initial_balance, referrer_id)
    )
    conn.commit()
    if referrer_id:
        cur.execute("UPDATE users SET balance = balance + 30 WHERE user_id=?", (referrer_id,))
        conn.commit()
    return True


def get_user_data(user_id: int):
    cur.execute("SELECT balance, unlimited_until, daily_count, last_active_date FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return {"balance": 30, "unlimited_until": None, "daily_count": 0, "last_active_date": ""}
    return {"balance": row[0], "unlimited_until": row[1], "daily_count": row[2], "last_active_date": row[3]}


def spend_balance(user_id: int, cost: int = 1) -> bool:
    # У администратора нет лимитов
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        return True
        
    data = get_user_data(user_id)
    today = str(asyncio.get_event_loop().time())[:10] # упрощенная дата для проверки дня
    
    # Проверяем безлимит по подписке (до 50 запросов в день)
    unlimited_until = data["unlimited_until"]
    if unlimited_until:
        # Простая проверка активности безлимита (можно расширить по датам, здесь держим структуру)
        cur.execute("SELECT datetime('now') < datetime(?)", (unlimited_until,))
        is_active_sub = cur.fetchone()[0]
        if is_active_sub:
            # Проверяем дневной лимит в 50 запросов
            last_date = data["last_active_date"]
            daily_count = data["daily_count"]
            import datetime
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
                return False # Превышен дневной лимит безлимита (50 запросов)

    # Обычное списание запросов с баланса
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
    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title) VALUES (?, ?, ?)", (user_id, vacancy_id, title))
    conn.commit()


def get_user_preferences(user_id: int) -> str:
    cur.execute("SELECT title FROM liked_vacancies WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,))
    rows = cur.fetchall()
    if not rows:
        return "Нет истории предпочтений."
    return ", ".join([r[0] for r in rows])


# ---------------- ИИ-слой ----------------
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
                log.warning("Gemini %s failed: %s", m, str(e)[:150])
                
    if GROQ_KEY:
        try:
            log.info("AI ok: groq/%s", GROQ_MODEL)
            return _openai_compat(prompt, "https://api.groq.com/openai/v1", GROQ_KEY, GROQ_MODEL)
        except Exception as e:
            log.warning("Groq failed: %s", str(e)[:150])
            
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
            reader = PdfReader(path)
            pages_text = []
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
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
    t.add_done_callback(TASKS.discard)
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
        await send_telegram(chat_id, p, markup, parse_mode)
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
        [{"text": "👥 Пригласить друга"}, {"text": "💎 Оплата и Баланс"}],
        [{"text": "ℹ️ Помощь"}],
    ]
    if is_admin:
        kb.append([{"text": "👑 Админ-панель"}])
    return {"keyboard": kb, "resize_keyboard": True}


# ---------------- hh.ru поиск ----------------
async def hh_api_search(query: str):
    try:
        async with HTTP.get("https://api.hh.ru/vacancies",
                            params={"text": query, "area": "1", "per_page": "50"},
                            headers={"User-Agent": "LemusCareerBot/1.0 (career-bot)",
                                     "HH-User-Agent": "LemusCareerBot/1.0"}) as resp:
            data = await resp.json()
        items = []
        for i in data.get("items", []):
            items.append({"id": i.get("id"), "name": i.get("name"),
                          "company": (i.get("employer") or {}).get("name"),
                          "url": i.get("alternate_url") or f"https://hh.ru/vacancy/{i.get('id')}"})
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
            out.append({"id": vid, "name": it.get("name"),
                        "company": (it.get("company") or {}).get("name"),
                        "url": f"https://hh.ru/vacancy/{vid}"})
        return out or None
    except Exception as e:
        log.warning("hh scrape failed: %s", str(e)[:150])
        return None


# ---------------- Фичи бота ----------------
async def handle_search(chat_id: int, is_admin: bool):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ У вас закончились запросы! Пополните баланс через меню «💎 Оплата и Баланс» или пригласите друзей.", get_keyboard(is_admin))
        return

    await send_telegram(chat_id, "🔍 Ищу вакансии по всему рынку (с приоритетом для топ-компаний)...")
    
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

    await send_telegram(chat_id, f"🔥 Нашел вакансии (вверху списка — предложения от топ-компаний, доступно: {len(filtered_list)}):", get_keyboard(is_admin))
    
    for v in filtered_list[:15]:
        vid = str(v["id"])
        name = v.get("name") or "Вакансия"
        comp = v.get("company") or "Компания"
        temp_vacancies[vid] = {"title": name, "employer": comp}
        
        comp_lower = comp.lower()
        is_top = any(tc in comp_lower for tc in top_companies)
        badge = "⭐ *[ТОП-КОМПАНИЯ]*\n" if is_top else ""
        
        markup = {"inline_keyboard": [
            [
                {"text": "👍 Подходит", "callback_data": f"like_{vid}"},
                {"text": "✍️ Сопроводительное", "callback_data": f"gen_{vid}"}
            ],
            [
                {"text": "📊 Соответствие", "callback_data": f"match_{vid}"},
                {"text": "🗑 Мусор", "callback_data": f"hide_{vid}"}
            ]
        ]}
        await send_telegram(chat_id, f"{badge}🏢 *{comp}*\n💼 [{name}]({v.get('url')})", markup)
        await asyncio.sleep(0.2)


async def run_skill_gap_analysis(chat_id: int):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов для анализа навыков!")
        return

    await send_telegram(chat_id, "📊 *Провожу анализ навыков (Skill Gap) и формирую матрицу компетенций...*")
    resume = get_active_resume(chat_id)
    if not resume:
        await send_telegram(chat_id, "⚠️ Сначала загрузите резюме!")
        return

    prompt = (
        "Ты — экспертный карьерный консультант уровня C-level. Проведи глубокий анализ навыков (Skill Gap Analysis) для этого кандидата, "
        "претендующего на позиции Head of / Директор по развитию / Руководитель направления.\n"
        "Выдели:\n"
        "1. Сильные управленческие и технические компетенции (что уже отлично развито).\n"
        "2. Зоны роста и пробелы (каких навыков, методологий или метрик может не хватать для топовых позиций в крупных экосистемах вроде Сбера или Яндекса).\n"
        "3. Рекомендации по развитию на ближайшие 3–6 месяцев.\n\n"
        f"Резюме кандидата:\n{resume[:8000]}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен, не удалось провести анализ навыков.")
        return
    await send_telegram(chat_id, f"📊 *Анализ навыков (Skill Gap):*\n\n{analysis}")


async def run_ai_generation(chat_id: int, vac_info: dict):
    await send_telegram(chat_id, f"✍️ Готовлю сопроводительное письмо для *{vac_info['employer']}* на позицию «{vac_info['title']}»...")
    resume = get_active_resume(chat_id) or "Опыт не указан."
    letter = await asyncio.to_thread(ai_generate,
        f"Напиши профессиональное сопроводительное письмо на позицию '{vac_info['title']}' "
        f"в '{vac_info['employer']}' на основе резюме:\n\n{resume}")
    if not letter:
        await send_telegram(chat_id, "⚠️ ИИ недоступен, письмо не получилось.")
        return
    await send_telegram(chat_id, f"📝 *Сопроводительное письмо:*\n\n{letter}")


async def run_vacancy_match(chat_id: int, vac_info: dict):
    await send_telegram(chat_id, f"📊 Анализирую соответствие вашего резюме вакансии *{vac_info['title']}* в компании *{vac_info['employer']}*...")
    resume = get_active_resume(chat_id) or "Резюме не найдено."
    prompt = (
        f"Проанализируй, насколько резюме кандидата подходит под вакансию '{vac_info['title']}' в компанию '{vac_info['employer']}'. "
        f"Дай оценку соответствия в процентах, перечисли сильные стороны кандидата для этой роли, "
        f"а также укажи ключевые пробелы (что нужно исправить или добавить в резюме):\n\nРезюме:\n{resume}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен, не удалось провести анализ.")
        return
    await send_telegram(chat_id, f"📊 *Анализ соответствия вакансии:*\n\n{analysis}")


async def run_resume_adaptation(chat_id: int, resume_id: int, vacancy_text: str):
    await send_telegram(chat_id, "🛠 *Адаптирую резюме под специфику вакансии и формирую чистый файл...*")
    resume_text = get_resume_by_id(chat_id, resume_id) or get_active_resume(chat_id)
    
    if not resume_text:
        await send_telegram(chat_id, "⚠️ Резюме не найдено! Загрузите файл резюме.")
        return

    prompt_resume = (
        "Ты — элитный карьерный стратег. Перепиши и оптимизируй резюме кандидата строго под требования вакансии. "
        "Сохрани правдивость фактов (компании, даты), но полностью репозиционируй опыт так, чтобы он закрывал требования вакансии.\n"
        "ВАЖНО: Выдай ТОЛЬКО текст резюме, начиная сразу с ФИО. Никаких вступительных фраз вроде 'Вот ваше резюме' или 'Для того чтобы ваше резюме...'.\n"
        "Сформируй резюме строго по структуре:\n"
        "1. ФИО и контакты\n"
        "2. Summary\n"
        "3. Ключевые навыки\n"
        "4. Опыт работы\n"
        "5. Образование\n\n"
        f"--- ТРЕБОВАНИЯ ВАКАНСИИ ---\n{vacancy_text[:3000]}\n\n"
        f"--- ИСХОДНОЕ РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:6000]}"
    )
    adapted_text = await asyncio.to_thread(ai_generate, prompt_resume)

    prompt_letter = (
        "Напиши короткое, емкое и профессиональное сопроводительное письмо к этой вакансии от лица кандидата. "
        "Письмо должно быть выдержано в деловом стиле C-level, подчеркивать релевантный управленческий опыт и достижения, "
        "без воды и штампов (до 1000 знаков).\n\n"
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
        await send_telegram(chat_id, f"📝 *Готовое сопроводительное письмо для работодателя:*\n\n{cover_letter}")

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
            chat_id, 
            file_bytes, 
            filename="Adapted_Resume.docx", 
            caption="📄 *Ваше адаптированное резюме готово к отправке работодателю!*",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    except Exception as e:
        log.error("DOCX generation failed: %s", e)
        await send_telegram(chat_id, "⚠️ Ошибка формирования файла.")


async def run_resume_audit(chat_id: int):
    if not spend_balance(chat_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return

    await send_telegram(chat_id, "📋 *Провожу C-Level аудит и формирую элитный Word-файл...*")
    resume = get_active_resume(chat_id)
    if not resume:
        await send_telegram(chat_id, "⚠️ Сначала загрузите резюме!")
        return

    audit_prompt = f"Сделай жесткий, глубокий аудит этого резюме с позиции C-level:\n\n{resume[:8000]}"
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
        await send_document_bytes(chat_id, stream.getvalue(), "C_Level_Resume_Pro.docx", "✅ Ваше элитное резюме (Word).")
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
        await send_telegram(chat_id, "⚠️ Не извлёк текст.", get_keyboard(is_admin))
        return
    add_resume(chat_id, file_name, text_content)
    await send_telegram(chat_id, f"✅ Резюме «{file_name}» успешно распознано и назначено активным!", get_keyboard(is_admin))


async def activate_resume(chat_id: int, rid: str):
    try:
        rid = int(rid)
    except ValueError:
        return
    cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (chat_id,))
    cur.execute("UPDATE resumes SET active=1 WHERE id=? AND user_id=?", (rid, chat_id))
    conn.commit()
    await send_telegram(chat_id, "✅ Резюме сделано активным.", get_keyboard(ADMIN_ID != 0 and chat_id == ADMIN_ID))


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

    if photo and user_states.get(chat_id) == "waiting_for_receipt":
        user_states.pop(chat_id, None)
        await send_telegram(chat_id, "✅ Чек отправлен администратору на проверку. Ожидайте зачисления!")
        
        # Кнопки в админке для начисления пакета или безлимита
        admin_markup = {
            "inline_keyboard": [
                [{"text": "✅ +50 запросов", "callback_data": f"paycred_{chat_id}_50"}],
                [{"text": "⭐ Безлимит 10 дней", "callback_data": f"payunl_{chat_id}"}]
            ]
        }
        await send_telegram(
            ADMIN_ID, 
            f"📸 *Новый чек на проверку!*\nОт: @{username or 'нет_юзера'} (ID: `{chat_id}`)",
            reply_markup=admin_markup
        )
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

    if text.startswith("/start"):
        await send_telegram(chat_id, "👋 Привет! Твой карьерный агент готов к работе.\n\n🎁 Вам начислено *30 приветственных запросов*!", get_keyboard(is_admin))

    elif text == "👥 Пригласить друга":
        bot_info = await HTTP.get(f"{TELEGRAM_API}/getMe")
        bot_data = await bot_info.json()
        bot_username = bot_data.get("result", {}).get("username", "bot")
        ref_link = f"https://t.me/{bot_username}?start={chat_id}"
        await send_telegram(chat_id, f"👥 *Реферальная программа*\nПриглашайте друзей и получайте по 30 запросов!\n\n🔗 Ссылка:\n`{ref_link}`", get_keyboard(is_admin))

    elif text == "💎 Оплата и Баланс":
        data = get_user_data(chat_id)
        balance = data["balance"]
        unl = data["unlimited_until"]
        status_str = f"📊 Баланс: `{balance} запросов`"
        if unl:
            status_str = f"⭐ Активен безлимит (до 50 запросов в день) до: `{unl}`"

        balance_text = (
            f"💎 *Оплата и Баланс*\n\n"
            f"{status_str}\n\n"
            "💳 *Тарифы и способы пополнения:*\n"
            "1️⃣ **Пакет «50 запросов»:** 100 ⭐ (Telegram Stars) ИЛИ 200 руб.\n"
            "2️⃣ **Пакет «Безлимит на 10 дней»** (до 50 запросов в сутки): 500 ⭐ ИЛИ 500 руб.\n\n"
            "🏦 **Реквизиты для перевода (СБП / Карта):**\n"
            "`2202208459089018`\n"
            "_После перевода нажмите кнопку ниже и отправьте скриншот чека в чат._"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "⭐ Оплатить 50 запросов (100 Звезд)", "callback_data": "buy_pack_stars"}],
                [{"text": "⭐ Безлимит 10 дней (500 Звезд)", "callback_data": "buy_unl_stars"}],
                [{"text": "📄 Отправить чек об оплате (Карта/СБП)", "callback_data": "send_receipt"}]
            ]
        }
        await send_telegram(chat_id, balance_text, kb)

    elif text == "🛠 Адаптация резюме":
        rows = list_resumes(chat_id)
        if not rows:
            await send_telegram(chat_id, "⚠️ Сначала загрузите резюме!", get_keyboard(is_admin))
            return
        kb = {"inline_keyboard": [[{"text": f"📄 {r['name']}", "callback_data": f"adaptsel_{r['id']}"}] for r in rows]}
        await send_telegram(chat_id, "🛠 *Выберите резюме* для адаптации, генерации файла и сопроводительного письма:", kb)

    elif text == "📋 Аудит резюме":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "⚠️ Сначала загрузите резюме!", get_keyboard(is_admin))
            return
        bg(run_resume_audit(chat_id))

    elif text == "📊 Анализ навыков (Skill Gap)":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "⚠️ Сначала загрузите резюме!", get_keyboard(is_admin))
            return
        bg(run_skill_gap_analysis(chat_id))

    elif text == "ℹ️ Помощь":
        help_text = (
            "ℹ️ *Справка по возможностям карьерного бота:*\n\n"
            "• 🔍 *Поиск вакансий* — интеллектуальный поиск по всему рынку с приоритетным поднятием предложений от топ-компаний (Сбер, Яндекс, МТС, Т-Банк, ВТБ, Альфа и др.) с фильтрацией линейного мусора.\n"
            "• 🛠 *Адаптация резюме* — переработка вашего резюме под конкретные требования выбранной вакансии с мгновенной генерацией чистого файла `.docx`.\n"
            "• 📊 *Анализ навыков (Skill Gap)* — глубокая оценка сильных сторон, зон роста и матрица компетенций под управленческие роли C-level.\n"
            "• 📋 *Аудит резюме* — жесткий управленческий разбор профиля и формирование элитного Word-документа.\n"
            "• 👍 *Обучение на лайках* — бот запоминает выбранные вами вакансии и настраивает выдачу под ваши предпочтения.\n"
            "• 💎 *Оплата и Баланс* — гибкие тарифы (пакеты запросов или безлимит на 10 дней) через Telegram Stars или перевод по карте."
        )
        await send_telegram(chat_id, help_text, get_keyboard(is_admin))

    elif text in ("👑 Админ-панель", "/admin"):
        if not is_admin:
            await send_telegram(chat_id, "⛔ Нет доступа.")
            return
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM resumes")
        total_resumes = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE created_at >= date('now')")
        new_today = cur.fetchone()[0]
        
        admin_panel_text = (
            f"👑 *Админ-панель*\n\n"
            f"👥 Всего пользователей: `{total_users}` (новых сегодня: `{new_today}`)\n"
            f"📁 Всего резюме: `{total_resumes}`\n\n"
            "⚙️ *Команды управления:*\n"
            "`/add_credits <user_id> <кол-во>`"
        )
        await send_telegram(chat_id, admin_panel_text, get_keyboard(is_admin))

    elif text == "📁 Мои резюме":
        rows = list_resumes(chat_id)
        if not rows:
            await send_telegram(chat_id, "⚠️ Нет загруженных резюме.", get_keyboard(is_admin))
        else:
            kb = {"inline_keyboard": [[{"text": f"{'✅' if r['active'] else '📄'} {r['name']}", "callback_data": f"act_{r['id']}"}] for r in rows]}
            await send_telegram(chat_id, "📁 *Твои резюме:*", kb)

    elif text == "📥 Загрузить резюме":
        await send_telegram(chat_id, "📄 Отправь файл резюме (PDF, DOCX, RTF или TXT) в чат.", get_keyboard(is_admin))

    elif text == "🔍 Поиск вакансий":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "⚠️ Сначала загрузи резюме!", get_keyboard(is_admin))
        else:
            bg(handle_search(chat_id, is_admin))

    else:
        await send_telegram(chat_id, "ℹ️ Воспользуйтесь кнопками меню.", get_keyboard(is_admin))


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
                await send_telegram(uid, "🎉 Оплата прошла успешно! Вам активирован безлимит на 10 дней.")
            else:
                admin_add_balance(uid, 50)
                await send_telegram(uid, "🎉 Оплата прошла успешно! Начислено 50 запросов.")
        else:
            bg(process_message(msg))

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        message_id = (cb.get("message") or {}).get("message_id")
        data_str = cb.get("data", "") or ""
        bg(answer_callback(cb.get("id", "")))
        
        if chat_id:
            if data_str == "buy_pack_stars":
                bg(send_stars_invoice(chat_id, 100, "Пакет 50 запросов", "credits_50"))
            elif data_str == "buy_unl_stars":
                bg(send_stars_invoice(chat_id, 500, "Безлимит на 10 дней", "unl_10d"))
            elif data_str == "send_receipt":
                user_states[chat_id] = "waiting_for_receipt"
                await send_telegram(chat_id, "📸 Пожалуйста, отправьте **фотографию или скриншот чека** прямо в этот чат.")
            elif data_str.startswith("paycred_"):
                parts = data_str.split("_")
                target_uid = int(parts[1])
                amt = int(parts[2])
                admin_add_balance(target_uid, amt)
                await send_telegram(target_uid, f"✅ Администратор подтвердил ваш платеж! Начислено {amt} запросов.")
                await http_edit_message_text(chat_id, message_id, "✅ Чек одобрен, запросы начислены.")
            elif data_str.startswith("payunl_"):
                parts = data_str.split("_")
                target_uid = int(parts[1])
                admin_set_unlimited(target_uid, 10)
                await send_telegram(target_uid, "✅ Администратор подтвердил ваш платеж! Активирован безлимит на 10 дней.")
                await http_edit_message_text(chat_id, message_id, "✅ Чек одобрен, безлимит активирован.")
            elif data_str.startswith("like_"):
                vid = data_str[5:]
                vac = temp_vacancies.get(vid, {"title": "Позиция"})
                like_vacancy(chat_id, vid, vac["title"])
                await send_telegram(chat_id, f"👍 Запомнил вакансию «{vac['title']}». Бот будет подбирать похожие!")
            elif data_str.startswith("gen_"):
                if not spend_balance(chat_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов или исчерпан лимит на сегодня!")
                    return
                v = temp_vacancies.get(data_str[4:], {"title": "Вакансия", "employer": "Компания"})
                bg(run_ai_generation(chat_id, dict(v)))
            elif data_str.startswith("match_"):
                if not spend_balance(chat_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов или исчерпан лимит на сегодня!")
                    return
                v = temp_vacancies.get(data_str[6:], {"title": "Вакансия", "employer": "Компания"})
                bg(run_vacancy_match(chat_id, dict(v)))
            elif data_str.startswith("act_"):
                bg(activate_resume(chat_id, data_str[4:]))
            elif data_str.startswith("adaptsel_"):
                rid = data_str[9:]
                user_adapt_target[chat_id] = int(rid)
                user_states[chat_id] = "waiting_for_adaptation_vacancy"
                bg(http_edit_message_text(chat_id, message_id, "✅ Резюме выбрано!\n\nТеперь **скопируйте и вставьте текст вакансии** в ответное сообщение."))
            elif data_str.startswith("hide_"):
                vid = data_str[5:]
                hide_vacancy(chat_id, vid)
                bg(http_edit_message_text(chat_id, message_id, "🗑 Вакансия скрыта."))

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

    log.info("🚀 Bot started successfully.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.main() if hasattr(asyncio, "main") else asyncio.run(main())
