import asyncio
import html
import json
import logging
import os
import re
import sqlite3

import aiohttp
import requests
from aiohttp import web
from google import genai
from google.genai import types as gtypes
from pypdf import PdfReader
from docx import Document

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("🔴 BOT_TOKEN не задан!")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
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
OPENROUTER_MODELS = [
    "openai/gpt-oss-20b:free",
    "google/gemma-4-26b-a4b:free",
    "openrouter/free",
]
GROQ_MODEL = "llama-3.1-8b-instant"

_working_model = {"name": None}
HTTP: aiohttp.ClientSession = None
TASKS = set()
temp_vacancies = {}

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT);
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
""")
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


def hide_vacancy(user_id: int, vacancy_id: str):
    cur.execute("INSERT OR IGNORE INTO hidden_vacancies (user_id, vacancy_id) VALUES (?, ?)", (user_id, vacancy_id))
    conn.commit()


def is_vacancy_hidden(user_id: int, vacancy_id: str) -> bool:
    cur.execute("SELECT 1 FROM hidden_vacancies WHERE user_id=? AND vacancy_id=?", (user_id, vacancy_id))
    return cur.fetchone() is not None


# ---------------- ИИ-слой с фолбэками ----------------
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
    """Возвращает текст или None. Вызывающий ОБЯЗАН проверить результат."""
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
    if OPENROUTER_KEY:
        for m in OPENROUTER_MODELS:
            try:
                log.info("AI ok: openrouter/%s", m)
                return _openai_compat(prompt, "https://openrouter.ai/api/v1", OPENROUTER_KEY, m)
            except Exception as e:
                log.warning("OpenRouter %s failed: %s", m, str(e)[:150])
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


async def send_telegram(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    payload = {"chat_id": chat_id, "text": text[:4096]}
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


async def http_edit_message_text(chat_id, message_id, text):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
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
        [{"text": "💎 Оплата и Баланс"}, {"text": "ℹ️ Помощь"}],
    ]
    if is_admin:
        kb.append([{"text": "👑 Админ-панель"}])
    return {"keyboard": kb, "resize_keyboard": True}


# ---------------- hh.ru: официальный API + скрейпинг в запасе ----------------
async def hh_api_search(query: str):
    try:
        async with HTTP.get("https://api.hh.ru/vacancies",
                            params={"text": query, "area": "1", "per_page": "20"},
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
        for it in items[:20]:
            vid = it.get("vacancyId") or it.get("id")
            out.append({"id": vid, "name": it.get("name"),
                        "company": (it.get("company") or {}).get("name"),
                        "url": f"https://hh.ru/vacancy/{vid}"})
        return out or None
    except Exception as e:
        log.warning("hh scrape failed: %s", str(e)[:150])
        return None


# ---------------- Фичи ----------------
async def handle_search(chat_id: int, is_admin: bool):
    await send_telegram(chat_id, "🔍 Анализирую резюме и подбираю вакансии...")
    resume = get_active_resume(chat_id)
    
    prompt = (f"Проанализируй текст резюме и напиши только короткое название должности для поиска на hh.ru (2-4 слова, без кавычек). "
              f"Пример ответа: Руководитель проектов\n\nРезюме: {resume[:2000]}")
    
    query = await asyncio.to_thread(ai_generate, prompt)
    if not query or len(query.strip()) > 50:
        await send_telegram(chat_id, "⚠️ Не удалось автоматически подобрать запрос. Напиши, какую должность ты ищешь?", get_keyboard(is_admin))
        return
    query = query.strip().strip('"').strip()

    items = await hh_api_search(query) or await hh_scrape_search(query)
    if not items:
        await send_telegram(chat_id, f"⚠️ Не удалось найти вакансии по запросу «{query}». Попробуй написать должность вручную.", get_keyboard(is_admin))
        return

    # Фильтруем скрытые вакансии (мусор)
    filtered_items = [v for v in items if not is_vacancy_hidden(chat_id, str(v["id"]))]

    if not filtered_items:
        await send_telegram(chat_id, f"⚠️ Все найденные вакансии по запросу «{query}» находятся в вашем черном списке.", get_keyboard(is_admin))
        return

    await send_telegram(chat_id, f"🔥 Нашел позиций по запросу «{query}» (доступно: {len(filtered_items)}):",
                        get_keyboard(is_admin))
    for v in filtered_items[:15]:
        vid = str(v["id"])
        name = v.get("name") or "Вакансия"
        comp = v.get("company") or "Компания"
        temp_vacancies[vid] = {"title": name, "employer": comp}
        
        # Кнопки под вакансией: Сопроводительное, Соответствие, Мусор
        markup = {"inline_keyboard": [
            [
                {"text": "✍️ Сопроводительное", "callback_data": f"gen_{vid}"},
                {"text": "📊 Соответствие", "callback_data": f"match_{vid}"}
            ],
            [
                {"text": "🗑 Мусор", "callback_data": f"hide_{vid}"}
            ]
        ]}
        await send_telegram(chat_id, f"🏢 *{comp}*\n💼 [{name}]({v.get('url')})", markup)
        await asyncio.sleep(0.2)


async def run_ai_generation(chat_id: int, vac_info: dict):
    await send_telegram(chat_id,
                        f"✍️ Готовлю сопроводительное письмо для *{vac_info['employer']}* "
                        f"на позицию «{vac_info['title']}»...")
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
        f"а также укажи ключевые пробелы (что нужно исправить или добавить в резюме):\n\n"
        f"Резюме:\n{resume}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен, не удалось провести анализ.")
        return
    await send_telegram(chat_id, f"📊 *Анализ соответствия вакансии:*\n\n{analysis}")


async def handle_ai(chat_id: int, is_admin: bool, prompt: str):
    await send_telegram(chat_id, "⏳ Думаю над ответом...")
    answer = await asyncio.to_thread(ai_generate, prompt)
    if not answer:
        await send_telegram(chat_id, "⚠️ ИИ недоступен, попробуй позже.", get_keyboard(is_admin))
    else:
        await send_telegram(chat_id, answer, get_keyboard(is_admin))


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
        await send_telegram(chat_id, "⚠️ Не извлёк текст. Поддерживаю PDF, DOCX, RTF, TXT.", get_keyboard(is_admin))
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
    await send_telegram(chat_id, "✅ Резюме сделано активным.",
                        get_keyboard(ADMIN_ID != 0 and chat_id == ADMIN_ID))


# ---------------- Обработка сообщений ----------------
async def process_message(msg: dict):
    chat_id = msg["chat"]["id"]
    username = msg["chat"].get("username", "")
    text = (msg.get("text") or "").strip()
    document = msg.get("document")

    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (chat_id, username))
    conn.commit()
    is_admin = ADMIN_ID != 0 and chat_id == ADMIN_ID

    if document:
        await handle_document(chat_id, document, is_admin)
        return
    if not text:
        return

    if text.startswith("/start"):
        await send_telegram(chat_id, "👋 Привет! Твой карьерный агент готов к работе.", get_keyboard(is_admin))

    elif text == "ℹ️ Помощь":
        help_text = (
            "ℹ️ *Справка по функционалу бота:*\n\n"
            "📁 *Мои резюме / Загрузить резюме* — загружайте свои резюме в форматах PDF, DOCX, RTF или TXT, переключайте активные версии.\n"
            "🔍 *Поиск вакансий* — ИИ автоматически подбирает релевантные вакансии с hh.ru под ваше активное резюме.\n"
            "  • *Сопроводительное* — генерация персонального письма под конкретную вакансию.\n"
            "  • *Соответствие* — проверка, насколько ваше резюме подходит к вакансии, и советы по доработке.\n"
            "  • *Мусор* — скрывает неинтересные вакансии из выдачи.\n"
            "🛠 *Адаптация резюме* — подстраивает текст вашего резюме под желаемую роль.\n"
            "📊 *Анализ навыков (Skill Gap)* — находит пробелы в скиллах для выбранной позиции.\n"
            "📋 *Аудит резюме* — жесткая критика и профессиональные рекомендации.\n"
            "🎤 *Тренажер собеседований* — симуляция каверзных вопросов на интервью.\n"
            "📌 *Трекер откликов* — учет отправленных заявок.\n"
            "💎 *Оплата и Баланс* — пополнение лимита запросов."
        )
        await send_telegram(chat_id, help_text, get_keyboard(is_admin))

    elif text == "💎 Оплата и Баланс":
        balance_text = (
            "💎 *Оплата и Баланс запросов*\n\n"
            "В вашем личном кабинете расходуются ИИ-запросы для генерации писем, аудита и поиска.\n\n"
            "💳 *Как пополнить баланс / докупить запросы:*\n"
            "1. **Telegram Stars (⭐):** Самый быстрый способ оплаты внутри мессенджера (нажмите кнопку пополнения ниже, если доступно).\n"
            "2. **Перевод с карты / СБП:** Прямой перевод средств. Для пополнения свяжитесь с администратором: "
            f"{f'@{ADMIN_ID}' if ADMIN_ID else 'администратором сервиса'}, указав свой ID (`{chat_id}`).\n\n"
            "После подтверждения перевода баланс запросов будет мгновенно зачислен!"
        )
        await send_telegram(chat_id, balance_text, get_keyboard(is_admin))

    elif text in ("👑 Админ-панель", "/admin"):
        if not is_admin:
            await send_telegram(chat_id, "⛔ Нет доступа.")
            return
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM resumes")
        resumes = cur.fetchone()[0]
        await send_telegram(chat_id, f"👑 *Админ-панель*\n\n👥 Пользователей: `{total}`\n📁 Резюме: `{resumes}`",
                            get_keyboard(is_admin))

    elif text == "📁 Мои резюме":
        rows = list_resumes(chat_id)
        if not rows:
            await send_telegram(chat_id, "⚠️ Нет загруженных резюме.", get_keyboard(is_admin))
        else:
            kb = {"inline_keyboard": [[{"text": f"{'✅' if r['active'] else '📄'} {r['name']}",
                                        "callback_data": f"act_{r['id']}"}] for r in rows]}
            await send_telegram(chat_id, "📁 *Твои резюме:* (нажми, чтобы сделать активным)", kb)

    elif text == "📥 Загрузить резюме":
        await send_telegram(chat_id, "📄 Отправь файл резюме (PDF, DOCX, RTF или TXT) в чат.", get_keyboard(is_admin))

    elif text == "🔍 Поиск вакансий":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "⚠️ Сначала загрузи резюме!", get_keyboard(is_admin))
        else:
            bg(handle_search(chat_id, is_admin))

    elif text == "📌 Трекер откликов":
        await send_telegram(chat_id, "📌 Твои отклики пока пусты.", get_keyboard(is_admin))

    else:
        resume = get_active_resume(chat_id)
        if not resume and ("Адаптация" in text or "Анализ навыков" in text or "Аудит" in text):
            await send_telegram(chat_id, "⚠️ Сначала загрузи резюме!", get_keyboard(is_admin))
            return
        if "Адаптация" in text:
            prompt = f"Адаптируй это резюме под позицию руководителя проектов:\n\n{resume}"
        elif "Анализ навыков" in text:
            prompt = f"Проведи Skill Gap анализ для руководителя проектов:\n\n{resume}"
        elif "Аудит" in text:
            prompt = f"Сделай жесткий аудит и дай рекомендации по резюме:\n\n{resume}"
        elif "Тренажер" in text:
            prompt = "Ты жесткий интервьюер. Задай мне первый каверзный вопрос для руководителя проектов."
        else:
            prompt = (f"Ты карьерный консультант. Контекст резюме пользователя:\n{resume[:8000]}\n\n"
                      f"Вопрос пользователя: {text}")
        bg(handle_ai(chat_id, is_admin, prompt))


# ---------------- Вебхук ----------------
async def telegram_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="OK")

    if is_duplicate(data.get("update_id")):
        return web.Response(text="OK")

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        message_id = (cb.get("message") or {}).get("message_id")
        data_str = cb.get("data", "") or ""
        bg(answer_callback(cb.get("id", "")))
        if chat_id:
            if data_str.startswith("gen_"):
                v = temp_vacancies.get(data_str[4:], {"title": "Вакансия", "employer": "Компания"})
                bg(run_ai_generation(chat_id, dict(v)))
            elif data_str.startswith("match_"):
                v = temp_vacancies.get(data_str[6:], {"title": "Вакансия", "employer": "Компания"})
                bg(run_vacancy_match(chat_id, dict(v)))
            elif data_str.startswith("act_"):
                bg(activate_resume(chat_id, data_str[4:]))
            elif data_str.startswith("hide_"):
                vid = data_str[5:]
                hide_vacancy(chat_id, vid)
                bg(http_edit_message_text(chat_id, message_id, "🗑 Вакансия помечена как мусор и больше не будет показываться."))
        return web.Response(text="OK")

    if "message" in data:
        bg(process_message(data["message"]))

    return web.Response(text="OK")


# ---------------- Старт ----------------
def log_startup():
    providers = []
    if GEMINI_API_KEY:
        providers.append("Gemini")
    if OPENROUTER_KEY:
        providers.append("OpenRouter")
    if GROQ_KEY:
        providers.append("Groq")
    if providers:
        log.info("🟢 ИИ-провайдеры активны: %s (%d из 3)", ", ".join(providers), len(providers))
    else:
        log.error("🔴 ВНИМАНИЕ: ни один ИИ-провайдер не настроен!")
    log.info("📋 GEMINI_MODELS=%s", GEMINI_MODEL_CANDIDATES)
    log.info("👑 ADMIN_ID=%s | 🌐 PORT=%s", ADMIN_ID, PORT)


async def main():
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    log_startup()

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
    else:
        log.warning("RENDER_EXTERNAL_URL не задан — webhook не установлен.")

    log.info("🚀 Bot started on aiohttp webhook.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
