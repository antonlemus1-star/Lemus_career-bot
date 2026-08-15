import asyncio
import csv
import json
import os
import time
import requests
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from google import genai
from pypdf import PdfReader
from docx import Document

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")
LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"

USER_DATA_DIR = "user_data"
os.makedirs(USER_DATA_DIR, exist_ok=True)

selenium_semaphore = asyncio.Semaphore(1)
lm_studio_semaphore = asyncio.Semaphore(1)

session = AiohttpSession(proxy="http://127.0.0.1:10809") if os.name != 'posix' else None # На Render прокси не нужен
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = 'gemini-2.0-flash'

USER_VACANCY_STORAGE = {}
user_resumes = {}

# --- УТИЛИТЫ И ПУТИ ---
def get_user_resume_path(user_id):
    return os.path.join(USER_DATA_DIR, f"resume_{user_id}.txt")

def get_user_csv_path(user_id):
    return os.path.join(USER_DATA_DIR, f"vacancies_{user_id}.csv")

def save_to_user_csv(user_id, title, employer, url):
    csv_path = get_user_csv_path(user_id)
    file_exists = os.path.isfile(csv_path)
    with open(csv_path, mode='a', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, delimiter=';')
        if not file_exists:
            writer.writerow(['Должность', 'Компания', 'Ссылка'])
        writer.writerow([title, employer, url])

# --- ПАРСЕР ЧЕРЕЗ SELENIUM ---
def fetch_vacancies_via_browser():
    print("🤖 [Selenium] Запуск браузера для сбора вакансий с HH.ru...")
    options = Options()
    options.add_argument("--headless")  # На сервере (Render) обязательно headless
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    vacancies = []

    try:
        url = (
            f"https://hh.ru/search/vacancy?"
            f"text=(%22Руководитель+проектов%22+OR+%22Руководитель+направления%22+OR+%22Руководитель+отдела+продаж%22+OR+%22Business+Development%22)"
            f"&area=1&items_on_page=20&order_by=publication_time"
        )
        driver.get(url)
        time.sleep(4) 

        items = driver.find_elements(By.CSS_SELECTOR, "div.vacancy-search-item__card")
        if not items:
            items = driver.find_elements(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy']")
              
        for item in items: 
            try:
                title_elem = item.find_element(By.CSS_SELECTOR, "[data-qa='serp-item__title']")
                title = title_elem.text
                link = title_elem.get_attribute("href").split("?")[0]
                vac_id = link.split("/")[-1].split("?")[0]
                
                try:
                    employer = item.find_element(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy-employer']").text
                except:
                    employer = "Компания не указана"

                try:
                    snippet = item.find_element(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy_snippet_requirement']").text
                except:
                    snippet = ""

                vacancies.append({
                    "id": vac_id, "name": title, "employer": {"name": employer},
                    "alternate_url": link, "snippet": {"requirement": snippet}
                })
            except: 
                continue
    except Exception as e:
        print(f"❌ [Selenium] Ошибка парсера: {e}")
    finally:
        driver.quit()
        
    return vacancies

async def safe_fetch_vacancies():
    async with selenium_semaphore:
        await asyncio.sleep(1)
        return await asyncio.to_thread(fetch_vacancies_via_browser)

# --- FSM СОСТОЯНИЯ ---
class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    waiting_for_vacancy_adapt = State()
    mock_in_progress = State()
    admin_add_balance_user = State()
    admin_add_balance_amount = State()

# --- ПОЛНЫЙ ИНТЕРФЕЙС СО ВСЕМИ КНОПКАМИ ---
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📁 Мои резюме")
    builder.button(text="📤 Загрузить")
    builder.button(text="🔍 Поиск вакансий")
    builder.button(text="🛠 Адаптация резюме")
    builder.button(text="✍️ Отклик")
    builder.button(text="📊 Skill Gap")
    builder.button(text="📋 Аудит резюме")
    builder.button(text="🎤 Тренажер собеседований")
    builder.button(text="📌 Трекер откликов")
    builder.button(text="💎 Оплата и Баланс")
    builder.button(text="🎁 Пригласить друга")
    builder.button(text="ℹ️ Помощь")
    builder.adjust(2, 2, 2, 2, 1, 2, 1)
    return builder.as_markup(resize_keyboard=True)

# --- ХЕНДЛЕРЫ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **Привет! Я твой карьерный AI-агент со встроенным парсером.**\n\n"
        "Нажми **«🔍 Поиск вакансий»**, чтобы собрать актуальные позиции с HeadHunter.",
        reply_markup=get_main_keyboard(), parse_mode="Markdown"
    )

@dp.message(F.text == "🔍 Поиск вакансий")
async def btn_search(message: types.Message):
    await message.answer("🔍 Запускаю парсер HeadHunter, собираю свежие вакансии...")
    
    vacancies = await safe_fetch_vacancies()
    
    if not vacancies:
        return await message.answer("Не удалось собрать вакансии. Попробуй позже.")

    await message.answer(f"🔥 Нашел {len(vacancies)} вакансий. Вот свежие результаты:")

    for vac in vacancies[:10]: # Выводим первые 10 актуальных
        vac_id = vac['id']
        title = vac.get("name", "")
        employer = vac.get("employer", {}).get("name", "Компания не указана")
        url = vac.get("alternate_url")
        
        save_to_user_csv(message.from_user.id, title, employer, url)
        
        text = f"✅ *{title}*\n🏢 {employer}\n\n[Открыть вакансию на HH.ru]({url})"
        
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ Сопроводительное письмо", callback_data=f"gen_cover_{vac_id}")
        builder.button(text="👎 Мимо", callback_data=f"disl_{vac_id}")
        builder.adjust(1)

        USER_VACANCY_STORAGE[(message.from_user.id, vac_id)] = {"desc": title, "title": title, "employer": employer}
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("disl_"))
async def process_dislike(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ Вакансия скрыта.")
    await callback.answer()

@dp.callback_query(F.data.startswith("gen_cover_"))
async def process_gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_cover_", "")
    vac_data = USER_VACANCY_STORAGE.get((callback.from_user.id, vac_id))
    title = vac_data["title"] if vac_data else "Вакансия"
    
    await callback.answer("Генерирую письмо...", show_level=False)
    
    prompt = f"Напиши сильное профессиональное сопроводительное письмо для отклика на позицию: {title}."
    res = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt) if ai_client else "AI временно недоступен"
    
    await callback.message.answer(f"📝 **Сопроводительное письмо:**\n\n{res.text}", parse_mode="Markdown")

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    await message.answer("🤖 Используй кнопки меню для поиска вакансий, аудита резюме, адаптации и тренажера.")

async def main():
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="search", description="Запустить поиск вакансий")
    ]
    await bot.set_my_commands(commands)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())