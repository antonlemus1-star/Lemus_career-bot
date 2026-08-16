import asyncio
import os
import sqlite3
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from openai import AsyncOpenAI
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
BASE_URL = "https://api.groq.com/openai/v1"
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

ai_client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
MODEL_NAME = "llama-3.3-70b-versatile"

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

# --- СОСТОЯНИЯ ---
class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    waiting_for_skill_gap_vacancy = State()

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

# --- ПАРСЕР HH API ---
async def fetch_hh_vacancies():
    url = "https://api.hh.ru/vacancies"
    headers = {"User-Agent": "LemusCareerBot/2.0 (antonio.lemus@yandex.ru)"}
    queries = ["Руководитель направления", "Руководитель проектов", "Директор", "Руководитель"]
    
    async with aiohttp.ClientSession() as session:
        for q in queries:
            params = {"text": q, "area": 113, "per_page": 5, "order_by": "publication_time"}
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        if items:
                            return items
            except Exception as e:
                print(f"HH API Error: {e}")
    return []

# --- ИНТЕРФЕЙС ---
def get_main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме"); b.button(text="📤 Загрузить резюме")
    b.button(text="🔍 Поиск вакансий"); b.button(text="🛠 Адаптация резюме")
    b.button(text="✍️ Отклик"); b.button(text="📊 Анализ навыков (Skill Gap)")
    b.button(text="📋 Аудит резюме"); b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов"); b.button(text="💎 Оплата и Баланс")
    b.button(text="🎁 Пригласить друга"); b.button(text="ℹ️ Помощь")
    b.adjust(2, 2, 2, 2, 1, 2, 1)
    return b.as_markup(resize_keyboard=True)

# --- ХЕНДЛЕРЫ БОТА ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет! Я твой карьерный агент. Выбирай нужную функцию в меню:", reply_markup=get_main_keyboard())

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return await message.answer("⛔ У тебя нет доступа к этой команде.")
    
    cursor.execute("SELECT COUNT(*) FROM users")
    users_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM applications")
    apps_count = cursor.fetchone()[0]
    
    await message.answer(
        f"👑 **Панель администратора**\n\n"
        f"👥 Всего пользователей: {users_count}\n"
        f"📌 Всего откликов: {apps_count}",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Ищу актуальные вакансии на HeadHunter...")
    vacancies = await fetch_hh_vacancies()
    
    if not vacancies:
        return await message.answer("Не удалось получить вакансии. Попробуй чуть позже.")
    
    await message.answer(f"🔥 Нашел свежие позиции ({len(vacancies)}):")
    
    for v in vacancies:
        vac_id = str(v.get("id"))
        title = v.get("name", "Вакансия")
        employer = v.get("employer", {}).get("name", "Компания")
        url = v.get("alternate_url", "https://hh.ru")
        
        temp_vacancies[vac_id] = title
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{vac_id}")
        builder.adjust(1)
        
        text = f"🏢 **{employer}**\n💼 [{title}]({url})"
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))

@dp.message(F.text == "📤 Загрузить резюме")
async def upload_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл резюме (поддерживаются PDF, Word (.docx) и RTF).", reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_file(message: types.Message, state: FSMContext):
    doc = message.document
    file_name = doc.file_name.lower()
    if not (file_name.endswith('.pdf') or file_name.endswith('.docx') or file_name.endswith('.rtf')):
        return await message.answer("⚠️ Поддерживаются только форматы PDF, Word (.docx) и RTF!")
        
    path = f"tmp_{message.from_user.id}_{doc.file_name}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    
    text = ""
    try:
        if file_name.endswith('.pdf'):
            text = "".join([p.extract_text() or "" for p in PdfReader(path).pages])
        elif file_name.endswith('.docx'):
            text = "\n".join([p.text for p in Document(path).paragraphs])
        elif file_name.endswith('.rtf'):
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
    except Exception as e:
        text = f"Ошибка чтения: {e}"
        
    user_resumes[message.from_user.id] = text
    await message.answer(f"✅ Резюме «{doc.file_name}» успешно сохранено!", reply_markup=get_main_keyboard())
    
    if os.path.exists(path):
        os.remove(path)
    await state.clear()

@dp.message(F.text == "📊 Анализ навыков (Skill Gap)")
async def skill_gap_start(message: types.Message, state: FSMContext):
    if message.from_user.id not in user_resumes:
        return await message.answer("⚠️ Сначала загрузи свое резюме через кнопку «📤 Загрузить резюме».", reply_markup=get_main_keyboard())
    
    await state.set_state(CareerState.waiting_for_skill_gap_vacancy)
    await message.answer("🎯 Напиши название целевой позиции или вставь описание вакансии, чтобы я провел анализ недостающих навыков (Skill Gap) на русском языке.")

@dp.message(CareerState.waiting_for_skill_gap_vacancy, F.text)
async def skill_gap_process(message: types.Message, state: FSMContext):
    target_info = message.text
    resume = user_resumes.get(message.from_user.id, "")
    
    await message.answer("🔍 Анализирую соответствие навыков...")
    
    prompt = (
        "Проведи глубокий анализ разрыва навыков (Skill Gap Analysis) на русском языке. "
        "Сравни резюме кандидата с требованиями целевой вакансии/позиции.\n\n"
        f"РЕЗЮМЕ КАНДИДАТА:\n{resume}\n\n"
        f"ЦЕЛЕВАЯ ВАКАНСИЯ / ТРЕБОВАНИЯ:\n{target_info}\n\n"
        "Выдай структурированный ответ на русском языке:\n"
        "1. Сильные стороны и совпадения.\n"
        "2. Чего критически не хватает (каких навыков, опыта или технологий).\n"
        "3. Конкретные рекомендации, как закрыть эти пробелы."
    )
    
    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}]
        )
        result_text = response.choices[0].message.content
    except Exception as e:
        result_text = f"⚠️ Ошибка анализа: {e}"
        
    await message.answer(f"📊 **Результаты анализа навыков:**\n\n{result_text}", parse_mode="Markdown", reply_markup=get_main_keyboard())
    await state.clear()

@dp.callback_query(F.data.startswith("gen_"))
async def gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_", "")
    title = temp_vacancies.get(vac_id, "Вакансия")
    
    await callback.answer("Генерирую письмо...", show_alert=False)
    resume = user_resumes.get(callback.from_user.id, "Опыт: Руководитель проектов и направлений в B2B и IT.")
    
    prompt = f"Напиши сильное профессиональное сопроводительное письмо на русском языке для отклика на позицию '{title}' на основе резюме:\n{resume}"
    
    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}]
        )
        letter_text = response.choices[0].message.content
    except Exception as e:
        letter_text = f"⚠️ Ошибка генерации: {e}"
        
    await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter_text}", parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    await message.answer("🤖 Используй кнопки меню для поиска вакансий, загрузки резюме, анализа навыков и генерации откликов.")

@dp.message(F.text)
async def chat(message: types.Message):
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (message.from_user.id,))
    conn.commit()
    
    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": message.text}]
        )
        answer = response.choices[0].message.content
    except Exception as e:
        answer = f"⚠️ Ошибка ИИ: {e}"
    await message.answer(answer, reply_markup=get_main_keyboard())

async def main():
    await start_web_server()
    print("Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())