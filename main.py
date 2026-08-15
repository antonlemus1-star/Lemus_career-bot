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

# ================= ОГРАНИЧИТЕЛИ НАГРУЗКИ =================
selenium_semaphore = asyncio.Semaphore(1)
lm_studio_semaphore = asyncio.Semaphore(1)
# =========================================================

session = AiohttpSession(proxy="http://127.0.0.1:10809")
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL_NAME = 'gemini-2.0-flash'

USER_VACANCY_STORAGE = {}
user_resumes = {}

# Базовые резюме по умолчанию
RESUME_PROJECTS = """
Лемус Антон. Желаемая должность: Руководитель проектов. 
Опыт работы 13 лет 9 месяцев.
Ключевые компетенции: Эксперт в построении и развитии процессов продаж, управлении RM, развитии клиентского портфеля и достижении планов. Руководитель по развитию бизнеса с более чем 13 годами опыта в B2B-продажах и запуске IT-продуктов в телеком и IT-компаниях.
"""

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ПУТИ ---
def get_user_resume_path(user_id):
    return os.path.join(USER_DATA_DIR, f"resume_{user_id}.txt")

def get_user_seen_path(user_id):
    return os.path.join(USER_DATA_DIR, f"seen_{user_id}.txt")

def get_user_csv_path(user_id):
    return os.path.join(USER_DATA_DIR, f"vacancies_{user_id}.csv")

def get_user_pref_path(user_id):
    return os.path.join(USER_DATA_DIR, f"preferences_{user_id}.json")

def load_seen_ids(user_id):
    path = get_user_seen_path(user_id)
    if not os.path.exists(path): return set()
    with open(path, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def mark_as_seen(user_id, vac_id):
    path = get_user_seen_path(user_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{vac_id}\n")

def save_to_user_csv(user_id, title, employer, url):
    csv_path = get_user_csv_path(user_id)
    file_exists = os.path.isfile(csv_path)
    with open(csv_path, mode='a', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, delimiter=';')
        if not file_exists:
            writer.writerow(['Должность', 'Компания', 'Ссылка'])
        writer.writerow([title, employer, url])

def load_user_preferences(user_id):
    path = get_user_pref_path(user_id)
    if not os.path.exists(path): return {"liked": [], "disliked": []}
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return {"liked": [], "disliked": []}

def save_user_preference(user_id, title, employer, is_positive):
    path = get_user_pref_path(user_id)
    prefs = load_user_preferences(user_id)
    entry = f"{title} ({employer})"
    if is_positive:
        if entry not in prefs["liked"]:
            prefs["liked"].append(entry)
            if len(prefs["liked"]) > 10: prefs["liked"].pop(0)
    else:
        if entry not in prefs["disliked"]:
            prefs["disliked"].append(entry)
            if len(prefs["disliked"]) > 10: prefs["disliked"].pop(0)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(prefs, f, ensure_ascii=False, indent=4)

# --- ПАРСЕР ЧЕРЕЗ SELENIUM ---
def fetch_vacancies_via_browser():
    print("🤖 [Selenium] Запуск браузера для сбора до 200 вакансий (4 стр. по 50)...")
    options = Options()
    # options.add_argument("--headless")  
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    vacancies = []
    seen_titles_employers = set()

    try:
        for page in range(4):
            url = (
                f"https://hh.ru/search/vacancy?"
                f"text=(%22Руководитель+проектов%22+OR+%22Управление+продажами%22+OR+%22Руководитель+отдела+продаж%22+OR+%22Business+Development+Manager%22+OR+%22Руководитель+направления%22)"
                f"+AND+(МТС+OR+Билайн+OR+МегаФон+OR+Ростелеком+OR+Tele2+OR+Т2+OR+Сбер+OR+ВТБ+OR+%22Альфа-Банк%22+OR+Ozon+OR+Wildberries)"
                f"&area=1&items_on_page=50&page={page}&order_by=publication_time"
            )
            driver.get(url)
            time.sleep(4) 
            items = driver.find_elements(By.CSS_SELECTOR, "div.vacancy-search-item__card")
            if not items:
                items = driver.find_elements(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy']")
            if not items: break

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

                    unique_key = (title.strip().lower(), employer.strip().lower())
                    if unique_key in seen_titles_employers: continue
                    seen_titles_employers.add(unique_key)

                    vacancies.append({
                        "id": vac_id, "name": title, "employer": {"name": employer},
                        "alternate_url": link, "snippet": {"requirement": snippet, "responsibility": ""}
                    })
                except: continue
            time.sleep(2)
    finally:
        driver.quit()
    return vacancies

async def safe_fetch_vacancies():
    async with selenium_semaphore:
        await asyncio.sleep(2)
        return await asyncio.to_thread(fetch_vacancies_via_browser)

# --- ИИ ОЦЕНКА ---
def evaluate_relevance(vacancy_description, vacancy_title, employer_name, user_preferences):
    learned_context = ""
    if user_preferences["liked"]:
        learned_context += f"\nПользователю нравятся: {', '.join(user_preferences['liked'])}."
    if user_preferences["disliked"]:
        learned_context += f"\nПользователь отклонил: {', '.join(user_preferences['disliked'])}."

    prompt = f"""
    Проанализируй вакансию для топ-менеджера в B2B-продажах и управлении проектами.
    Название: {vacancy_title} | Компания: {employer_name}
    Описание: {vacancy_description} {learned_context}
    СТОП-ФАКТОР: Кандидат категорически НЕ рассматривает системы видеонаблюдения (СВН). Если это СВН — отвечай НЕТ.
    Ответь ТОЛЬКО одним словом: ДА или НЕТ.
    """
    payload = {"messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 10, "stream": False}
    try:
        response = requests.post(LM_STUDIO_URL, json=payload, timeout=25)
        return "ДА" in response.json()["choices"][0]["message"]["content"].strip().upper()
    except: return False

async def safe_evaluate_relevance(full_desc, title, employer, user_preferences):
    async with lm_studio_semaphore:
        await asyncio.sleep(1)
        return await asyncio.to_thread(evaluate_relevance, full_desc, title, employer, user_preferences)

# --- FSM И СОСТОЯНИЯ ---
class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    waiting_for_vacancy_adapt = State()
    mock_in_progress = State()
    admin_add_balance_user = State()
    admin_add_balance_amount = State()

# --- КЛАВИАТУРА ИНТЕРФЕЙСА СО ВСЕМИ ФУНКЦИЯМИ ---
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
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    await message.answer("🤖 *Привет! Я твой автономный карьерный AI-агент с парсером вакансий.*", reply_markup=get_main_keyboard(), parse_mode="Markdown")

@dp.message(F.text == "🔍 Поиск вакансий")
async def btn_search(message: types.Message):
    user_id = message.from_user.id
    await message.answer("🔍 Запускаю Selenium-партизан, собираю до 200 вакансий с HH.ru...")
    
    vacancies = await safe_fetch_vacancies()
    if not vacancies:
        return await message.answer("Не удалось собрать вакансии через браузер.")

    seen_ids = load_seen_ids(user_id)
    new_vacancies = [v for v in vacancies if v['id'] not in seen_ids]
    if not new_vacancies:
        return await message.answer("Новых уникальных вакансий пока нет.")

    user_prefs = load_user_preferences(user_id)
    await message.answer(f"🧠 Собрал {len(new_vacancies)} новых вакансий. Оцениваю через LM Studio...")

    relevant_found = 0
    for vac in new_vacancies:
        vac_id = vac['id']
        title = vac.get("name", "")
        employer = vac.get("employer", {}).get("name", "Компания не указана")
        snippet = vac.get("snippet", {}).get("requirement", "")
        full_desc = f"Название: {title}\nКомпания: {employer}\nОписание: {snippet}"
        
        mark_as_seen(user_id, vac_id)
        is_relevant = await safe_evaluate_relevance(full_desc, title, employer, user_prefs)
        
        if is_relevant:
            relevant_found += 1
            url = vac.get("alternate_url")
            save_to_user_csv(user_id, title, employer, url)
            
            text = f"✅ *{title}*\n🏢 {employer}\n\n[Открыть вакансию на HH.ru]({url})"
            builder = InlineKeyboardBuilder()
            builder.button(text="✍️ Сгенерировать письмо", callback_data=f"gen_cover_{vac_id}")
            builder.button(text="👍 То что надо", callback_data=f"fb_good_{vac_id}")
            builder.button(text="👎 Мусор / Мимо", callback_data=f"fb_bad_{vac_id}")
            builder.adjust(1, 2)

            USER_VACANCY_STORAGE[(user_id, vac_id)] = {"desc": full_desc, "title": title, "employer": employer}
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    if relevant_found == 0:
        await message.answer("Анализ завершен. Подходящих под жесткие критерии вакансий не найдено.")
    else:
        await message.answer(f"Готово! Найдено подходящих вакансий: {relevant_found}. Сохранено в CSV.")

@dp.callback_query(F.data.startswith("fb_"))
async def process_feedback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    feedback_type, vac_id = parts[1], parts[2]
    user_id = callback.from_user.id
    vac_info = USER_VACANCY_STORAGE.get((user_id, vac_id))
    if vac_info:
        save_user_preference(user_id, vac_info["title"], vac_info["employer"], (feedback_type == "good"))
        await callback.answer("Спасибо! Учту в будущих подборках.", show_alert=False)
        await callback.message.edit_reply_markup(reply_markup=None)

@dp.callback_query(F.data.startswith("gen_cover_"))
async def process_gen_cover(callback: types.CallbackQuery):
    vac_id = callback.data.replace("gen_cover_", "")
    user_id = callback.from_user.id
    vac_data = USER_VACANCY_STORAGE.get((user_id, vac_id))
    desc = vac_data["desc"] if vac_data else "Описание"

    await callback.answer("Генерирую сопроводительное письмо...", show_alert=False)
    
    # Генерация через облачный Gemini или локальный LM Studio
    prompt = f"Напиши сильное сопроводительное письмо для отклика на вакансию на основе опыта:\n{RESUME_PROJECTS}\n\nВакансия:\n{desc}"
    res = ai_client.models.generate_content(model=MODEL_NAME, contents=prompt) if ai_client else "⚠️ AI недоступен"
    
    await callback.message.answer(f"📝 Сопроводительное письмо:\n\n{res.text}")

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    await message.answer("🤖 Используй кнопку «🔍 Поиск вакансий» для запуска Selenium-парсера до 200 вакансий.")

async def main():
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="search", description="Запустить парсер вакансий")
    ]
    await bot.set_my_commands(commands)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())