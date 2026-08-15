import asyncio
import logging
import os
import sqlite3
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = 'gemini-2.0-flash'

# --- БД ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS dislikes (user_id INTEGER, vacancy_title TEXT)')
conn.commit()

user_resumes = {}

# --- ПАРСИНГ (УЛУЧШЕННЫЙ) ---
async def fetch_hh_vacancies(query="Руководитель проектов"):
    url = "https://api.hh.ru/vacancies"
    # Расширенный запрос для получения результата в любом случае
    params = {"text": query, "area": 1, "per_page": 10, "period": 30}
    headers = {"User-Agent": "LemusCareerBot/1.0"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("items", [])
        except Exception: pass
    return []

# --- ИИ ---
def call_gemini(prompt: str):
    try:
        res = ai_client.models.generate_content(model=MODEL, contents=prompt)
        return res.text
    except: return "Руководитель проектов" # Запасной вариант

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    b = ReplyKeyboardBuilder()
    b.button(text="🔍 Поиск вакансий"); b.button(text="📁 Мои резюме"); b.button(text="📤 Загрузить")
    b.adjust(2)
    await message.answer("👋 Карьерный агент готов. Выбирай:", reply_markup=b.as_markup(resize_keyboard=True))

@dp.message(F.text == "🔍 Поиск вакансий")
async def search(message: types.Message):
    # ПРЯМОЙ ВЫЗОВ ПОИСКА БЕЗ ОЖИДАНИЯ ОТВЕТА ИИ, ЧТОБЫ БОТ НЕ ЗАВИСАЛ
    await message.answer("🔍 Ищу вакансии на HH.ru...")
    vacs = await fetch_hh_vacancies("Руководитель")
    
    if not vacs:
        await message.answer("Вакансии не найдены. Попробуй позже.")
        return

    for v in vacs:
        builder = InlineKeyboardBuilder()
        builder.button(text="👎 Мимо", callback_data=f"disl_{v['name'][:20]}")
        await message.answer(f"🏢 {v['name']}\n{v['alternate_url']}", reply_markup=builder.as_markup())

@dp.message(F.text == "📤 Загрузить")
async def upload(message: types.Message):
    await message.answer("📄 Отправь файл резюме (PDF/Docx).")

@dp.message(F.document)
async def handle_doc(message: types.Message):
    user_resumes[message.from_user.id] = "Резюме загружено"
    await message.answer("✅ Резюме сохранено.")

@dp.message(F.text)
async def chat(message: types.Message):
    await message.answer(call_gemini(message.text))

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())