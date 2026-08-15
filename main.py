import asyncio
import logging
import os
import sqlite3
import aiohttp
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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

# --- ПАРСЕР HH API ---
async def fetch_hh_vacancies(keywords: str):
    url = "https://api.hh.ru/vacancies"
    params = {"text": keywords, "area": 1, "per_page": 5, "period": 30, "order_by": "publication_time"}
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
    except Exception as e: return f"⚠️ Ошибка ИИ: {e}"

# --- КЛАВИАТУРА ---
def get_main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме"); b.button(text="📤 Загрузить")
    b.button(text="🔍 Поиск вакансий"); b.button(text="🛠 Адаптация резюме")
    b.button(text="✍️ Отклик"); b.button(text="📊 Skill Gap")
    b.button(text="📋 Аудит резюме"); b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов"); b.button(text="💎 Оплата и Баланс")
    b.button(text="🎁 Пригласить друга"); b.button(text="ℹ️ Помощь")
    b.adjust(2, 2, 2, 2, 1, 2, 1)
    return b.as_markup(resize_keyboard=True)

# --- УТИЛИТЫ ---
def get_balance(user_id):
    res = cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)).fetchone()
    return res[0] if res else 0

def add_balance(user_id, amount):
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()

async def check_and_deduct(user_id, message: types.Message) -> bool:
    if get_balance(user_id) <= 0:
        await message.answer("⚠️ Нет запросов! Пополни баланс.")
        return False
    add_balance(user_id, -1)
    return True

# --- FSM СОСТОЯНИЯ ---
class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    choosing_cv = State()
    waiting_for_vacancy = State()
    mock_in_progress = State()

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    if not cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (message.from_user.id,)).fetchone():
        cursor.execute('INSERT INTO users (user_id) VALUES (?)', (message.from_user.id,))
        conn.commit()
    await message.answer("👋 Привет! Я твой AI-помощник. Загрузи резюме или начни поиск.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_init(message: types.Message, state: FSMContext):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("⚠️ Сначала загрузи резюме.")
    
    b = InlineKeyboardBuilder()
    for name in resumes.keys(): b.button(text=f"📄 {name}", callback_data=f"cv_search:{name}")
    await message.answer("Выбери резюме для поиска:", reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("cv_search:"))
async def do_search(callback: types.CallbackQuery):
    cv_name = callback.data.split(":")[1]
    cv_text = user_resumes[callback.from_user.id][cv_name]
    
    # Интеллектуальный подбор
    prompt = f"Извлеки должность из этого резюме для поиска вакансий (только название): {cv_text[:500]}"
    keywords = call_gemini(prompt).strip()
    
    await callback.message.edit_text(f"🔍 Ищу: {keywords}...")
    dislikes = [r[0] for r in cursor.execute('SELECT vacancy_title FROM dislikes WHERE user_id = ?', (callback.from_user.id,)).fetchall()]
    vacs = await fetch_hh_vacancies(keywords)
    
    found = False
    for v in vacs:
        if v['name'] not in dislikes:
            found = True
            temp_vacancies[str(v['id'])] = v['name']
            b = InlineKeyboardBuilder()
            b.button(text="👎 Мимо", callback_data=f"disl_{v['id']}")
            await callback.message.answer(f"🏢 {v['employer']['name']}\n💼 [{v['name']}]({v['alternate_url']})", 
                                          reply_markup=b.as_markup(), parse_mode="Markdown")
    if not found: await callback.message.answer("Ничего не найдено.")

@dp.callback_query(F.data.startswith("disl_"))
async def handle_dislike(callback: types.CallbackQuery):
    v_id = callback.data.split("_")[1]
    cursor.execute('INSERT INTO dislikes (user_id, vacancy_title) VALUES (?, ?)', (callback.from_user.id, temp_vacancies.get(v_id, "Вакансия")))
    conn.commit()
    await callback.message.edit_text("❌ Скрыто.")

@dp.message(F.text == "📤 Загрузить")
async def upload_req(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл (PDF/Docx).")

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_doc(message: types.Message, state: FSMContext):
    path = f"tmp_{message.from_user.id}"
    await bot.download(await bot.get_file(message.document.file_id), destination=path)
    # Упрощенная вычитка для примера
    user_resumes.setdefault(message.from_user.id, {})[message.document.file_name] = "Текст резюме..."
    os.remove(path)
    await message.answer("✅ Сохранено!")
    await state.clear()

@dp.message(F.text)
async def chat_any(message: types.Message):
    await message.answer(call_gemini(message.text))

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())