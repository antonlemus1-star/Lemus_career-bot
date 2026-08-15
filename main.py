import asyncio
import logging
import os
import sqlite3
import aiohttp
import striprtf  # Установи: pip install striprtf
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
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
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = 'gemini-2.0-flash'

# --- БД ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
# ... [остальные таблицы те же] ...
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_name TEXT, status TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS dislikes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, vacancy_title TEXT)')
conn.commit()

user_resumes = {}
temp_vacancies = {}

# --- УНИВЕРСАЛЬНЫЙ ПАРСИНГ ФАЙЛОВ ---
def extract_text_from_file(path, filename):
    if filename.endswith('.pdf'):
        return "".join([p.extract_text() or "" for p in PdfReader(path).pages])
    elif filename.endswith('.docx'):
        return "\n".join([p.text for p in Document(path).paragraphs])
    elif filename.endswith('.rtf'):
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return striprtf.striprtf(f.read())
    return None

# --- ПАРСЕР HH API ---
async def fetch_hh_vacancies(keywords: str):
    url = "https://api.hh.ru/vacancies"
    headers = {"User-Agent": "LemusCareerBot/1.0"}
    # Поиск по широким запросам
    for q in [keywords, "Руководитель", "Director"]:
        params = {"text": q, "area": 1, "per_page": 10, "period": 30, "order_by": "publication_time"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("items"): return data["items"], q
            except: continue
    return [], ""

# --- FSM СОСТОЯНИЯ ---
class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    choosing_cv_for_search = State()
    # ... [остальные состояния сохранены] ...

# --- ХЕНДЛЕРЫ ---
@dp.message(F.document)
async def process_doc(message: types.Message):
    doc = message.document
    path = f"tmp_{message.from_user.id}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    
    text = extract_text_from_file(path, doc.file_name)
    if not text:
        await message.answer("⚠️ Не удалось прочитать формат файла. Отправь текст резюме просто сообщением.")
    else:
        user_resumes.setdefault(message.from_user.id, {})[doc.file_name] = text
        await message.answer(f"✅ Успешно сохранено: {doc.file_name} (символов: {len(text)})")
    
    if os.path.exists(path): os.remove(path)

@dp.callback_query(F.data.startswith("use_cv:"))
async def process_search_logic(callback: types.CallbackQuery, state: FSMContext):
    cv_idx = int(callback.data.split(":")[1])
    resumes = user_resumes.get(callback.from_user.id, {})
    cv_text = list(resumes.values())[cv_idx]
    
    await callback.message.edit_text("🔍 Анализирую резюме и ищу вакансии...")
    
    # ИИ вытаскивает должность из резюме, если файл прочитан
    prompt = f"Вытащи только должность из резюме: {cv_text[:500]}"
    keywords = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt).text.strip()
    
    vacs, used_q = await fetch_hh_vacancies(keywords)
    if vacs:
        for v in vacs[:5]:
            temp_vacancies[str(v['id'])] = v['name']
            await callback.message.answer(f"🏢 {v['employer']['name']}\n💼 [{v['name']}]({v['alternate_url']})", parse_mode="Markdown")
    else:
        await callback.message.edit_text("Ничего не найдено.")
    await state.clear()

# --- ОСТАЛЬНОЙ ФУНКЦИОНАЛ ОСТАВЛЯЕМ БЕЗ ИЗМЕНЕНИЙ ---
# [Здесь должны быть сохранены все остальные функции CRM, Админки и т.д.]

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())