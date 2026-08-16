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
from bs4 import BeautifulSoup
from docx import Document
from google import genai
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def ai_generate(prompt: str) -> str:
    if not client:
        return "⚠️ Ошибка: API-ключ Gemini не настроен на Render."
    try:
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
        )
        return response.text if response and response.text else "⚠️ Пустой ответ от ИИ."
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {str(e)[:80]}"


USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

MAX_RESUMES = 5
user_resumes: dict[int, list[dict]] = {}
user_active_resume: dict[int, int] = {}
temp_vacancies = {}

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_name TEXT, status TEXT)')
conn.commit()


def get_active_resume_text(user_id: int) -> str:
    resumes = user_resumes.get(user_id, [])
    if not resumes:
        return ""
    idx = user_active_resume.get(user_id, len(resumes) - 1)
    idx = max(0, min(idx, len(resumes) - 1))
    return resumes[idx]["text"]


async def build_query_from_resume(resume_text: str) -> str:
    if not resume_text.strip():
        return "Руководитель проектов"
    prompt = (
        "На основе текста резюме сформулируй ОДНУ короткую поисковую фразу "
        "(2-4 слова) для поиска вакансий на hh.ru — конкретную должность/специализацию, "
        "без вводных слов, без кавычек, без пояснений. Ответь только фразой.\n\n"
        + resume_text[:6000]
    )
    query = await asyncio.to_thread(ai_generate, prompt)
    query = query.strip().strip('"').strip("'")
    if not query or len(query) > 80:
        return "Руководитель проектов"
    return query


HH_SEARCH_URL = "https://hh.ru/search/vacancy"
STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)


def fetch_hh_vacancies_sync(query: str, area: str = "1"):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    params = {"text": query, "area": area, "search_field": "name"}
    try:
        r = requests.get(HH_SEARCH_URL, params=params, headers=headers, timeout=15)
    except Exception as e:
        return [], f"Сетевая ошибка: {e}"

    if r.status_code != 200:
        return [], f"HTTP {r.status_code}"

    match = STATE_RE.search(r.text)
    if not match:
        return [], "Не найден блок данных на hh.ru."

    try:
        data = json.loads(html.unescape(match.group(1)))
    except Exception as e:
        return [], f"Ошибка разбора: {e}"

    raw_vacancies = (data.get("vacancySearchResult") or {}).get("vacancies") or []
    formatted = []
    for item in raw_vacancies:
        vac_id = item.get("vacancyId") or item.get("id")
        name = item.get("name") or "Вакансия"
        employer_name = "Компания"
        company = item.get("company") or item.get("employer")
        if isinstance(company, dict):
            employer_name = company.get("visibleName") or company.get("name") or employer_name

        url = None
        links = item.get("links")
        if isinstance(links, dict):
            url = links.get("desktop") or links.get("vacancy")
        if not url and vac_id:
            url = f"https://hh.ru/vacancy/{vac_id}"

        if not vac_id or not url:
            continue
        formatted.append({"id": str(vac_id), "name": name, "employer": {"name": employer_name}, "alternate_url": url})
    return formatted, ""


async def fetch_hh_vacancies(query: str, area: str = "1"):
    return await asyncio.to_thread(fetch_hh_vacancies_sync, query, area)


# --- ОТПРАВКА СООБЩЕНИЙ ЧЕРЕЗ TELEGRAM API ---
async def send_telegram(chat_id: int, text: str, reply_markup=None):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            return await resp.json()


def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "📁 Мои резюме"}, {"text": "📥 Загрузить резюме"}],
            [{"text": "🔍 Поиск вакансий"}, {"text": "🛠 Адаптация резюме"}],
            [{"text": "📊 Анализ навыков (Skill Gap)"}, {"text": "📋 Аудит резюме"}],
            [{"text": "🎤 Тренажер собеседований"}, {"text": "📌 Трекер откликов"}],
            [{"text": "💎 Оплата и Баланс"}, {"text": "ℹ️ Помощь"}]
        ],
        "resize_keyboard": True
    }


# --- ОБРАБОТЧИК ВХОДЯЩИХ ВЕБХУКОВ ТЕЛЕГРАМА ---
async def telegram_webhook(request):
    try:
        data = await request.json()
    except:
        return web.Response(text="OK")

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "")
        document = msg.get("document")

        if text.startswith("/start"):
            await send_telegram(chat_id, "👋 Привет, Антон! Бот полностью восстановлен на быстром движке.", get_main_keyboard())
        elif text == "📁 Мои резюме":
            resumes = user_resumes.get(chat_id, [])
            if not resumes:
                await send_telegram(chat_id, "⚠️ У тебя пока нет загруженного резюме. Нажми «📥 Загрузить резюме».")
            else:
                active_idx = user_active_resume.get(chat_id, len(resumes) - 1)
                lines = [f"📁 Сохранено резюме: {len(resumes)}/{MAX_RESUMES}\n"]
                for i, r in enumerate(resumes):
                    mark = "✅ " if i == active_idx else ""
                    lines.append(f"{mark}{i + 1}. {r['name']}")
                await send_telegram(chat_id, "\n".join(lines))
        elif text == "📥 Загрузить резюме":
            await send_telegram(chat_id, "📄 Отправь файл резюме (PDF, Word .docx или RTF) прямо в чат.")
        elif text == "🔍 Поиск вакансий":
            resume_text = get_active_resume_text(chat_id)
            if not resume_text.strip():
                await send_telegram(chat_id, "⚠️ Сначала загрузи резюме («📥 Загрузить резюме»).")
            else:
                await send_telegram(chat_id, "🔍 Анализирую резюме и ищу вакансии на hh.ru...")
                query = await build_query_from_resume(resume_text)
                vacancies, err = await fetch_hh_vacancies(query)
                if not vacancies:
                    await send_telegram(chat_id, f"⚠️ Не удалось найти вакансии по запросу «{query}». Причина: {err or 'пусто'}")
                else:
                    await send_telegram(chat_id, f"🔥 Нашёл позиций по запросу «{query}»: {len(vacancies)}")
                    for v in vacancies[:5]:
                        msg_text = f"🏢 *{v['employer']['name']}*\n💼 [{v['name']}]({v['alternate_url']})"
                        await send_telegram(chat_id, msg_text)
        else:
            resume = get_active_resume_text(chat_id) or "Резюме не загружено."
            prompt = text
            if text == "🛠 Адаптация резюме":
                prompt = f"Адаптируй это резюме под позицию руководителя проектов в крупном телекоме:\n{resume}"
            elif text == "📊 Анализ навыков (Skill Gap)":
                prompt = f"Проведи Skill Gap анализ для руководителя проектов на основе резюме:\n{resume}"
            elif text == "📋 Аудит резюме":
                prompt = f"Сделай жесткий аудит и дай рекомендации по улучшению этого резюме:\n{resume}"
            elif text == "🎤 Тренажер собеседований":
                prompt = "Ты интервьюер. Задай мне первый каверзный вопрос для кандидата на позицию Руководитель проектов."
            
            answer = await asyncio.to_thread(ai_generate, prompt)
            await send_telegram(chat_id, answer, get_main_keyboard())

        if document:
            file_id = document["file_id"]
            file_name = document.get("file_name", "resume.pdf")
            # Скачиваем файл через Telegram API
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{TELEGRAM_API}/getFile?file_id={file_id}") as resp:
                    file_info = await resp.json()
                    file_path = file_info.get("result", {}).get("file_path")
                    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                    async with session.get(download_url) as f_resp:
                        content = await f_resp.read()
                        path = f"tmp_{chat_id}_{file_name}"
                        with open(path, "wb") as f:
                            f.write(content)

            text_content = ""
            try:
                if file_name.endswith('.pdf'):
                    text_content = "".join([p.extract_text() or "" for p in PdfReader(path).pages])
                elif file_name.endswith('.docx'):
                    text_content = "\n".join([p.text for p in Document(path).paragraphs])
                elif file_name.endswith('.rtf'):
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        text_content = f.read()
            except Exception as e:
                text_content = f"Ошибка чтения: {e}"

            resumes = user_resumes.setdefault(chat_id, [])
            resumes.append({"name": file_name, "text": text_content})
            user_active_resume[chat_id] = len(resumes) - 1
            await send_telegram(chat_id, f"✅ Резюме «{file_name}» успешно сохранено и назначено активным!", get_main_keyboard())
            if os.path.exists(path):
                os.remove(path)

    return web.Response(text="OK")


async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    # Устанавливаем вебхук для Telegram автоматически
    webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/{BOT_TOKEN}"
    async with aiohttp.ClientSession() as session:
        await session.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}")

    log.info("Webhook bot started successfully.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())