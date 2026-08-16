import asyncio
import os
import sqlite3
import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ИНИЦИАЛИЗАЦИЯ ИИ (стабильная версия)
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def ai_generate(prompt: str) -> str:
    if not client: return "ИИ не настроен."
    try:
        # Используем самую стабильную текущую версию
        response = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
        return response.text
    except Exception as e:
        return f"Ошибка ИИ: {str(e)[:50]}"

# --- ПАРСЕР HH (Прямой запрос с заголовками браузера) ---
def fetch_hh_vacancies_sync():
    # Запрос через API HH напрямую
    url = "https://api.hh.ru/vacancies"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    params = {"text": "Руководитель проектов", "area": "1", "per_page": "10"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", [])
        return None
    except:
        return None

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Бот обновлен и готов к работе!", reply_markup=ReplyKeyboardBuilder().button(text="🔍 Поиск вакансий").as_markup(resize_keyboard=True))

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Ищу вакансии...")
    vacancies = await asyncio.to_thread(fetch_hh_vacancies_sync)
    if not vacancies:
        await message.answer("Не удалось получить вакансии. Попробуйте позже.")
        return
    for v in vacancies:
        await message.answer(f"🏢 {v.get('employer', {}).get('name')}\n💼 {v.get('name')}\n{v.get('alternate_url')}")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer)

async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Running"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
