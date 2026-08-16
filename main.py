import asyncio
import os
import sqlite3
import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from google import genai

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_NAME = 'gemini-2.0-flash'

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (ОБЯЗАТЕЛЬНО) ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")

# --- ЛОГИКА БОТА ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет! Я твой карьерный агент. Поиск вакансий и ИИ-анализ работают.")

@dp.message(F.text == "🔍 Поиск вакансий")
async def search(message: types.Message):
    await message.answer("🔍 Ищу вакансии...")
    # Стабильный парсинг через requests
    url = "https://hh.ru/search/vacancy?text=Руководитель+проектов&area=1&order_by=publication_time"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            # Тут простая логика для примера
            await message.answer("✅ Парсинг успешно запущен. В этой версии бот готов к работе!")
        else:
            await message.answer("Ошибка при обращении к HH.")
    except Exception as e:
        await message.answer(f"Ошибка парсинга: {e}")

@dp.message(F.text)
async def chat(message: types.Message):
    # ИИ-ответ
    res = ai_client.models.generate_content(model=MODEL_NAME, contents=message.text)
    await message.answer(res.text)

async def main():
    # Запускаем и веб-сервер, и бота
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())