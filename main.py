import asyncio
import logging
import os
import sqlite3
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = 'gemini-2.0-flash'

# --- БД ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS dislikes (user_id INTEGER, vacancy_title TEXT)')
conn.commit()

user_resumes = {}

# --- ПАРСИНГ ПО КЛЮЧЕВИКУ ---
async def fetch_hh_vacancies(query: str):
    url = "https://api.hh.ru/vacancies"
    params = {"text": query, "area": 1, "per_page": 5, "period": 30}
    headers = {"User-Agent": "LemusCareerBot/1.0"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("items", [])
        except Exception: 
            pass
    return []

# --- ИИ ---
def call_gemini(prompt: str):
    try:
        res = ai_client.models.generate_content(model=MODEL, contents=prompt)
        return res.text
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {e}"

# --- КЛАВИАТУРА ---
def get_main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме"); b.button(text="📤 Загрузить")
    b.button(text="🔍 Поиск вакансий"); b.button(text="🛠 Адаптация резюме")
    b.button(text="✍️ Отклик"); b.button(text="📊 Skill Gap")
    b.button(text="📋 Аудит резюме"); b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов"); b.button(text="💎 Оплата и Баланс")
    b.button(text="🎁 Пригласить друга"); b.button(text="ℹ️ Помощь")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    if not cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (message.from_user.id,)).fetchone():
        cursor.execute('INSERT INTO users (user_id) VALUES (?)', (message.from_user.id,))
        conn.commit()
    await message.answer("👋 Привет! Я твой карьерный агент. Загрузи резюме и начни поиск.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_menu(message: types.Message):
    # Предлагаем выбрать целевую должность/направление для поиска
    builder = InlineKeyboardBuilder()
    builder.button(text="👔 Руководитель проектов", callback_data="search_pos:Руководитель проектов")
    builder.button(text="📈 Руководитель направления", callback_data="search_pos:Руководитель направления")
    builder.button(text="💼 Руководитель отдела продаж", callback_data="search_pos:Руководитель отдела продаж")
    builder.button(text="🚀 Развитие бизнеса (BDM)", callback_data="search_pos:Business Development")
    builder.adjust(1)
    await message.answer("🎯 Выбери желаемую должность для поиска вакансий:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("search_pos:"))
async def execute_search(callback: types.CallbackQuery):
    position = callback.data.replace("search_pos:", "")
    await callback.message.edit_text(f"🔍 Ищу вакансии по роли: **{position}**...", parse_mode="Markdown")
    
    vacs = await fetch_hh_vacancies(position)
    
    if not vacs:
        await callback.message.answer(f"По запросу «{position}» ничего не найдено.")
        return

    for v in vacs:
        name = v.get('name', 'Без названия')
        url = v.get('alternate_url', '')
        employer = v.get('employer', {}).get('name', 'Компания')
        
        builder = InlineKeyboardBuilder()
        builder.button(text="👎 Мимо", callback_data=f"disl_{name[:20]}")
        await callback.message.answer(f"🏢 **{employer}**\n💼 [{name}]({url})", reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))

@dp.callback_query(F.data.startswith("disl_"))
async def dislike(callback: types.CallbackQuery):
    title = callback.data.replace("disl_", "")
    cursor.execute('INSERT INTO dislikes VALUES (?, ?)', (callback.from_user.id, title))
    conn.commit()
    await callback.message.edit_text(f"❌ Вакансия «{title}» скрыта.")

@dp.message(F.text == "📁 Мои резюме")
async def list_resumes(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("📂 Список резюме пуст.")
    await message.answer("📂 Твои резюме:\n" + "\n".join([f"• {n}" for n in resumes.keys()]))

@dp.message(F.text == "📤 Загрузить")
async def upload_resume_start(message: types.Message):
    await message.answer("📄 Отправь файл резюме (PDF или .docx).", reply_markup=types.ReplyKeyboardRemove())

@dp.message(F.document)
async def process_resume_document(message: types.Message):
    doc = message.document
    if not (doc.file_name.endswith('.pdf') or doc.file_name.endswith('.docx')): 
        return await message.answer("⚠️ Только PDF или Word!")
    path = f"temp_{message.from_user.id}_{doc.file_name}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    try: 
        text = "".join([p.extract_text() or "" for p in PdfReader(path).pages]) if doc.file_name.endswith('.pdf') else "\n".join([p.text for p in Document(path).paragraphs])
    except Exception as e:
        if os.path.exists(path): os.remove(path)
        return await message.answer(f"⚠️ Ошибка чтения: {e}")
    
    user_resumes.setdefault(message.from_user.id, {})[doc.file_name] = text
    if os.path.exists(path): os.remove(path)
    await message.answer(f"✅ Успешно сохранено: {doc.file_name}", reply_markup=get_main_keyboard())

@dp.message(F.text == "📌 Трекер откликов")
async def tracker(message: types.Message):
    rows = cursor.execute('SELECT company_name, status FROM applications WHERE user_id = ?', (message.from_user.id,)).fetchall()
    await message.answer("📌 Твои отклики:\n" + "\n".join([f"{r[0]} — {r[1]}" for r in rows]) or "В трекере пока пусто.")

@dp.message(Command("adminlemus71"))
async def admin(message: types.Message):
    if str(message.from_user.id) == str(ADMIN_ID):
        await message.answer("👑 Админ-панель активна.")

@dp.message(F.text)
async def chat_handler(message: types.Message):
    await message.answer(call_gemini(message.text))

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())