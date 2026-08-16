import asyncio
import os
import sqlite3
import time
from bs4 import BeautifulSoup
import requests
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
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
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ИСПРАВЛЕНИЕ: Используем актуальную модель gemini-2.0-flash
MODEL_NAME = 'gemini-2.0-flash'
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def ai_generate(prompt: str) -> str:
    if not ai_client:
        return "⚠️ ИИ не инициализирован."
    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {str(e)[:100]}"

# --- ПАРСЕР HH (ОБНОВЛЕННЫЙ) ---
def fetch_hh_vacancies_sync(query="Руководитель проектов"):
    # Используем официальный API, он сейчас самый надежный
    url = "https://api.hh.ru/vacancies"
    headers = {"User-Agent": "LemusCareerBot/2.0 (anton@megafon.ru)"}
    params = {"text": query, "area": "1", "per_page": "20", "order_by": "publication_time"}
    
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", []), ""
        return [], f"Ошибка HH {r.status_code}"
    except Exception as e:
        return [], str(e)

# --- ИНТЕРФЕЙС И ХЕНДЛЕРЫ ---
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

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет! Я твой карьерный агент.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Ищу актуальные вакансии...")
    vacancies, err = await asyncio.to_thread(fetch_hh_vacancies_sync, "Руководитель проектов")
    
    if not vacancies:
        await message.answer(f"⚠️ HH пока не ответил. {err}")
        return

    for v in vacancies[:5]:
        title = v.get("name")
        employer = v.get("employer", {}).get("name")
        url = v.get("alternate_url")
        await message.answer(f"🏢 {employer}\n💼 {title}\n{url}")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer, reply_markup=get_main_keyboard())

async def main():
    # Запуск веб-сервера для Render
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