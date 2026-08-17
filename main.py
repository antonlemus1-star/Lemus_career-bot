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
from pypdf import PdfReader
from docx import Document

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    log.error("🔴 BOT_TOKEN не задан в переменных окружения!")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_MODEL_CANDIDATES = list(dict.fromkeys([
    os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
    "gemini-3.5-flash",
    "gemini-flash-latest",
]))
GROQ_MODEL = "llama-3.1-8b-instant"
OPENROUTER_MODELS = [
    "openai/gpt-oss-20b:free",
    "google/gemma-4-26b-a4b:free",
    "openrouter/free",
]

_working_model = {"name": None}
HTTP: aiohttp.ClientSession = None
TASKS = set()
temp_vacancies = {}
ignored_vacancies = set()

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 5);
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    text TEXT,
    active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


def get_user_balance(user_id: int) -> int:
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row else 5


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
    # 1. Резерв №1: Groq (сверхбыстрый и стабильный)
    if GROQ_KEY:
        try:
            res = _openai_compat(prompt, "https://api.groq.com/openai/v1", GROQ_KEY, GROQ_MODEL)
            if res:
                log.info("AI ok: groq/%s", GROQ_MODEL)
                return res
        except Exception as e:
            log.warning("Groq failed: %s", str(e)[:150])

    # 2. Основной: Gemini (чистый вызов без функций)
    if client:
        cands = [_working_model["name"]] if _working_model["name"] else GEMINI_MODEL_CANDIDATES
        for m in cands:
            try:
                resp = client.models.generate_content(model=m, contents=prompt)
                if resp is not None and resp.text:
                    _working_model["name"] = m
                    log.info("AI ok: gemini/%s", m)
                    return resp.text
            except Exception as e:
                log.warning("Gemini %s failed: %s", m, str(e)[:150])
    
    # 3. Резерв №2: OpenRouter
    if OPENROUTER_KEY:
        for m in OPENROUTER_MODELS:
            try:
                res = _openai_compat(prompt, "https://openrouter.ai/api/v1", OPENROUTER_KEY, m)
                if res:
                    log.info("AI ok: openrouter/%s", m)
                    return res
            except Exception as e:
                log.warning("OpenRouter %s failed: %s", m, str(e)[:150])
            
    return None


# ---------------- Извлечение текста ----------------
def extract_text(path: str, file_name: str) -> str:
    fn = file_name.lower()
    try:
        if fn.endswith(".pdf"):
            return "".join(p.extract_text() or "" for p in PdfReader(path).pages)
        elif fn.endswith(".docx"):
            return "\n".join(p.text for p in Document(path).paragraphs)
        elif fn.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        log.error("extract_text failed: %s", e)
    return ""


def bg(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


async def send_telegram(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    if not TELEGRAM_API:
        return
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


async def answer_callback(cb_id: str, text: str = ""):
    if not TELEGRAM_API:
        return
    try:
        payload = {"callback_query_id": cb_id}
        if text:
            payload["text"] = text
        async with HTTP.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload) as resp:
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


async def hh_api_search(query: str):
    try:
        async with HTTP.get("https://api.hh.ru/vacancies",
                            params={"text": query, "area": "1", "per_page": "30"},
                            headers={"User-Agent": "LemusCareerBot/1.0", "HH-User-Agent": "LemusCareerBot/1.0"}) as resp:
            data = await resp.json()
            items = []
            for i in data.get("items", []):
                items.append({
                    "id": i.get("id"), 
                    "name": i.get("name"),
                    "company": (i.get("employer") or {}).get("name"),
                    "url": i.get("alternate_url") or f"https://hh.ru/vacancy/{i.get('id')}"
                })
            return items or None
    except Exception as e:
        log.warning("hh API failed: %s", str(e)[:150])
        return None


async def handle_search(chat_id: int, is_admin: bool):
    await send_telegram(chat_id, "🔍 Анализирую резюме и подбираю вакансии...")
    resume = get_active_resume(chat_id)
    
    prompt = (
        "Проанализируй резюме и сформируй ОДНУ максимально точную поисковую фразу для hh.ru (2-4 слова без кавычек), "
        "которая соответствует уровню квалификации и специальности пользователя. "
        "Используй слова, которые работодатели указывают в заголовках вакансий для такого уровня.\n\n" + resume[:4000]
    )
    
    query = await asyncio.to_thread(ai_generate, prompt)
    if not query or query.startswith("⚠"):
        await send_telegram(chat_id, "⚠️ Не удалось автоматически подобрать запрос. Напиши, какую должность ты ищешь?", get_keyboard(is_admin))
        return
        
    query = query.strip().strip('"').strip()
    await send_telegram(chat_id, f"🔍 Ищу по запросу: *{query}*...", get_keyboard(is_admin))

    items = await hh_api_search(query)
    if not items:
        await send_telegram(chat_id, f"⚠️ Не удалось найти вакансии по запросу «{query}».", get_keyboard(is_admin))
        return

    valid_items = [v for v in items if str(v["id"]) not in ignored_vacancies]

    await send_telegram(chat_id, f"🔥 Нашел позиций по запросу «{query}»: {len(valid_items)}. Вывожу лучшие:", get_keyboard(is_admin))
    
    for v in valid_items[:12]:
        vid = str(v["id"])
        name = v.get("name") or "Вакансия"
        comp = v.get("company") or "Компания"
        temp_vacancies[vid] = {"title": name, "employer": comp}
        
        markup = {
            "inline_keyboard": [
                [
                    {"text": "✍️ Сопроводительное письмо", "callback_data": f"gen_{vid}"},
                    {"text": "👎 Не релевантно", "callback_data": f"ignore_{vid}"}
                ]
            ]
        }
        await send_telegram(chat_id, f"🏢 *{comp}*\n💼 [{name}]({v.get('url')})", markup)
        await asyncio.sleep(0.2)


async def run_ai_generation(chat_id: int, vac_info: dict):
    await send_telegram(chat_id, f"✍️ Готовлю сопроводительное письмо для *{vac_info['employer']}* на позицию «{vac_info['title']}»...")
    resume = get_active_resume(chat_id) or "Опыт работы указан в профиле."
    letter = await asyncio.to_thread(
        ai_generate,
        f"Напиши профессиональное убедительное сопроводительное письмо на позицию '{vac_info['title']}' в '{vac_info['employer']}' на основе резюме:\n\n{resume}"
    )
    if not letter:
        await send_telegram(chat_id, "⚠️ ИИ временно перегружен, попробуй через минуту.")
        return
    await send_telegram(chat_id, f"📝 *Сопроводительное письмо:*\n\n{letter}")


async def handle_ai(chat_id: int, is_admin: bool, prompt: str):
    await send_telegram(chat_id, "⏳ Думаю над ответом...")
    answer = await asyncio.to_thread(ai_generate, prompt)
    if not answer:
        await send_telegram(chat_id, "⚠️ ИИ временно недоступен, попробуй позже.", get_keyboard(is_admin))
    else:
        await send_telegram(chat_id, answer, get_keyboard(is_admin))


async def handle_document(chat_id: int, document: dict, is_admin: bool):
    file_id = document["file_id"]
    file_name = document.get("file_name", "resume.pdf")
    
    fn_lower = file_name.lower()
    if not (fn_lower.endswith(".pdf") or fn_lower.endswith(".docx") or fn_lower.endswith(".txt")):
        await send_telegram(chat_id, "⚠️ Пожалуйста, отправьте резюме в формате PDF, DOCX или TXT.", get_keyboard(is_admin))
        return

    try:
        async with HTTP.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}) as resp:
            file_info = await resp.json()
            file_path = file_info.get("result", {}).get("file_path")
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

    if not text_content or len(text_content.strip()) < 30:
        await send_telegram(chat_id, "⚠️ Файл оказался пустым или не содержит читаемого текста.", get_keyboard(is_admin))
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


async def process_message(msg: dict):
    chat_id = msg["chat"]["id"]
    username = msg["chat"].get("username", "")
    text = (msg.get("text") or "").strip()
    document = msg.get("document")

    cur.execute("INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?, 5)", (chat_id, username))
    conn.commit()
    is_admin = ADMIN_ID != 0 and chat_id == ADMIN_ID

    if document:
        await handle_document(chat_id, document, is_admin)
        return
    if not text:
        return

    if text.startswith("/start"):
        await send_telegram(chat_id, "👋 Привет! Твой карьерный агент готов к работе. Используй меню ниже для управления резюме и поиска вакансий.", get_keyboard(is_admin))
    elif text == "ℹ️ Помощь":
        await send_telegram(chat_id, "💡 *Как пользоваться ботом:*\n1. Загрузи резюме (PDF, DOCX или TXT).\n2. Нажми «🔍 Поиск вакансий».\n3. Используй ИИ-инструменты.", get_keyboard(is_admin))
    elif text == "💎 Оплата и Баланс":
        balance = get_user_balance(chat_id)
        await send_telegram(chat_id, f"💎 *Твой баланс:* `{balance}` запросов к ИИ.", get_keyboard(is_admin))
    elif text in ("👑 Админ-панель", "/admin"):
        if not is_admin:
            await send_telegram(chat_id, "⛔ Нет доступа.")
            return
        cur.execute("SELECT COUNT(*) FROM users")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM resumes")
        resumes = cur.fetchone()[0]
        await send_telegram(chat_id, f"👑 *Админ-панель*\n\n👥 Всего пользователей: `{total}`\n📁 Всего резюме: `{resumes}`", get_keyboard(is_admin))
    elif text == "📁 Мои резюме":
        rows = list_resumes(chat_id)
        if not rows:
            await send_telegram(chat_id, "⚠️ Нет загруженных резюме.", get_keyboard(is_admin))
        else:
            kb = {"inline_keyboard": [[{"text": f"{'✅' if r['active'] else '📄'} {r['name']}",
                                        "callback_data": f"act_{r['id']}"}] for r in rows]}
            await send_telegram(chat_id, "📁 *Твои резюме:*", kb)
    elif text == "📥 Загрузить резюме":
        await send_telegram(chat_id, "📄 Отправь файл резюме (PDF, DOCX или TXT) в чат.", get_keyboard(is_admin))
    elif text == "🔍 Поиск вакансий":
        if not get_active_resume(chat_id):
            await send_telegram(chat_id, "⚠️ Сначала загрузи резюме!", get_keyboard(is_admin))
        else:
            bg(handle_search(chat_id, is_admin))
    elif text == "📌 Трекер откликов":
        await send_telegram(chat_id, "📌 Твои отклики пока пусты.", get_keyboard(is_admin))
    else:
        resume = get_active_resume(chat_id)
        if not resume and any(k in text for k in ["Адаптация", "Анализ навыков", "Аудит"]):
            await send_telegram(chat_id, "⚠️ Сначала загрузи резюме!", get_keyboard(is_admin))
            return
            
        if "Адаптация" in text:
            prompt = f"Адаптируй это резюме под целевую позицию:\n\n{resume}"
        elif "Анализ навыков" in text:
            prompt = f"Проведи Skill Gap анализ на основе резюме:\n\n{resume}"
        elif "Аудит" in text:
            prompt = f"Сделай жесткий аудит и дай рекомендации по улучшению этого резюме:\n\n{resume}"
        elif "Тренажер" in text:
            prompt = "Ты жесткий интервьюер. Задай мне первый каверзный вопрос для кандидата."
        else:
            prompt = f"Ты карьерный консультант. Контекст резюме пользователя:\n{resume[:8000]}\n\nВопрос пользователя: {text}"
            
        bg(handle_ai(chat_id, is_admin, prompt))


async def telegram_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="OK")

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
        data_str = cb.get("data", "") or ""
        cb_id = cb.get("id", "")
        
        if data_str.startswith("gen_"):
            bg(answer_callback(cb_id, "Генерирую письмо..."))
            v = temp_vacancies.get(data_str[4:], {"title": "Вакансия", "employer": "Компания"})
            bg(run_ai_generation(chat_id, dict(v)))
        elif data_str.startswith("ignore_"):
            vid = data_str[7:]
            ignored_vacancies.add(vid)
            bg(answer_callback(cb_id, "Вакансия скрыта."))
            try:
                await HTTP.post(f"{TELEGRAM_API}/deleteMessage", json={
                    "chat_id": chat_id, 
                    "message_id": cb["message"]["message_id"]
                })
            except Exception:
                pass
        elif data_str.startswith("act_"):
            bg(answer_callback(cb_id))
            bg(activate_resume(chat_id, data_str[4:]))
            
        return web.Response(text="OK")

    if "message" in data:
        bg(process_message(data["message"]))

    return web.Response(text="OK")


async def main():
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    if BOT_TOKEN:
        app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    app_site = web.TCPSite(runner, "0.0.0.0", PORT)
    await app_site.start()

    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url and BOT_TOKEN:
        webhook_url = f"{render_url}/{BOT_TOKEN}"
        async with HTTP.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}") as resp:
            log.info("setWebhook: %s", (await resp.text())[:200])

    log.info("🚀 Bot started successfully.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
