import asyncio
import os
import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from google import genai

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Используем самую простую и стабильную точку входа
client = genai.Client(api_key=GEMINI_API_KEY)

def ai_generate(prompt: str) -> str:
    try:
        # Используем gemini-2.0-flash (это стандарт)
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"Ошибка: {str(e)[:30]}"

def fetch_vacancies():
    # Прямой запрос к API HH с эмуляцией браузера
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {"text": "Руководитель проектов", "area": "1", "per_page": "5"}
    try:
        r = requests.get("https://api.hh.ru/vacancies", headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", [])
    except:
        pass
    return []

@dp.message(F.text == "🔍 Поиск вакансий")
async def search(message: types.Message):
    await message.answer("🔍 Ищу...")
    vacs = await asyncio.to_thread(fetch_vacancies)
    if not vacs:
        await message.answer("Не удалось найти вакансии.")
    for v in vacs:
        await message.answer(f"{v['name']}\n{v['alternate_url']}")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer)

async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
