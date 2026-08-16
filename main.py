import asyncio
import html
import json
import logging
import os
import re
import sqlite3

import aiohttp
import requests
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from docx import Document
from google import genai
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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


# --- ПОМОЩНИКИ ПО РЕЗЮМЕ ---
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


# --- ПАРСЕР ЧЕРЕЗ HTML-СТРАНИЦУ ПОИСКА ---
HH_SEARCH_URL = "https://hh.ru/search/vacancy"
STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)


def fetch_hh_vacancies_sync(query: str, area: str = "1"):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
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
        return [], "Не найден блок HH-Lux-InitialState (возможно, защита/капча hh.ru)."

    try:
        data = json.loads(html.unescape(match.group(1)))
    except Exception as e:
        return [], f"Ошибка разбора JSON: {e}"

    raw_vacancies = (data.get("vacancySearchResult") or {}).get("vacancies") or []
    if not raw_vacancies:
        return [], "Вакансии не найдены."

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

        formatted.append({
            "id": str(vac_id),
            "name": name,
            "employer": {"name": employer_name},
            "alternate_url": url,
        })

    return formatted, ""


async def fetch_hh_vacancies(query: str, area: str = "1"):
    return await asyncio.to_thread(fetch_hh_vacancies_sync, query, area)


# --- ИНТЕРФЕЙС ---
def get_main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме"); b.button(text="📥 Загрузить резюме")
    b.button(text="🔍 Поиск вакансий"); b.button(text="🛠 Адаптация резюме")
    b.button(text="✍️ Отклик"); b.button(text="📊 Анализ навыков (Skill Gap)")
    b.button(text="📋 Аудит резюме"); b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов")
    b.button(text="💎 Оплата и Баланс"); b.button(text="🎁 Пригласить друга")
    b.button(text="ℹ️ Помощь")
    b.adjust(2, 2, 2, 2, 1, 2, 1)
    return b.as_markup(resize_keyboard=True)


class CareerState(StatesGroup):
    waiting_for_resume_file = State()


# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет, Антон! Твой карьерный агент полностью готов к работе.", reply_markup=get_main_keyboard())


@dp.message(F.text == "📁 Мои резюме")
async def my_resumes(message: types.Message):
    user_id = message.from_user.id
    resumes = user_resumes.get(user_id, [])
    if not resumes:
        await message.answer("⚠️ У тебя пока нет загруженного резюме. Нажми «📥 Загрузить резюме».")
        return

    active_idx = user_active_resume.get(user_id, len(resumes) - 1)
    lines = [f"📁 Сохранено резюме: {len(resumes)}/{MAX_RESUMES}\n"]
    builder = InlineKeyboardBuilder()
    for i, r in enumerate(resumes):
        mark = "✅ " if i == active_idx else ""
        lines.append(f"{mark}{i + 1}. {r['name']}")
        builder.button(text=f"Сделать активным: {i + 1}", callback_data=f"setactive_{i}")
    builder.adjust(1)
    await message.answer("\n".join(lines), reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("setactive_"))
async def set_active_resume(callback: types.CallbackQuery):
    idx = int(callback.data.replace("setactive_", ""))
    user_active_resume[callback.from_user.id] = idx
    await callback.answer("Активное резюме обновлено ✅")
    resumes = user_resumes.get(callback.from_user.id, [])
    if 0 <= idx < len(resumes):
        await callback.message.answer(f"✅ Активное резюме: {resumes[idx]['name']}")


@dp.message(F.text == "📥 Загрузить резюме")
async def upload_resume(message: types.Message, state: FSMContext):
    resumes = user_resumes.get(message.from_user.id, [])
    if resumes >= MAX_RESUMES:
        await message.answer(f"⚠️ Достигнут лимит в {MAX_RESUMES} резюме. Сбрось их командой /reset_resumes.")
        return
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл резюме (PDF, Word .docx или RTF).", reply_markup=types.ReplyKeyboardRemove())


@dp.message(Command("reset_resumes"))
async def reset_resumes(message: types.Message):
    user_resumes.pop(message.from_user.id, None)
    user_active_resume.pop(message.from_user.id, None)
    await message.answer("🗑 Все резюме удалены.", reply_markup=get_main_keyboard())


@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_file(message: types.Message, state: FSMContext):
    doc = message.document
    path = f"tmp_{message.from_user.id}_{doc.file_name}"
    await bot.download(doc, destination=path)

    text = ""
    try:
        if doc.file_name.endswith('.pdf'):
            text = "".join([p.extract_text() or "" for p in PdfReader(path).pages])
        elif doc.file_name.endswith('.docx'):
            text = "\n".join([p.text for p in Document(path).paragraphs])
        elif doc.file_name.endswith('.rtf'):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
    except Exception as e:
        text = f"Ошибка чтения: {e}"

    resumes = user_resumes.setdefault(message.from_user.id, [])
    resumes.append({"name": doc.file_name, "text": text})
    user_active_resume[message.from_user.id] = len(resumes) - 1

    await message.answer(
        f"✅ Резюме «{doc.file_name}» сохранено ({len(resumes)}/{MAX_RESUMES}). Оно назначено активным.",
        reply_markup=get_main_keyboard(),
    )

    if os.path.exists(path):
        os.remove(path)
    await state.clear()


@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    user_id = message.from_user.id
    resume_text = get_active_resume_text(user_id)

    if not resume_text.strip():
        await message.answer("⚠️ Сначала загрузи резюме («📥 Загрузить резюме») — по нему я подберу поисковый запрос.")
        return

    await message.answer("🔍 Анализирую резюме и ищу подходящие вакансии на hh.ru...")

    query = await build_query_from_resume(resume_text)
    vacancies, err = await fetch_hh_vacancies(query)

    if not vacancies:
        return await message.answer(f"⚠️ Не удалось получить вакансии по запросу «{query}». Причина: {err or 'пустой ответ'}")

    await message.answer(f"🔥 По запросу «{query}» нашёл позиций: {len(vacancies)}. Вывожу:")

    for v in vacancies:
        vac_id = str(v.get("id"))
        title = v.get("name", "Вакансия")
        employer = v.get("employer", {}).get("name", "Компания")
        url = v.get("alternate_url", "https://hh.ru")
        temp_vacancies[vac_id] = title

        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{vac_id}")

        await message.answer(
            f"🏢 **{employer}**\n💼 [{title}]({url})",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        )
        await asyncio.sleep(0.3)


@dp.callback_query(F.data.startswith("gen_"))
async def gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_", "")
    title = temp_vacancies.get(vac_id, "Вакансия")
    await callback.answer("Генерирую письмо...", show_alert=False)

    resume = get_active_resume_text(callback.from_user.id) or "Опыт: не указан."
    prompt = f"Напиши сильное профессиональное сопроводительное письмо для отклика на позицию '{title}' на основе резюме:\n{resume}"

    letter_text = await asyncio.to_thread(ai_generate, prompt)
    await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter_text}", parse_mode="Markdown")


@dp.message(F.text)
async def chat(message: types.Message):
    text = message.text
    resume = get_active_resume_text(message.from_user.id) or "Резюме не загружено."

    if text == "🛠 Адаптация резюме":
        prompt = f"Адаптируй это резюме под позицию руководителя проектов в крупном телекоме:\n{resume}"
    elif text == "📊 Анализ навыков (Skill Gap)":
        prompt = f"Проведи Skill Gap анализ для руководителя проектов на основе резюме:\n{resume}"
    elif text == "📋 Аудит резюме":
        prompt = f"Сделай жесткий аудит и дай рекомендации по улучшению этого резюме:\n{resume}"
    elif text == "🎤 Тренажер собеседований":
        prompt = "Ты интервьюер. Задай мне первый каверзный вопрос для кандидата на позицию Руководитель проектов."
    elif text == "📌 Трекер откликов":
        await message.answer("📌 Твои активные отклики пока пусты. Отправляй отклики через поиск вакансий!")
        return
    elif text in ["💎 Оплата и Баланс", "🎁 Пригласить друга", "ℹ️ Помощь"]:
        await message.answer("ℹ️ Брендовый карьерный агент работает в штатном режиме. Баланс запросов: 30.")
        return
    else:
        prompt = message.text

    answer = await asyncio.to_thread(ai_generate, prompt)
    await message.answer(answer, reply_markup=get_main_keyboard())


async def keep_alive_pinger():
    external_url = os.getenv("RENDER_EXTERNAL_URL")
    if not external_url:
        log.info("RENDER_EXTERNAL_URL не задан — самопинг пропущен.")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(external_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    log.info("Keep-alive ping: %s", resp.status)
            except Exception as e:
                log.warning("Keep-alive ping failed: %s", e)


async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()

    asyncio.create_task(keep_alive_pinger())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())