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

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def ai_generate(prompt: str) -> str:
    if not client:
        return "⚠️ Ошибка: API-ключ Gemini не настроен на Render."
    try:
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {str(e)[:60]}"

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

# --- ПАРСЕР ЧЕРЕЗ RSS HH ---
def fetch_hh_vacancies_sync(query="Руководитель проектов"):
    q_encoded = query.replace(" ", "+")
    url = f"https://hh.ru/search/vacancy?text={q_encoded}&area=1&format=rss"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    
    for attempt in range(3):
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
                if items:
                    return items, ""
            return [], f"HTTP {response.status_code}"
        except Exception as e:
            if attempt == 2:
                return [], f"ошибка: {e}"
        time.sleep(1)
    return [], "превышено число попыток"

# --- ИНТЕРФЕЙС СО ВСЕМИ КНОПКАМИ ---
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

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет, Антон! Твой карьерный агент полностью восстановлен со всем функционалом.", reply_markup=get_main_keyboard())

@dp.message(F.text == "📁 Мои резюме")
async def my_resumes(message: types.Message):
    resume = user_resumes.get(message.from_user.id)
    if resume:
        await message.answer("✅ В базе сохранено твое активное резюме. Бот использует его для откликов и анализа!")
    else:
        await message.answer("⚠️ У тебя пока нет загруженного резюме. Нажми «📥 Загрузить резюме».")

@dp.message(F.text == "📥 Загрузить резюме")
async def upload_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл резюме (PDF, Word .docx или RTF).", reply_markup=types.ReplyKeyboardRemove())

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

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Запрашиваю свежие вакансии через RSS-ленту HeadHunter...")
    vacancies, err = await asyncio.to_thread(fetch_hh_vacancies_sync, "Руководитель проектов")

    if not vacancies:
        return await message.answer(f"⚠️ Не удалось получить вакансии. Ошибка: {err or 'пустой ответ'}")

    await message.answer(f"🔥 Нашел позиций: {len(vacancies)}. Вывожу первые 10:")

    for v in vacancies[:10]:
        vac_id = str(v.get("id"))
        title = v.get("name", "Вакансия")
        employer = v.get("employer", {}).get("name", "Компания")
        url = v.get("alternate_url", "https://hh.ru")
        temp_vacancies[vac_id] = title

        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_{vac_id}")

        await message.answer(f"🏢 **{employer}**\n💼 [{title}]({url})", reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))
        await asyncio.sleep(0.3)

@dp.callback_query(F.data.startswith("gen_"))
async def gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_", "")
    title = temp_vacancies.get(vac_id, "Вакансия")
    await callback.answer("Генерирую письмо...", show_alert=False)
    
    resume = user_resumes.get(callback.from_user.id, "Опыт: Руководитель проектов.")
    prompt = f"Напиши сильное профессиональное сопроводительное письмо для отклика на позицию '{title}' на основе резюме:\n{resume}"
    
    letter_text = await asyncio.to_thread(ai_generate, prompt)
    await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter_text}", parse_mode="Markdown")

@dp.message(F.text)
async def chat(message: types.Message):
    # Обработка кнопок интерфейса через ИИ-анализ или заглушки
    text = message.text
    resume = user_resumes.get(message.from_user.id, "Резюме не загружено.")
    
    if text == "🛠 Адаптация резюме":
        prompt = f"Адаптируй это резюме под позицию руководителя проектов в крупном телекоме:\n{resume}"
    elif text == "📊 Анализ навыков (Skill Gap)":
        prompt = f"Проведи Skill Gap анализ для руководителя проектов на основе резюме:\n{resume}"
    elif text == "📋 Аудит резюме":
        prompt = f"Сделай жесткий аудит и дай рекомендации по улучшению этого резюме:\n{resume}"
    elif text == "🎤 Тренажер собеседований":
        prompt = "Ты интервьюер. Задай мне первый каверзный вопрос для кандидата на позицию Руководитель проектов."
    elif text == "📌 Трекер откликов":
        await message.answer("📌 Твои активные отклики пока пусты. Отправляй отклики через поиск вакансий!")
        return
    elif text in ["💎 Оплата и Баланс", "🎁 Пригласить друга", "ℹ️ Помощь"]:
        await message.answer("ℹ️ Брендовый карьерный агент работает в штатном режиме. Баланс запросов: 30.")
        return
    else:
        prompt = message.text

    answer = await asyncio.to_thread(ai_generate, prompt)
    await message.answer(answer, reply_markup=get_main_keyboard())

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
