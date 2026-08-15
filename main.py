import asyncio
import logging
import os
import sqlite3
import aiohttp
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-2.0-flash'

# --- БД ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_name TEXT, status TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS dislikes (user_id INTEGER, vacancy_title TEXT)')
conn.commit()

user_resumes = {}
temp_vacancies = {}

# --- ПАРСИНГ ---
async def fetch_hh_vacancies(keywords: str):
    url = "https://api.hh.ru/vacancies"
    params = {"text": keywords, "area": 1, "per_page": 5, "period": 10, "order_by": "relevance"}
    headers = {"User-Agent": "LemusCareerBot/1.0"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("items", [])
        except: return []
    return []

# --- ИИ ---
def call_gemini(prompt: str):
    try:
        res = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
        return res.text
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {e}"

# --- КЛАВИАТУРА ---
def get_main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме"); b.button(text="📤 Загрузить")
    b.button(text="🔍 Поиск вакансий"); b.button(text="🛠 Адаптация резюме")
    b.button(text="✍️ Отклик"); b.button(text="📊 Skill Gap")
    b.button(text="📋 Аудит резюме"); b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов"); b.button(text="💎 Оплата и Баланс")
    b.button(text="🎁 Пригласить друга"); b.button(text="ℹ️ Помощь")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

# --- БАЛАНС И УТИЛИТЫ ---
def get_balance(user_id):
    res = cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)).fetchone()
    return res[0] if res else 0

def add_balance(user_id, amount):
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()

async def check_and_deduct(user_id, message: types.Message) -> bool:
    if get_balance(user_id) <= 0:
        await message.answer("⚠️ Твои запросы закончились!")
        return False
    add_balance(user_id, -1)
    return True

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    if not cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (message.from_user.id,)).fetchone():
        cursor.execute('INSERT INTO users (user_id) VALUES (?)', (message.from_user.id,))
        conn.commit()
    await message.answer("👋 Привет! Я твой карьерный агент.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_handler(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("⚠️ Сначала загрузи резюме.")
    b = InlineKeyboardBuilder()
    for name in resumes.keys(): b.button(text=f"📄 {name}", callback_data=f"search_cv:{name}")
    await message.answer("Выберите резюме:", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("search_cv:"))
async def do_search(callback: types.CallbackQuery):
    cv_name = callback.data.split(":")[1]
    cv_text = user_resumes[callback.from_user.id][cv_name]
    if not await check_and_deduct(callback.from_user.id, callback.message): return
    
    dislikes = [r[0] for r in cursor.execute('SELECT vacancy_title FROM dislikes WHERE user_id = ?', (callback.from_user.id,)).fetchall()]
    keywords = call_gemini(f"Выдели 3 ключевых слова для поиска вакансии. Исключи: {dislikes}. РЕЗЮМЕ: {cv_text[:1000]}")
    vacs = await fetch_hh_vacancies(keywords.replace('"', ''))
    
    for v in vacs:
        b = InlineKeyboardBuilder()
        b.button(text="👎 Мимо", callback_data=f"disl_{v['name'][:30]}")
        await callback.message.answer(f"🏢 {v['name']}\n{v['alternate_url']}", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("disl_"))
async def dislike(callback: types.CallbackQuery):
    cursor.execute('INSERT INTO dislikes VALUES (?, ?)', (callback.from_user.id, callback.data.split("_")[1]))
    conn.commit()
    await callback.message.edit_text("❌ Скрыто и учтено.")

@dp.message(F.text == "📋 Аудит резюме")
async def audit(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("⚠️ Сначала загрузи резюме.")
    cv_text = list(resumes.values())[0]
    await message.answer(call_gemini(f"Проведи глубокий аудит резюме:\n\n{cv_text}"))

@dp.message(F.text == "📌 Трекер откликов")
async def tracker(message: types.Message):
    rows = cursor.execute('SELECT company_name, status FROM applications WHERE user_id = ?', (message.from_user.id,)).fetchall()
    await message.answer("\n".join([f"{r[0]} — {r[1]}" for r in rows]) or "Пусто")

@dp.message(Command("adminlemus71"))
async def admin(message: types.Message):
    if str(message.from_user.id) == str(ADMIN_ID):
        await message.answer("👑 Админка: используй кнопки или команды для управления.")

@dp.message(F.text)
async def chat_handler(message: types.Message):
    # Универсальный ИИ-чат для всего остального
    await message.answer(call_gemini(message.text))

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())