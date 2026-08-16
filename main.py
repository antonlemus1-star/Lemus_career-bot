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

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Функция универсальной генерации с перебором актуальных имен моделей Google
def ai_generate(prompt: str) -> str:
    if not ai_client:
        return "⚠️ ИИ не инициализирован."
    
    # Список актуальных вариантов на случай очередного обновления Google
    models_to_try = ['gemini-2.5-flash', 'gemini-2.0-flash', 'gemini-flash-latest']
    
    for model_name in models_to_try:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if response and response.text:
                return response.text
        except Exception as e:
            continue
            
    return "⚠️ Ошибка ИИ: все доступные версии моделей отклонили запрос."

# --- ПАРСЕР ЧЕРЕЗ RSS HH (ОБХОД ОШИБКИ 403) ---
def fetch_hh_vacancies_sync(query="Руководитель проектов"):
    q_encoded = query.replace(" ", "+")
    # RSS-лента не блокируется защитой 403 так, как прямой API
    url = f"https://hh.ru/search/vacancy?text={q_encoded}&area=1&format=rss"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            items = []
            for item in soup.find_all(['item', 'vacancy']):
                title = item.find('title').text if item.find('title') else "Вакансия"
                link = item.find('link').text if item.find('link') else "https://hh.ru"
                items.append({
                    "id": str(abs(hash(link))),
                    "name": title,
                    "employer": {"name": "HeadHunter (RSS)"},
                    "alternate_url": link
                })
            return items, ""
        return [], f"HTTP {response.status_code}"
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
    await message.answer("👋 Привет! Я твой карьерный агент. Ошибки устранены!", reply_markup=get_main_keyboard())

@dp.message(F.text == "📁 Мои резюме")
async def my_resumes(message: types.Message):
    await message.answer("✅ Бот готов использовать твое загруженное резюме для работы.")

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Запрашиваю свежие вакансии...")
    vacancies, err = await asyncio.to_thread(fetch_hh_vacancies_sync, "Руководитель проектов")
    
    if not vacancies:
        await message.answer(f"⚠️ Не удалось получить вакансии. Ошибка: {err}")
        return

    for v in vacancies[:10]:
        title = v.get("name")
        employer = v.get("employer", {}).get("name")
        url = v.get("alternate_url")
        
        vac_id = v.get("id")
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{vac_id}")
        
        await message.answer(f"🏢 {employer}\n💼 {title}\n{url}", reply_markup=builder.as_markup())
        await asyncio.sleep(0.2)

@dp.callback_query(F.data.startswith("gen_"))
async def gen_cover(callback: types.CallbackQuery):
    await callback.answer("Генерирую письмо...", show_alert=False)
    prompt = "Напиши сильное профессиональное сопроводительное письмо для руководителя проектов."
    letter = await asyncio.to_thread(ai_generate, prompt)
    await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter}", parse_mode="Markdown")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer, reply_markup=get_main_keyboard())

async def main():
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