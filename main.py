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

# --- ИНИЦИАЛИЗАЦИЯ GEMINI ЧЕРЕЗ НОВЫЙ SDK ---
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = 'gemini-2.5-flash'

def ai_generate(prompt: str) -> str:
    if not ai_client:
        return "⚠️ ИИ не инициализирован: проверь GEMINI_API_KEY на Render."
    try:
        response = ai_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Gemini ошибка: {e}")
        return f"⚠️ Ошибка ответа ИИ: {e}"

USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)
user_resumes = {}
temp_vacancies = {}

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_name TEXT, status TEXT)')
conn.commit()

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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

# --- НАДЕЖНЫЙ ПАРСЕР ЧЕРЕЗ BEAUTIFULSOUP ---
def fetch_hh_vacancies_sync(query="Руководитель проектов"):
    q_encoded = query.replace(" ", "+")
    url = f"https://hh.ru/search/vacancy?text={q_encoded}&area=1&enable_snippets=true&ored_clusters=true&search_field=name&no_magic=true&items_on_page=50&format=rss"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 200:
                # Используем BeautifulSoup вместо строгого XML-парсера
                soup = BeautifulSoup(response.content, 'html.parser')
                items = []
                
                for item in soup.find_all('item'):
                    title_elem = item.find('title')
                    link_elem = item.find('link')
                    
                    title = title_elem.text if title_elem else "Вакансия"
                    link = link_elem.text if link_elem else "https://hh.ru"
                    
                    items.append({
                        "id": str(abs(hash(link))),
                        "name": title,
                        "employer": {"name": "HeadHunter (RSS)"},
                        "alternate_url": link
                    })
                return items, ""
            return [], f"HTTP {response.status_code}"
        except Exception as e:
            if attempt == 2:
                return [], f"ошибка парсинга RSS: {e}"
        time.sleep(2 * (attempt + 1))
    return [], "превышено число попыток"

# --- ИНТЕРФЕЙС ---
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

# --- ХЕНДЛЕРЫ БОТА ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет! Я твой карьерный агент. Парсер обновлен!", reply_markup=get_main_keyboard())

@dp.message(F.text == "📁 Мои резюме")
async def my_resumes(message: types.Message):
    resume = user_resumes.get(message.from_user.id)
    if resume:
        await message.answer("✅ В базе сохранено твое активное резюме. Бот использует его для написания откликов!")
    else:
        await message.answer("⚠️ У тебя пока нет загруженного резюме. Нажми «📥 Загрузить резюме».")

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Запрашиваю свежие вакансии через RSS-ленту HeadHunter...")

    vacancies, err = await asyncio.to_thread(fetch_hh_vacancies_sync, "Руководитель проектов")

    if not vacancies:
        return await message.answer(f"⚠️ Не удалось получить вакансии. Ошибка: {err or 'пустой ответ'}")

    keywords = ["b2b", "телеком", "telecom", "интеграци", "связь", "it", "ит ", "ит-", "продукт", "product", "развити", "инфраструктур", "тех", "tech"]
    relevant_vacancies = []

    for v in vacancies:
        text_to_check = (v.get("name", "") + " " + v.get("employer", {}).get("name", "")).lower()
        if any(k in text_to_check for k in keywords):
            relevant_vacancies.append(v)

    if len(relevant_vacancies) == 0:
        relevant_vacancies = vacancies[:10]
        await message.answer("⚠️ Строгих совпадений по ключевым словам не найдено. Вывожу 10 самых свежих позиций:")
    else:
        await message.answer(f"🔥 Нашел {len(relevant_vacancies)} релевантных позиций:")

    for v in relevant_vacancies:
        vac_id = str(v.get("id"))
        title = v.get("name", "Вакансия")
        employer = v.get("employer", {}).get("name", "Компания")
        url = v.get("alternate_url", "https://hh.ru")

        temp_vacancies[vac_id] = title

        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{vac_id}")

        try:
            text = f"🏢 **{employer}**\n💼 [{title}]({url})"
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown",
                                 link_preview_options=types.LinkPreviewOptions(is_disabled=True))
        except Exception:
            text = f"🏢 {employer}\n💼 {title}\n{url}"
            await message.answer(text, reply_markup=builder.as_markup(),
                                 link_preview_options=types.LinkPreviewOptions(is_disabled=True))
        await asyncio.sleep(0.3)

@dp.message(F.text == "📥 Загрузить резюме")
async def upload_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл резюме (PDF, Word или RTF).", reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_file(message: types.Message, state: FSMContext):
    doc = message.document
    path = f"tmp_{message.from_user.id}_{doc.file_name}"
    await bot.download(doc, destination=path)

    text = ""
    try:
        if doc.file_name.endswith('.pdf'):
            text = "".join([p.extract_text() or "" for p in PdfReader(path).pages])
        elif doc.file_name.endswith('.docx'):
            text = "\n".join([p.text for p in Document(path).paragraphs])
        elif doc.file_name.endswith('.rtf'):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
    except Exception as e:
        text = f"Ошибка чтения: {e}"

    user_resumes[message.from_user.id] = text
    await message.answer(f"✅ Резюме «{doc.file_name}» успешно сохранено!", reply_markup=get_main_keyboard())

    if os.path.exists(path):
        os.remove(path)
    await state.clear()

@dp.callback_query(F.data.startswith("gen_"))
async def gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_", "")
    title = temp_vacancies.get(vac_id, "Вакансия")

    await callback.answer("Генерирую письмо...", show_alert=False)
    resume = user_resumes.get(callback.from_user.id, "Опыт: Руководитель проектов.")

    prompt = f"Напиши сильное профессиональное сопроводительное письмо для отклика на позицию '{title}' на основе резюме:\n{resume}"

    letter_text = await asyncio.to_thread(ai_generate, prompt)

    try:
        await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter_text}", parse_mode="Markdown")
    except Exception:
        await callback.message.answer(f"📝 Сопроводительное письмо:\n\n{letter_text}")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer, reply_markup=get_main_keyboard())

async def main():
    await start_web_server()
    print("Бот и веб-сервер запущены!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())