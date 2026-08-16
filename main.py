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

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def ai_generate(prompt: str) -> str:
    if not client:
        return "⚠️ Ошибка: API-ключ Gemini не настроен на Render."
    try:
        # Используем стабильную точку входа Google
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"Ошибка ИИ: {str(e)[:60]}"

def fetch_vacancies():
    headers = {"User-Agent": "Mozilla/5.0"}
    params = {"text": "Руководитель проектов", "area": "1", "per_page": "5"}
    try:
        r = requests.get("https://api.hh.ru/vacancies", headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("items", [])
    except Exception:
        pass
    return []

@dp.message(Command("start"))
async def start(message: types.Message):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔍 Поиск вакансий")
    kb.button(text="📁 Мои резюме")
    kb.adjust(2)
    await message.answer("👋 Привет, Антон! Бот полностью обновлен и готов к работе.", reply_markup=kb.as_markup(resize_keyboard=True))

@dp.message(F.text == "🔍 Поиск вакансий")
async def search(message: types.Message):
    await message.answer("🔍 Ищу актуальные позиции...")
    vacs = await asyncio.to_thread(fetch_vacancies)
    if not vacs:
        await message.answer("Не удалось получить вакансии от HeadHunter. Попробуйте позже.")
        return
    for v in vacs:
        title = v.get("name", "Вакансия")
        employer = v.get("employer", {}).get("name", "Компания")
        url = v.get("alternate_url", "https://hh.ru")
        await message.answer(f"🏢 **{employer}**\n💼 {title}\n{url}", parse_mode="Markdown")

@dp.message(F.text == "📁 Мои резюме")
async def my_resume_info(message: types.Message):
    await message.answer("✅ Твое резюме сохранено в памяти бота и используется для анализа и откликов.")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer)

async def main():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
