import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from pypdf import PdfReader
from docx import Document

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = 'gemini-2.0-flash'

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 30)')
cursor.execute('CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_name TEXT, status TEXT)')
conn.commit()

user_resumes = {}
temp_vacancies = {}

# --- ПАРСИНГ SELENIUM ---
def fetch_vacancies_via_browser():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    vacancies = []
    try:
        # Широкий запрос для гарантированного результата
        url = "https://hh.ru/search/vacancy?text=Руководитель+проектов+OR+Руководитель+отдела+продаж+OR+Руководитель+направления&area=1&order_by=publication_time"
        driver.get(url)
        time.sleep(5)
        items = driver.find_elements(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy']")
        for item in items[:10]:
            try:
                title = item.find_element(By.CSS_SELECTOR, "[data-qa='serp-item__title']").text
                link = item.find_element(By.CSS_SELECTOR, "[data-qa='serp-item__title']").get_attribute("href")
                employer = item.find_element(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy-employer']").text
                vac_id = str(time.time()) + str(hash(title))
                vacancies.append({"name": title, "employer": employer, "url": link, "id": vac_id})
            except: continue
    finally: driver.quit()
    return vacancies

# --- ИНТЕРФЕЙС ---
def get_main_keyboard():
    b = ReplyKeyboardBuilder()
    b.button(text="📁 Мои резюме"); b.button(text="📤 Загрузить")
    b.button(text="🔍 Поиск вакансий"); b.button(text="🛠 Адаптация резюме")
    b.button(text="✍️ Отклик"); b.button(text="📊 Skill Gap")
    b.button(text="📋 Аудит резюме"); b.button(text="🎤 Тренажер собеседований")
    b.button(text="📌 Трекер откликов"); b.button(text="💎 Оплата и Баланс")
    b.button(text="🎁 Пригласить друга"); b.button(text="ℹ️ Помощь")
    b.adjust(2, 2, 2, 2, 1, 2, 1)
    return b.as_markup(resize_keyboard=True)

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Привет! Я твой карьерный агент. Выбирай функцию:", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def search_vacancies(message: types.Message):
    await message.answer("🔍 Ищу актуальные вакансии...")
    vacancies = await asyncio.to_thread(fetch_vacancies_via_browser)
    if not vacancies: return await message.answer("Ничего не найдено.")
    
    for v in vacancies:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное", callback_data=f"gen_{v['id']}")
        temp_vacancies[v['id']] = v
        await message.answer(f"🏢 {v['employer']}\n💼 [{v['name']}]({v['url']})", 
                             reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(F.text == "📤 Загрузить")
async def upload_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл резюме (PDF/Docx).")

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_file(message: types.Message, state: FSMContext):
    doc = message.document
    path = f"tmp_{doc.file_name}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    # Чтение
    text = ""
    if doc.file_name.endswith('.pdf'): text = "".join([p.extract_text() for p in PdfReader(path).pages])
    elif doc.file_name.endswith('.docx'): text = "\n".join([p.text for p in Document(path).paragraphs])
    
    user_resumes[message.from_user.id] = text
    await message.answer("✅ Резюме успешно сохранено!")
    if os.path.exists(path): os.remove(path)
    await state.clear()

@dp.callback_query(F.data.startswith("gen_"))
async def gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_", "")
    v = temp_vacancies.get(vac_id)
    if not v: return await callback.answer("Ошибка.")
    
    resume = user_resumes.get(callback.from_user.id, "Опыт: Руководитель проектов.")
    prompt = f"Напиши сопроводительное письмо для вакансии {v['name']} на основе резюме: {resume}"
    res = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt)
    await callback.message.answer(f"📝 {res.text}")

@dp.message(F.text)
async def chat(message: types.Message):
    res = ai_client.models.generate_content(model=MODEL_NAME, contents=message.text)
    await message.answer(res.text)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())