import asyncio
import json
import logging
import os
import sqlite3
import aiohttp
import requests
import html
import re
from aiohttp import web
from google import genai
from google.genai import types as gtypes
from pypdf import PdfReader
from docx import Document

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Используем самую стабильную текущую строку модели для нового SDK
GEMINI_MODEL = "gemini-2.5-flash"


def ai_generate(prompt: str) -> str:
    if not client:
        return "⚠️ Ошибка: API-ключ Gemini не настроен на Render."
    if not prompt or not prompt.strip():
        return "⚠️ Ошибка: пустой запрос к ИИ."
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=gtypes.GenerateContentConfig(temperature=0.7)
        )
        return response.text if response and response.text else "⚠️ Пустой ответ от ИИ."
    except Exception as e:
        log.error("Gemini generation failed: %s", e)
        return f"⚠️ Ошибка ИИ: {str(e)[:250]}"


USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

user_resumes = {}
user_active_resume = {}
temp_vacancies = {}

conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)')
conn.commit()


def get_active_resume(user_id: int) -> str:
    resumes = user_resumes.get(user_id, [])
    if not resumes:
        return ""
    idx = user_active_resume.get(user_id, len(resumes) - 1)
    if idx >= len(resumes):
        idx = len(resumes) - 1
    return resumes[idx].get("text", "")


def get_keyboard(is_admin=False):
    kb = [
        [{"text": "📁 Мои резюме"}, {"text": "📥 Загрузить резюме"}],
        [{"text": "🔍 Поиск вакансий"}, {"text": "🛠 Адаптация резюме"}],
        [{"text": "📊 Анализ навыков (Skill Gap)"}, {"text": "📋 Аудит резюме"}],
        [{"text": "🎤 Тренажер собеседований"}, {"text": "📌 Трекер откликов"}],
        [{"text": "💎 Оплата и Баланс"}, {"text": "ℹ️ Помощь"}]
    ]
    if is_admin:
        kb.append([{"text": "👑 Админ-панель"}])
    return {"keyboard": kb, "resize_keyboard": True}


async def send_telegram(chat_id: int, text: str, reply_markup=None, parse_mode="Markdown"):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            result = await resp.json()

        if not result.get("ok") and parse_mode:
            payload.pop("parse_mode", None)
            async with session.post(url, json=payload) as resp2:
                result = await resp2.json()

        return result


async def run_ai_generation(chat_id: int, vac_info: dict):
    await send_telegram(chat_id, f"✍️ Готовлю сильное сопроводительное письмо для *{vac_info['employer']}* на позицию «{vac_info['title']}»...")
    resume = get_active_resume(chat_id) or "Опыт: Руководитель проектов в телекоммуникационной сфере."
    prompt = f"Напиши профессиональное сопроводительное письмо на позицию '{vac_info['title']}' в '{vac_info['employer']}' на основе резюме:\n\n{resume}"
    letter = ai_generate(prompt)
    await send_telegram(chat_id, f"📝 *Сопроводительное письмо:*\n\n{letter}")


async def telegram_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="OK")

    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        data_str = cb["data"]
        async with aiohttp.ClientSession() as session:
            await session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb["id"]})
        if data_str.startswith("gen_"):
            v_id = data_str.replace("gen_", "")
            v_info = temp_vacancies.get(v_id, {"title": "Вакансия", "employer": "Компания"})
            asyncio.create_task(run_ai_generation(chat_id, v_info))
        return web.Response(text="OK")

    if "message" in data:
        msg = data["message"]
        chat_id = msg["chat"]["id"]
        username = msg["chat"].get("username", "")
        text = msg.get("text", "")
        document = msg.get("document")

        cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (chat_id, username))
        conn.commit()
        is_admin = (chat_id == ADMIN_ID or ADMIN_ID == 0)

        if document:
            file_id = document["file_id"]
            file_name = document.get("file_name", "resume.pdf")
            
            if file_name.endswith('.doc'):
                await send_telegram(chat_id, "⚠️ Формат `.doc` устарел и не читается напрямую. Пожалуйста, сохрани файл в формате `.docx` или `.pdf` и отправь заново!", get_keyboard(is_admin))
                return web.Response(text="OK")

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

            if not text_content.strip():
                text_content = "Опыт работы: Руководитель проектов, управление продажами, B2B."

            res = user_resumes.setdefault(chat_id, [])
            res.append({"name": file_name, "text": text_content})
            user_active_resume[chat_id] = len(res) - 1
            
            await send_telegram(chat_id, f"✅ Резюме «{file_name}» успешно сохранено и назначено активным!", get_keyboard(is_admin))
            if os.path.exists(path):
                os.remove(path)

        elif text:
            if text.startswith("/start"):
                await send_telegram(chat_id, "👋 Привет, Антон! Твой карьерный агент готов к работе.", get_keyboard(is_admin))
            elif text == "ℹ️ Помощь":
                await send_telegram(chat_id, "💡 Загрузи резюме через меню и ищи вакансии.", get_keyboard(is_admin))
            elif text in ["👑 Админ-панель", "/admin"]:
                cursor.execute("SELECT COUNT(*) FROM users")
                total = cursor.fetchone()[0]
                await send_telegram(chat_id, f"👑 *Админ-панель*\n\n👥 Пользователей: `{total}`", get_keyboard(is_admin))
            elif text == "📁 Мои резюме":
                res = user_resumes.get(chat_id, [])
                if not res:
                    await send_telegram(chat_id, "⚠️ Нет загруженных резюме. Нажми «📥 Загрузить резюме».")
                else:
                    active = user_active_resume.get(chat_id, len(res) - 1)
                    txt = "📁 *Твои резюме:*\n"
                    for i, r in enumerate(res):
                        mark = "✅" if i == active else ""
                        txt += f"{i+1}. {r['name']} {mark}\n"
                    await send_telegram(chat_id, txt, get_keyboard(is_admin))
            elif text == "📥 Загрузить резюме":
                await send_telegram(chat_id, "📄 Отправь файл резюме (.docx или .pdf) в чат.", get_keyboard(is_admin))
            elif text == "🔍 Поиск вакансий":
                resume = get_active_resume(chat_id)
                if not resume:
                    await send_telegram(chat_id, "⚠️ Сначала загрузи резюме (.docx или .pdf)!", get_keyboard(is_admin))
                else:
                    await send_telegram(chat_id, "🔍 Ищу вакансии на hh.ru...", get_keyboard(is_admin))
                    prompt = "Сформулируй ОДНУ короткую фразу (2-4 слова) для поиска на hh.ru без кавычек:\n\n" + resume[:4000]
                    query = ai_generate(prompt).strip().strip('"')

                    r = requests.get("https://hh.ru/search/vacancy", params={"text": query, "area": "1", "items_on_page": "100"}, headers={"User-Agent": "Mozilla/5.0"})
                    match = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', r.text, re.S)
                    if match:
                        data = json.loads(html.unescape(match.group(1)))
                        items = (data.get("vacancySearchResult") or {}).get("vacancies") or []
                        await send_telegram(chat_id, f"🔥 Нашел позиций по запросу «{query}»: {len(items)}. Вывожу первые 15:", get_keyboard(is_admin))
                        for item in items[:15]:
                            v_id = item.get("vacancyId") or item.get("id")
                            name = item.get("name")
                            comp = (item.get("company") or {}).get("name") or "Компания"
                            link = f"https://hh.ru/vacancy/{v_id}"
                            temp_vacancies[str(v_id)] = {"title": name, "employer": comp}

                            markup = {"inline_keyboard": [[{"text": "✍️ Сопроводительное письмо", "callback_data": f"gen_{v_id}"}]]}
                            await send_telegram(chat_id, f"🏢 *{comp}*\n💼 [{name}]({link})", markup)
                            await asyncio.sleep(0.2)
                    else:
                        await send_telegram(chat_id, "⚠️ Не удалось найти вакансии.", get_keyboard(is_admin))
            else:
                resume = get_active_resume(chat_id)
                if not resume:
                    await send_telegram(chat_id, "⚠️ Сначала загрузи резюме через кнопку «📥 Загрузить резюме»!", get_keyboard(is_admin))
                    return web.Response(text="OK")

                if "Адаптация" in text:
                    prompt = f"Адаптируй это резюме под позицию руководителя проектов в крупном телекоме:\n\n{resume}"
                elif "Анализ навыков" in text:
                    prompt = f"Проведи Skill Gap анализ для руководителя проектов на основе резюме:\n\n{resume}"
                elif "Аудит" in text:
                    prompt = f"Сделай жесткий аудит и дай рекомендации по улучшению этого резюме:\n\n{resume}"
                elif "Тренажер" in text:
                    prompt = "Ты жесткий интервьюер. Задай мне первый каверзный вопрос для кандидата на позицию Руководитель проектов."
                elif "Трекер" in text:
                    await send_telegram(chat_id, "📌 Твои отклики пока пусты.", get_keyboard(is_admin))
                    return web.Response(text="OK")
                else:
                    prompt = text

                await send_telegram(chat_id, "⏳ Думаю над ответом...", get_keyboard(is_admin))
                answer = ai_generate(prompt)
                await send_telegram(chat_id, answer, get_keyboard(is_admin))

    return web.Response(text="OK")


async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    webhook_url = f"{os.getenv('RENDER_EXTERNAL_URL')}/{BOT_TOKEN}"
    async with aiohttp.ClientSession() as session:
        await session.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}")

    log.info("Bot started on aiohttp webhook.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())