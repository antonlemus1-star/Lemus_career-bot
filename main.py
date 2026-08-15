import asyncio
import os
import time
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, LabeledPrice, 
    PreCheckoutQuery, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from google import genai

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By

# ================= ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВАШ_ТОКЕН")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "ВАШ_GEMINI_КЛЮЧ")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789")) # ВАШ ID

# ================= ИНИЦИАЛИЗАЦИЯ =================
ai_client = genai.Client(api_key=GEMINI_API_KEY)
AI_MODEL = "gemini-2.5-flash"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
USER_VACANCY_STORAGE = {} # Хранилище спарсенных вакансий

# Ограничители нагрузки на ИИ и Браузер
selenium_semaphore = asyncio.Semaphore(1)
gemini_semaphore = asyncio.Semaphore(2)

# ================= СОСТОЯНИЯ FSM =================
class AppStates(StatesGroup):
    waiting_for_resume_name = State()
    waiting_for_resume_text = State()
    waiting_for_lpr_info = State()
    waiting_for_lpr_vacancy = State()
    waiting_for_adapt_vacancy = State()
    waiting_for_gap_vacancy = State()

# ================= БАЗА ДАННЫХ =================
DB_FILE = "bot_database.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        balance INTEGER DEFAULT 10,
                        total_spent_stars INTEGER DEFAULT 0,
                        joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS user_resumes (
                        resume_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        name TEXT,
                        resume_text TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tariffs (
                        tariff_id TEXT PRIMARY KEY,
                        name TEXT,
                        stars INTEGER,
                        credits INTEGER)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS crm (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        company TEXT,
                        vacancy_title TEXT,
                        status TEXT DEFAULT 'Отправлено',
                        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS user_activity (
                        user_id INTEGER PRIMARY KEY,
                        last_active_date DATE)''')

        cursor = conn.execute("SELECT COUNT(*) FROM tariffs")
        if cursor.fetchone()[0] == 0:
            conn.execute("INSERT INTO tariffs VALUES ('basic', 'Базовый', 50, 50)")
            conn.execute("INSERT INTO tariffs VALUES ('pro', 'Pro пакет', 150, 200)")

init_db()

# --- ФУНКЦИИ БД ---
def mark_active(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR REPLACE INTO user_activity (user_id, last_active_date) VALUES (?, date('now'))", (user_id,))

def register_user(user_id, referrer_id=None):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        if cursor.fetchone() is None:
            cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, 10)", (user_id,))
            if referrer_id and str(referrer_id) != str(user_id):
                cursor.execute("SELECT balance FROM users WHERE user_id=?", (referrer_id,))
                if cursor.fetchone():
                    cursor.execute("UPDATE users SET balance = balance + 30 WHERE user_id = ?", (referrer_id,))
                    return True
            return False
        return None

def get_user_balance(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        res = conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return res[0] if res else 0

def deduct_balance(user_id, amount=1):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))

def add_balance(user_id, amount, add_to_spent=0):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET balance = balance + ?, total_spent_stars = total_spent_stars + ? WHERE user_id = ?", 
                     (amount, add_to_spent, user_id))

def get_user_resumes(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("SELECT resume_id, name, resume_text FROM user_resumes WHERE user_id=?", (user_id,))
        return [{"id": row[0], "name": row[1], "text": row[2]} for row in cursor.fetchall()]

def save_resume(user_id, name, text):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO user_resumes (user_id, name, resume_text) VALUES (?, ?, ?)", (user_id, name, text))

def delete_resume(resume_id, user_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM user_resumes WHERE resume_id=? AND user_id=?", (resume_id, user_id))

def get_tariffs():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("SELECT tariff_id, name, stars, credits FROM tariffs")
        return {row[0]: {"name": row[1], "stars": row[2], "credits": row[3]} for row in cursor.fetchall()}

def add_to_crm(user_id, company, vacancy_title):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO crm (user_id, company, vacancy_title) VALUES (?, ?, ?)", (user_id, company, vacancy_title))

def get_crm(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("SELECT id, company, vacancy_title, status, date FROM crm WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,))
        return [{"id": r[0], "company": r[1], "title": r[2], "status": r[3], "date": r[4]} for r in cursor.fetchall()]

def update_crm_status(crm_id, user_id, status):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE crm SET status=? WHERE id=? AND user_id=?", (status, crm_id, user_id))

def get_admin_stats():
    with sqlite3.connect(DB_FILE) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        stars = conn.execute("SELECT SUM(total_spent_stars) FROM users").fetchone()[0] or 0
        paid_users = conn.execute("SELECT COUNT(*) FROM users WHERE total_spent_stars > 0").fetchone()[0]
        dau = conn.execute("SELECT COUNT(*) FROM user_activity WHERE last_active_date = date('now')").fetchone()[0]
        return users, stars, paid_users, dau

# ================= AI ФУНКЦИИ (GEMINI) =================
def ai_generate(prompt, system_instruction):
    try:
        response = ai_client.models.generate_content(
            model=AI_MODEL,
            contents=prompt,
            config=genai.types.GenerateContentConfig(system_instruction=system_instruction, temperature=0.3)
        )
        return response.text
    except Exception as e:
        return f"⚠️ Ошибка ИИ: {e}"

async def safe_eval_relevance(desc, title, employer):
    async with gemini_semaphore:
        prompt = f"Вакансия: {title} в {employer}.\nОписание: {desc}\nПодходит ли для руководителя (B2B/Проекты/IT)? ДА или НЕТ."
        res = await asyncio.to_thread(ai_generate, prompt, "Ты скорер. Отвечай одним словом.")
        return "ДА" in res.upper()

async def safe_gen_cover(desc, resume):
    async with gemini_semaphore:
        prompt = f"""
        ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:
        1. Начни: "Здравствуйте, уважаемая команда по подбору,"
        2. Закончи: "С уважением, [Имя из резюме] [Контакты из резюме]".
        3. Никакой воды. Выдели 3-4 пункта, где опыт кандидата бьет в боли вакансии.
        4. Не придумывай опыт.
        
        Вакансия: {desc}
        Резюме: {resume}
        """
        return await asyncio.to_thread(ai_generate, prompt, "Ты строгий карьерный консультант.")

async def safe_adapt_resume(desc, resume):
    async with gemini_semaphore:
        prompt = f"Адаптируй резюме под ATS-систему вакансии. Выдели нужное, не выдумывай факты.\nВакансия: {desc}\nРезюме: {resume}"
        return await asyncio.to_thread(ai_generate, prompt, "Ты карьерный архитектор.")

async def safe_roast(resume):
    async with gemini_semaphore:
        prompt = f"Сделай жесткий аудит резюме. Укажи на воду, клише, нехватку цифр. Дай 3 совета по улучшению.\nРезюме: {resume}"
        return await asyncio.to_thread(ai_generate, prompt, "Ты циничный IT-рекрутер из топовой корпорации.")

async def safe_gap(desc, resume):
    async with gemini_semaphore:
        prompt = f"Сравни резюме и вакансию. Напиши 3 сильные стороны, 2-3 пробела и совет как их сгладить.\nВакансия: {desc}\nРезюме: {resume}"
        return await asyncio.to_thread(ai_generate, prompt, "Ты карьерный стратег.")

async def safe_pitch(lpr, desc, resume):
    async with gemini_semaphore:
        prompt = f"""
        Напиши короткое сообщение для мессенджера (Telegram/LinkedIn) руководителю.
        Цель: {lpr}
        Вакансия: {desc}
        Резюме: {resume}
        Правила: Максимум 5 предложений. Захват внимания, 1 убойный факт, Call-to-action на звонок. Дай 2 варианта: деловой и дерзкий.
        """
        return await asyncio.to_thread(ai_generate, prompt, "Ты эксперт по B2B-продажам.")

# ================= SELENIUM (ПАРСИНГ HH) =================
def fetch_vacancies_via_browser():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    vacancies = []
    try:
        driver.get("https://hh.ru/search/vacancy?text=Руководитель+проектов+OR+Руководитель+продаж&items_on_page=20")
        time.sleep(3)
        items = driver.find_elements(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy']")
        for item in items[:15]: 
            title = item.find_element(By.CSS_SELECTOR, "[data-qa='serp-item__title']").text
            link = item.find_element(By.CSS_SELECTOR, "[data-qa='serp-item__title']").get_attribute("href")
            vac_id = link.split("/")[-1].split("?")[0]
            try: employer = item.find_element(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy-employer']").text
            except: employer = "Неизвестно"
            try: snippet = item.find_element(By.CSS_SELECTOR, "[data-qa='vacancy-serp__vacancy_snippet_requirement']").text
            except: snippet = ""
            vacancies.append({"id": vac_id, "name": title, "employer": employer, "url": link, "desc": snippet})
    except Exception as e: print(e)
    finally: driver.quit()
    return vacancies

async def safe_fetch_vacancies():
    async with selenium_semaphore:
        return await asyncio.to_thread(fetch_vacancies_via_browser)

# ================= МЕНЮ КЛАВИАТУРЫ =================
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Найти вакансии"), KeyboardButton(text="📊 Мои отклики (CRM)")],
            [KeyboardButton(text="🗂 Мои резюме"), KeyboardButton(text="🛠 Инструменты")],
            [KeyboardButton(text="💳 Баланс и Тарифы"), KeyboardButton(text="❓ Как пользоваться")]
        ], resize_keyboard=True
    )

# ================= ОБРАБОТЧИКИ (HANDLERS) =================

# --- СТАРТ И ИНСТРУКЦИЯ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    mark_active(message.from_user.id)
    user_id = message.from_user.id
    args = message.text.split()
    ref_id = args[1] if len(args) > 1 else None
    
    if register_user(user_id, ref_id):
        try: await bot.send_message(int(ref_id), "🎉 *Ура!* Новый друг зарегистрировался. Вам +30 запросов!", parse_mode="Markdown")
        except: pass
            
    await message.answer(
        f"🤖 *Привет! Я твой карьерный AI-ментор.*\n🎁 Баланс: `{get_user_balance(user_id)} запросов`\n\nИспользуй меню внизу:",
        reply_markup=get_main_keyboard(), parse_mode="Markdown"
    )

@dp.message(Command("help"))
@dp.message(F.text == "❓ Как пользоваться")
async def btn_help(msg: types.Message):
    mark_active(msg.from_user.id)
    help_text = (
        "🤖 *Твой карьерный AI-ментор: Руководство*\n\n"
        "1️⃣ *Загрузи резюме* («🗂 Мои резюме»)\n"
        "Сохрани свои профили. Просто отправь текст резюме.\n\n"
        "2️⃣ *Ищи работу умно* («🔍 Найти вакансии»)\n"
        "Бот сам соберет свежие вакансии с HH, отсеет мусор и предложит подходящие. Кликни, и бот напишет идеальный отклик.\n\n"
        "3️⃣ *Карьерные инструменты* («🛠 Инструменты»)\n"
        "🔹 *Прожарка резюме:* аудит твоего CV.\n"
        "🔹 *Адаптация:* переделка опыта под вакансию (для ATS).\n"
        "🔹 *Письмо ЛПР:* сообщение директору в Telegram/LinkedIn.\n"
        "🔹 *Skill Gap:* анализ того, чего тебе не хватает.\n\n"
        "4️⃣ *Воронка откликов* («📊 Мои отклики»)\n"
        "Встроенная CRM-система для отслеживания статусов.\n\n"
        "🎁 *Закончились запросы?*\n"
        "Зайди в «💳 Баланс» и отправь свою ссылку друзьям. За каждого друга — *+30 запросов* бесплатно!"
    )
    await msg.answer(help_text, parse_mode="Markdown")

# --- УПРАВЛЕНИЕ РЕЗЮМЕ ---
@dp.message(F.text == "🗂 Мои резюме")
async def btn_my_resumes(message: types.Message, state: FSMContext):
    mark_active(message.from_user.id)
    await state.clear()
    resumes = get_user_resumes(message.from_user.id)
    text = "🗂 *Ваши профили резюме:*\n\n" if resumes else "Нет сохраненных профилей.\n"
    kb = []
    for i, r in enumerate(resumes, 1):
        text += f"{i}. **{r['name']}**\n"
        kb.append([InlineKeyboardButton(text=f"🗑 Удалить: {r['name']}", callback_data=f"del_res_{r['id']}")])
    kb.append([InlineKeyboardButton(text="➕ Загрузить новое резюме", callback_data="add_new_resume")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data == "add_new_resume")
async def add_res(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(AppStates.waiting_for_resume_name)
    await call.message.answer("Введите короткое название (Например: Директор по продажам):")
    await call.answer()

@dp.message(AppStates.waiting_for_resume_name)
async def res_name(msg: types.Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await state.set_state(AppStates.waiting_for_resume_text)
    await msg.answer("Теперь отправьте полным текстом само резюме (Опыт, навыки, контакты):")

@dp.message(AppStates.waiting_for_resume_text)
async def res_text(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    save_resume(msg.from_user.id, data['name'], msg.text)
    await state.clear()
    await msg.answer(f"✅ Резюме **{data['name']}** сохранено!", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_res_"))
async def del_res(call: types.CallbackQuery):
    delete_resume(int(call.data.split("_")[2]), call.from_user.id)
    await call.message.delete()

# --- ПОИСК НА HH.RU И ОТКЛИКИ ---
@dp.message(F.text == "🔍 Найти вакансии")
async def btn_search(msg: types.Message):
    mark_active(msg.from_user.id)
    user_id = msg.from_user.id
    if get_user_balance(user_id) < 1: return await msg.answer("❌ Нет запросов! /buy")
    resumes = get_user_resumes(user_id)
    if not resumes: return await msg.answer("⚠️ Сначала загрузите резюме в '🗂 Мои резюме'.")

    m = await msg.answer("🔍 Парсер запущен. Ищем вакансии на HH...")
    vacancies = await safe_fetch_vacancies()
    if not vacancies: return await m.edit_text("Вакансии не найдены. Попробуйте позже.")

    await m.edit_text(f"Собрано {len(vacancies)} вакансий. Фильтрую ИИ...")
    for vac in vacancies:
        full_desc = f"{vac['name']} | {vac['employer']}\n{vac['desc']}"
        if await safe_eval_relevance(full_desc, vac['name'], vac['employer']):
            # Сохраняем вакансию для ИИ
            USER_VACANCY_STORAGE[vac['id']] = vac
            
            kb = [[InlineKeyboardButton(text=f"✍️ Отклик: {r['name']} (-1)", callback_data=f"gen_{vac['id']}_{r['id']}")] for r in resumes]
            await msg.answer(f"✅ *{vac['name']}*\n🏢 {vac['employer']}\n[HH.ru]({vac['url']})", 
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown", disable_web_page_preview=True)

@dp.callback_query(F.data.startswith("gen_"))
async def gen_cl(call: types.CallbackQuery):
    user_id = call.from_user.id
    if get_user_balance(user_id) < 1: return await call.answer("❌ Нет запросов!", show_alert=True)
    _, vac_id, res_id = call.data.split("_")
    
    vac_data = USER_VACANCY_STORAGE.get(vac_id)
    if not vac_data: return await call.answer("❌ Ошибка: Данные о вакансии устарели.", show_alert=True)
    vac_desc = f"{vac_data['name']} | {vac_data['employer']}\n{vac_data['desc']}"
    
    res_data = next((r for r in get_user_resumes(user_id) if str(r['id']) == res_id), None)
    if not res_data: return await call.answer("❌ Ошибка: Резюме удалено.", show_alert=True)

    await call.message.answer("⏳ Нейросеть пишет отклик...")
    deduct_balance(user_id, 1)
    
    # Добавляем в CRM
    add_to_crm(user_id, vac_data['employer'], vac_data['name'])
    
    letter = await safe_gen_cover(vac_desc, res_data['text'])
    # Убрал parse_mode, чтобы символы от ИИ не выдавали ошибку
    await call.message.answer(f"✉️ **Отклик:**\n\n{letter}")
    await call.answer()

# --- ИНСТРУМЕНТЫ (АУДИТ, ЛПР, GAP, АДАПТАЦИЯ) ---
@dp.message(F.text == "🛠 Инструменты")
async def btn_tools(msg: types.Message):
    mark_active(msg.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Прожарка резюме", callback_data="tool_roast")],
        [InlineKeyboardButton(text="🎯 Адаптировать резюме", callback_data="tool_adapt")],
        [InlineKeyboardButton(text="📩 Письмо ЛПР (Вхолодную)", callback_data="tool_pitch")],
        [InlineKeyboardButton(text="⚖️ Skill Gap (Пробелы)", callback_data="tool_gap")]
    ])
    await msg.answer("🛠 *Карьерные инструменты AI*\nВыберите нужную функцию:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("tool_"))
async def tool_routing(call: types.CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    action = call.data.split("_")[1]
    resumes = get_user_resumes(user_id)
    if not resumes: return await call.answer("Сначала загрузите резюме!", show_alert=True)
    
    kb = [[InlineKeyboardButton(text=r['name'], callback_data=f"do_{action}_{r['id']}")] for r in resumes]
    await call.message.answer("Выберите базовое резюме для этого инструмента:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

# 1. Прожарка
@dp.callback_query(F.data.startswith("do_roast_"))
async def do_roast(call: types.CallbackQuery):
    user_id = call.from_user.id
    if get_user_balance(user_id) < 1: return await call.answer("❌ Нет запросов!", show_alert=True)
    
    res_id = call.data.split("_")[2]
    res_text = next(r['text'] for r in get_user_resumes(user_id) if str(r['id']) == res_id)
    
    await call.message.edit_text("⏳ Токсичный рекрутер изучает твое резюме...")
    deduct_balance(user_id, 1)
    res = await safe_roast(res_text)
    await call.message.edit_text(res)

# 2. Питч для ЛПР
@dp.callback_query(F.data.startswith("do_pitch_"))
async def do_pitch(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(res_id=call.data.split("_")[2])
    await state.set_state(AppStates.waiting_for_lpr_info)
    await call.message.answer("💼 Кому пишем? (Например: Иван, Коммерческий директор VK)")
    await call.answer()

@dp.message(AppStates.waiting_for_lpr_info)
async def pitch_lpr(msg: types.Message, state: FSMContext):
    await state.update_data(lpr=msg.text)
    await state.set_state(AppStates.waiting_for_lpr_vacancy)
    await msg.answer("📝 Отправьте текст или описание проекта/вакансии, под который мы пишем питч.")

@dp.message(AppStates.waiting_for_lpr_vacancy)
async def pitch_vac(msg: types.Message, state: FSMContext):
    user_id = msg.from_user.id
    if get_user_balance(user_id) < 1: return await msg.answer("❌ Нет запросов!")
    data = await state.get_data()
    res_text = next(r['text'] for r in get_user_resumes(user_id) if str(r['id']) == data['res_id'])
    
    await msg.answer("⏳ Генерирую пробивной питч для мессенджера...")
    deduct_balance(user_id, 1)
    res = await safe_pitch(data['lpr'], msg.text, res_text)
    await msg.answer(res)
    await state.clear()

# 3. Адаптация резюме
@dp.callback_query(F.data.startswith("do_adapt_"))
async def do_adapt(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(res_id=call.data.split("_")[2])
    await state.set_state(AppStates.waiting_for_adapt_vacancy)
    await call.message.answer("🎯 Отправьте текст вакансии, под которую нужно переписать резюме.")
    await call.answer()

@dp.message(AppStates.waiting_for_adapt_vacancy)
async def process_adapt(msg: types.Message, state: FSMContext):
    user_id = msg.from_user.id
    if get_user_balance(user_id) < 1: return await msg.answer("❌ Нет запросов!")
    data = await state.get_data()
    res_text = next(r['text'] for r in get_user_resumes(user_id) if str(r['id']) == data['res_id'])
    
    await msg.answer("⏳ Адаптирую резюме под ATS-систему...")
    deduct_balance(user_id, 1)
    res = await safe_adapt_resume(msg.text, res_text)
    await msg.answer(res)
    await state.clear()

# 4. Анализ пробелов (Skill Gap)
@dp.callback_query(F.data.startswith("do_gap_"))
async def do_gap(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(res_id=call.data.split("_")[2])
    await state.set_state(AppStates.waiting_for_gap_vacancy)
    await call.message.answer("⚖️ Отправьте текст вакансии для анализа пробелов в ваших навыках.")
    await call.answer()

@dp.message(AppStates.waiting_for_gap_vacancy)
async def process_gap(msg: types.Message, state: FSMContext):
    user_id = msg.from_user.id
    if get_user_balance(user_id) < 1: return await msg.answer("❌ Нет запросов!")
    data = await state.get_data()
    res_text = next(r['text'] for r in get_user_resumes(user_id) if str(r['id']) == data['res_id'])
    
    await msg.answer("⏳ Анализирую ваши сильные и слабые стороны для этой роли...")
    deduct_balance(user_id, 1)
    res = await safe_gap(msg.text, res_text)
    await msg.answer(res)
    await state.clear()

# --- CRM СИСТЕМА (МОИ ОТКЛИКИ) ---
@dp.message(F.text == "📊 Мои отклики (CRM)")
async def btn_crm(msg: types.Message):
    mark_active(msg.from_user.id)
    records = get_crm(msg.from_user.id)
    if not records: return await msg.answer("У вас пока нет отправленных откликов.")
    await msg.answer("📊 *Ваша воронка откликов:*\nУправляйте статусами с помощью кнопок ниже.", parse_mode="Markdown")
    for r in records:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Собеседование", callback_data=f"crm_{r['id']}_Собеседование"),
             InlineKeyboardButton(text="🏆 Оффер", callback_data=f"crm_{r['id']}_Оффер"),
             InlineKeyboardButton(text="❌ Отказ", callback_data=f"crm_{r['id']}_Отказ")]
        ])
        date_str = r['date'].split()[0]
        await msg.answer(f"🏢 **{r['company']}** | {r['title']}\n📅 {date_str} | Статус: **{r['status']}**", 
                         reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("crm_"))
async def update_crm(call: types.CallbackQuery):
    _, crm_id, status = call.data.split("_")
    update_crm_status(crm_id, call.from_user.id, status)
    await call.answer(f"Статус изменен на: {status}", show_alert=False)
    await call.message.delete()

# --- БАЛАНС И ТАРИФЫ ---
@dp.message(F.text == "💳 Баланс и Тарифы")
async def btn_balance(msg: types.Message):
    mark_active(msg.from_user.id)
    user_id = msg.from_user.id
    bot_info = await bot.get_me()
    text = (f"💰 *Ваш баланс:* `{get_user_balance(user_id)} запросов`\n\n"
            f"🤝 *Пригласи друга и получи бонусы!*\nОтправь эту ссылку коллегам. За каждого нового пользователя ты получишь **+30 запросов**.\n\n"
            f"🔗 Твоя ссылка:\n`https://t.me/{bot_info.username}?start={user_id}`\n\n"
            f"⬇️ *Купить тариф за Telegram Stars:*")
    kb = [[InlineKeyboardButton(text=f"⭐️ {t['name']} ({t['credits']} шт) - {t['stars']} XTR", callback_data=f"buy_{id}")] for id, t in get_tariffs().items()]
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy_"))
async def process_buy(call: types.CallbackQuery):
    t = get_tariffs()[call.data.split("_")[1]]
    await bot.send_invoice(call.from_user.id, "Пакет запросов", f"{t['name']}", f"inv_{call.data.split('_')[1]}", "", "XTR", [LabeledPrice(t["name"], t["stars"])])

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery): 
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def success_pay(msg: types.Message):
    t = get_tariffs()[msg.successful_payment.invoice_payload.split("_")[1]]
    add_balance(msg.from_user.id, t["credits"], t["stars"])
    await msg.answer(f"🎉 Успешно! Вам начислено *{t['credits']} запросов*.", parse_mode="Markdown")

# --- СЕКРЕТНАЯ АДМИН-ПАНЕЛЬ ---
@dp.message(Command("adminlemus1"), F.from_user.id == ADMIN_ID)
async def cmd_admin(msg: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="💳 Тарифы", callback_data="admin_tariffs")],
        [InlineKeyboardButton(text="🎁 Выдать", callback_data="admin_access")]
    ])
    await msg.answer("🔐 *Секретная Панель*", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_"), F.from_user.id == ADMIN_ID)
async def admin_call(call: types.CallbackQuery):
    act = call.data.split("_")[1]
    if act == "stats":
        users, stars, paid, dau = get_admin_stats()
        await call.message.edit_text(f"📊 *Статистика*\n👥 Всего юзеров: {users}\n💸 Платников: {paid}\n🔥 Активных сегодня: {dau}\n⭐️ Заработано XTR: {stars}", parse_mode="Markdown", reply_markup=call.message.reply_markup)
    elif act == "tariffs":
        t = "\n".join([f"🔹 `{k}`: {v['stars']} XTR -> {v['credits']} шт." for k, v in get_tariffs().items()])
        await call.message.edit_text(f"💳 *Тарифы*\n{t}\n\nМенять командой: `/set_tariff basic 50 100`", parse_mode="Markdown", reply_markup=call.message.reply_markup)
    elif act == "access":
        await call.message.edit_text("🎁 Начислить баланс вручную:\nОтправьте команду: `/give <ID> <Сумма>`", parse_mode="Markdown", reply_markup=call.message.reply_markup)

@dp.message(Command("set_tariff"), F.from_user.id == ADMIN_ID)
async def cmd_set_tariff(msg: types.Message):
    try:
        args = msg.text.split()
        update_tariff(args[1], int(args[2]), int(args[3]))
        await msg.answer("✅ Тариф успешно обновлен!")
    except: 
        await msg.answer("⚠️ Ошибка. Формат: `/set_tariff basic 50 100`")

@dp.message(Command("give"), F.from_user.id == ADMIN_ID)
async def cmd_give(msg: types.Message):
    try:
        args = msg.text.split()
        target_id, amount = int(args[1]), int(args[2])
        add_balance(target_id, amount)
        await msg.answer(f"✅ Баланс юзера `{target_id}` изменен на {amount}.")
        if amount > 0: 
            await bot.send_message(target_id, f"🎁 Администратор начислил вам {amount} бесплатных запросов!")
    except: 
        pass

# ================= ЗАПУСК БОТА =================
async def main():
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню / Перезапуск"),
        BotCommand(command="help", description="Как пользоваться ботом")
    ])
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())