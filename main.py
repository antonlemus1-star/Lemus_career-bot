import asyncio
import os
import sqlite3
import time

import requests
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

import google.generativeai as genai
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
HH_PROXY = os.getenv("HH_PROXY")  # опционально: http://login:pass@host:port (если HH блочит IP)
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- АВТОПОДБОР ЖИВОЙ МОДЕЛИ GEMINI ---
GEMINI_MODEL_CANDIDATES = [
    "gemini-flash-latest",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

ai_model = None
ai_model_name = None

def pick_ai_model():
    global ai_model, ai_model_name
    if not GEMINI_API_KEY:
        print("Gemini: НЕТ GEMINI_API_KEY в переменных окружения!")
        return None
    genai.configure(api_key=GEMINI_API_KEY)
    for name in GEMINI_MODEL_CANDIDATES:
        try:
            m = genai.GenerativeModel(name)
            m.generate_content("Ответь одним словом: готов")
            ai_model, ai_model_name = m, name
            print(f"Gemini: активна модель {name}")
            return m
        except Exception as e:
            print(f"Gemini: модель {name} недоступна: {e}")
    ai_model, ai_model_name = None, None
    return None

ai_model = pick_ai_model()

def ai_generate(prompt: str) -> str:
    """Генерация с авто-восстановлением, если модель снова отключат."""
    global ai_model
    if ai_model is None:
        pick_ai_model()
    if ai_model is None:
        return "⚠️ ИИ не инициализирован: проверь GEMINI_API_KEY на Render."
    for _ in range(2):
        try:
            return ai_model.generate_content(prompt).text
        except Exception as e:
            print(f"Gemini ошибка: {e} — переподбираю модель")
            pick_ai_model()
            if ai_model is None:
                return f"⚠️ Ошибка ответа ИИ: {e}"
    return "⚠️ ИИ временно недоступен, попробуй через минуту."

USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)
user_resumes = {}
temp_vacancies = {}

# --- БАЗА ДАННЫХ (CRM) ---
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

# --- ПАРСЕР HH API ---
HH_HEADERS = {
    "User-Agent": "LemusCareerBot/1.0 (anton@megafon.ru)",
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
PROXIES = {"http": HH_PROXY, "https": HH_PROXY} if HH_PROXY else None

def fetch_hh_vacancies_sync(query="Руководитель проектов"):
    url = "https://api.hh.ru/vacancies"
    params = {
        "text": query,
        "area": "1", # 1 = Москва
        "per_page": "50",
        "page": "0",
        "order_by": "publication_time",
    }
    last_error = ""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=HH_HEADERS, timeout=15, proxies=PROXIES)
            if r.status_code == 200:
                return r.json().get("items", []), ""
            last_error = f"HTTP {r.status_code}: {r.text[:150]}"
            if r.status_code == 400 and "order_by" in params:
                del params["order_by"]
                continue
            if r.status_code == 403:
                last_error += " | HH блокирует IP. Задай HH_PROXY."
        except Exception as e:
            last_error = f"сетевая ошибка: {e}"
        time.sleep(2 * (attempt + 1))
    return [], last_error

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
    model_info = f" (ИИ: {ai_model_name})" if ai_model_name else ""
    await message.answer(f"👋 Привет! Я твой карьерный агент. База запущена{model_info}!",
                         reply_markup=get_main_keyboard())

@dp.message(F.text == "📁 Мои резюме")
async def my_resumes(message: types.Message):
    resume = user_resumes.get(message.from_user.id)
    if resume:
        await message.answer("✅ В базе сохранено твое активное резюме. Бот использует его для написания откликов!")
    else:
        await message.answer("⚠️ У тебя пока нет загруженного резюме. Нажми «📥 Загрузить резюме».")

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Собираю самые свежие вакансии «Руководитель проектов» по всем сферам...")

    vacancies, err = await asyncio.to_thread(fetch_hh_vacancies_sync, "Руководитель проектов")

    if not vacancies:
        return await message.answer(
            f"⚠️ Не удалось получить вакансии. Диагноз: {err or 'пустой ответ HH'}. "
            "Если это 403 — HH блокирует IP сервера, задай в Render переменную HH_PROXY."
        )

    # Берем 15 самых свежих вакансий из любой сферы, чтобы не заспамить чат
    top_vacancies = vacancies[:15]
    await message.answer(f"🔥 Нашел {len(vacancies)} свежих позиций. Вывожу топ-{len(top_vacancies)} последних:")

    for v in top_vacancies:
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
    
    # Сохраняем отклик в CRM (Базу данных)
    cursor.execute('INSERT INTO applications (user_id, company_name, status) VALUES (?, ?, ?)', 
                   (callback.from_user.id, title, "Сгенерировано письмо"))
    conn.commit()

    try:
        await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{letter_text}\n\n_✅ Отклик добавлен в трекер!_", parse_mode="Markdown")
    except Exception:
        await callback.message.answer(f"📝 Сопроводительное письмо:\n\n{letter_text}\n\n✅ Отклик добавлен в трекер!")

@dp.message(F.text == "📌 Трекер откликов")
async def track_applications(message: types.Message):
    cursor.execute("SELECT company_name, status FROM applications WHERE user_id = ?", (message.from_user.id,))
    apps = cursor.fetchall()
    
    if not apps:
        return await message.answer("📭 Твой трекер пока пуст. Найди вакансию и сгенерируй сопроводительное письмо, чтобы оно появилось здесь!")

    text = "📌 **История твоих откликов:**\n\n"
    # Показываем 15 последних записей
    for i, (company, status) in enumerate(apps[-15:], 1):
        text += f"{i}. **{company}** — {status}\n"
        
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text)
async def chat(message: types.Message):
    answer = await asyncio.to_thread(ai_generate, message.text)
    await message.answer(answer, reply_markup=get_main_keyboard())

async def main():
    await start_web_server()
    print("Бот и веб-сервер запущены!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())