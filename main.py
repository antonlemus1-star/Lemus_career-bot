import asyncio
import os
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from google.genai import types as gtypes
from pypdf import PdfReader
from docx import Document
import requests
import html
import re
from aiohttp import web

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def ai_generate(prompt: str) -> str:
    if not client:
        return "⚠️ Ошибка: API-ключ Gemini не настроен."
    try:
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
            config=gtypes.GenerateContentConfig(temperature=0.7)
        )
        return response.text if response and response.text else "⚠️ Пустой ответ от ИИ."
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {str(e)[:80]}"

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
    return resumes[idx]["text"]

def get_keyboard(is_admin=False):
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме")
    b.button(text="📥 Загрузить резюме")
    b.button(text="🔍 Поиск вакансий")
    b.button(text="🛠 Адаптация резюме")
    b.button(text="📊 Анализ навыков (Skill Gap)")
    b.button(text="📋 Аудит резюме")
    b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов")
    b.button(text="💎 Оплата и Баланс")
    b.button(text="ℹ️ Помощь")
    if is_admin:
        b.button(text="👑 Админ-панель")
    b.adjust(2, 2, 2, 2, 2, 1)
    return b.as_markup(resize_keyboard=True)

class ResState(StatesGroup):
    waiting_file = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", 
                   (message.from_user.id, message.from_user.username or ""))
    conn.commit()
    is_admin = (message.from_user.id == ADMIN_ID or ADMIN_ID == 0)
    await message.answer(
        "👋 Привет, Антон! Твой карьерный агент готов к работе. Выбирай нужный раздел в меню:",
        reply_markup=get_keyboard(is_admin)
    )

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    await message.answer(
        "💡 *Как пользоваться ботом:*\n"
        "1. Загрузи резюме через кнопку «📥 Загрузить резюме».\n"
        "2. Ищи релевантные вакансии через «🔍 Поиск вакансий».\n"
        "3. Используй ИИ-кнопки для адаптации, аудита и генерации писем!",
        parse_mode="Markdown"
    )

@dp.message(F.text == "👑 Админ-панель")
async def cmd_admin(message: types.Message):
    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]
    await message.answer(f"👑 *Админ-панель*\n\n👥 Всего пользователей: `{total}`", parse_mode="Markdown")

@dp.message(F.text == "📁 Мои резюме")
async def cmd_my_res(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, [])
    if not resumes:
        await message.answer("⚠️ У тебя нет загруженных резюме.")
        return
    active = user_active_resume.get(message.from_user.id, len(resumes) - 1)
    text = "📁 *Твои резюме:*\n\n"
    for i, r in enumerate(resumes):
        mark = "✅ (Активное)" if i == active else ""
        text += f"{i+1}. {r['name']} {mark}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "📥 Загрузить резюме")
async def cmd_load(message: types.Message, state: FSMContext):
    await state.set_state(ResState.waiting_file)
    await message.answer("📄 Отправь файл резюме (PDF, DOCX или RTF) в чат.")

@dp.message(ResState.waiting_file, F.document)
async def process_doc(message: types.Message, state: FSMContext):
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
        text = f"Ошибка: {e}"

    res = user_resumes.setdefault(message.from_user.id, [])
    res.append({"name": doc.file_name, "text": text})
    user_active_resume[message.from_user.id] = len(res) - 1
    
    await message.answer(f"✅ Резюме «{doc.file_name}» сохранено и назначено активным!", reply_markup=get_keyboard())
    if os.path.exists(path):
        os.remove(path)
    await state.clear()

@dp.message(F.text == "🔍 Поиск вакансий")
async def cmd_search(message: types.Message):
    resume = get_active_resume(message.from_user.id)
    if not resume:
        await message.answer("⚠️ Сначала загрузи резюме!")
        return
    
    await message.answer("🔍 Анализирую резюме и ищу вакансии на hh.ru...")
    
    prompt = "Сформулируй ОДНУ короткую поисковую фразу (2-4 слова) для поиска на hh.ru по резюме без кавычек:\n\n" + resume[:4000]
    query = ai_generate(prompt).strip().strip('"')
    
    url = "https://hh.ru/search/vacancy"
    r = requests.get(url, params={"text": query, "area": "1", "items_on_page": "100"}, headers={"User-Agent": "Mozilla/5.0"})
    
    match = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', r.text, re.S)
    if not match:
        await message.answer("⚠️ Не удалось получить вакансии с hh.ru.")
        return
        
    data = json.loads(html.unescape(match.group(1)))
    items = (data.get("vacancySearchResult") or {}).get("vacancies") or []
    
    if not items:
        await message.answer(f"⚠️ По запросу «{query}» ничего не найдено.")
        return
        
    await message.answer(f"🔥 Нашел позиций по запросу «{query}»: {len(items)}. Вывожу первые 15:")
    for item in items[:15]:
        v_id = item.get("vacancyId") or item.get("id")
        name = item.get("name")
        comp = (item.get("company") or {}).get("name") or "Компания"
        link = f"https://hh.ru/vacancy/{v_id}"
        temp_vacancies[str(v_id)] = {"title": name, "employer": comp}
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{v_id}")
        
        await message.answer(f"🏢 *{comp}*\n💼 [{name}]({link})", reply_markup=builder.as_markup(), parse_mode="Markdown")
        await asyncio.sleep(0.2)

@dp.callback_query(F.data.startswith("gen_"))
async def callback_gen(callback: types.CallbackQuery):
    v_id = callback.data.replace("gen_", "")
    v_info = temp_vacancies.get(v_id, {"title": "Вакансия", "employer": "Компания"})
    await callback.answer("Генерирую письмо...")
    
    resume = get_active_resume(callback.from_user.id) or "Опыт не указан."
    prompt = f"Напиши профессиональное сопроводительное письмо на позицию '{v_info['title']}' в '{v_info['employer']}' на основе резюме:\n\n{resume}"
    
    letter = ai_generate(prompt)
    await callback.message.answer(f"📝 *Сопроводительное письмо:*\n\n{letter}", parse_mode="Markdown")

@dp.message(F.text.in_({"🛠 Адаптация резюме", "📊 Анализ навыков (Skill Gap)", "📋 Аудит резюме", "🎤 Тренажер собеседований"}))
async def ai_buttons_handler(message: types.Message):
    resume = get_active_resume(message.from_user.id) or "Резюме не загружено."
    text = message.text
    
    if "Адаптация" in text:
        prompt = f"Адаптируй это резюме под позицию руководителя проектов в крупном телекоме:\n\n{resume}"
    elif "Анализ навыков" in text:
        prompt = f"Проведи Skill Gap анализ для руководителя проектов на основе резюме:\n\n{resume}"
    elif "Аудит" in text:
        prompt = f"Сделай жесткий аудит и дай рекомендации по улучшению этого резюме:\n\n{resume}"
    elif "Тренажер" in text:
        prompt = "Ты жесткий интервьюер. Задай мне первый каверзный вопрос для кандидата на позицию Руководитель проектов."
    else:
        return

    await message.answer("⏳ Думаю над ответом...")
    answer = ai_generate(prompt)
    await message.answer(answer)

@dp.message(F.text == "📌 Трекер откликов")
async def cmd_tracker(message: types.Message):
    await message.answer("📌 Твои активные отклики пока пусты.")

@dp.message(F.text.in_({"💎 Оплата и Баланс", "🎁 Пригласить друга"}))
async def cmd_balance(message: types.Message):
    await message.answer("ℹ️ Баланс запросов: 30.")

async def main():
    # Поднимаем фиктивный веб-сервер на порту 10000, чтобы Render видел открытый порт
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())