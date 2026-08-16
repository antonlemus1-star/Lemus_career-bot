import asyncio
import os
import sqlite3
import requests
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from groq import Groq
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
MODEL_NAME = 'llama3-70b-8192' # Отличная модель для работы с русским языком

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

# --- БРОНЕБОЙНЫЙ ПАРСЕР HH API ---
def fetch_hh_vacancies_sync(query):
    url = "https://api.hh.ru/vacancies"
    headers = {
        "User-Agent": "LemusCareerBot/1.0 (anton@megafon.ru)"
    }
    params = {
        "text": query,
        "area": "1", # 1 = Москва
        "per_page": "5",
        "page": "0",
        "order_by": "publication_time"
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("items", [])
        else:
            print(f"Ошибка HH API: {response.status_code}")
    except Exception as e:
        print(f"Сетевая ошибка при поиске: {e}")
    return []

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
    await message.answer("👋 Привет! Я твой карьерный агент. Меню обновлено:", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    user_id = message.from_user.id
    resume_text = user_resumes.get(user_id)
    
    search_query = "Руководитель проектов" # Запрос по умолчанию
    
    # УМНЫЙ ПОИСК ЧЕРЕЗ GROQ
    if resume_text and ai_client:
        await message.answer("🧠 Анализирую твой опыт в резюме для подбора максимально релевантных вакансий...")
        prompt = (
            "Проанализируй текст резюме и выдели ключевую экспертизу кандидата. "
            "Сформируй идеальный поисковый запрос для сайта вакансий (HeadHunter), "
            "чтобы найти наиболее подходящие должности. Запрос должен состоять максимум из 3-4 слов "
            "(например: 'Руководитель проектов B2B' или 'Директор по продажам телеком'). "
            "Верни ТОЛЬКО текст запроса, без кавычек, точек и пояснений.\n\n"
            f"Резюме:\n{resume_text[:3000]}"
        )
        try:
            chat_completion = ai_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=MODEL_NAME,
            )
            if chat_completion.choices[0].message.content:
                search_query = chat_completion.choices[0].message.content.strip().replace('"', '').replace("'", "")
        except Exception as e:
            print(f"Ошибка Groq при генерации запроса: {e}")
            
    await message.answer(f"🔍 Ищу вакансии по запросу: *{search_query}*...", parse_mode="Markdown")
    
    vacancies = await asyncio.to_thread(fetch_hh_vacancies_sync, search_query)
    
    if not vacancies:
        return await message.answer(f"По запросу «{search_query}» ничего не найдено. Попробуй чуть позже.")
    
    await message.answer(f"🔥 Нашел свежие позиции под твой опыт ({len(vacancies)}):")
    
    for v in vacancies:
        vac_id = str(v.get("id"))
        title = v.get("name", "Вакансия")
        employer = v.get("employer", {}).get("name", "Компания")
        url = v.get("alternate_url", "https://hh.ru")
        
        temp_vacancies[vac_id] = title
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{vac_id}")
        
        text = f"🏢 **{employer}**\n💼 [{title}]({url})"
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))

@dp.message(F.text == "📥 Загрузить резюме")
async def upload_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл резюме (PDF, Word или RTF).", reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_file(message: types.Message, state: FSMContext):
    doc = message.document
    path = f"tmp_{message.from_user.id}_{doc.file_name}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    
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
    await message.answer(f"✅ Резюме «{doc.file_name}» успешно сохранено! Теперь поиск вакансий будет адаптирован под твой опыт.", reply_markup=get_main_keyboard())
    
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
    
    if ai_client:
        try:
            chat_completion = ai_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model=MODEL_NAME,
            )
            letter_text = chat_completion.choices[0].message.content
        except Exception as e:
            letter_text = f"Ошибка при генерации письма через Groq: {e}"
    else:
        letter_text = "⚠️ ИИ-клиент Groq не инициализирован."
        
    await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter_text}", parse_mode="Markdown")

@dp.message(F.text)
async def chat(message: types.Message):
    if ai_client:
        try:
            chat_completion = ai_client.chat.completions.create(
                messages=[{"role": "user", "content": message.text}],
                model=MODEL_NAME,
            )
            answer = chat_completion.choices[0].message.content
        except Exception as e:
            answer = f"Ошибка Groq: {e}"
    else:
        answer = "ИИ временно недоступен."
    await message.answer(answer, reply_markup=get_main_keyboard())

async def main():
    await start_web_server()
    print("Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())