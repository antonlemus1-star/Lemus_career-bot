import asyncio
import logging
import os
import sys
import sqlite3
import aiohttp
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from google.genai import types as genai_types
from pypdf import PdfReader
from docx import Document

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
PAYMENT_TOKEN = os.getenv("PAYMENT_TOKEN")

if not BOT_TOKEN:
    print("Ошибка: не задан BOT_TOKEN!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# --- БАЗА ДАННЫХ И ТАРИФЫ ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, 
    referrer_id INTEGER, 
    balance INTEGER DEFAULT 30,
    is_paid INTEGER DEFAULT 0,
    last_active_date TEXT
)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, 
    user_id INTEGER, 
    company_name TEXT, 
    status TEXT
)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS tariffs (
    id TEXT PRIMARY KEY, 
    type TEXT, 
    requests INTEGER, 
    price INTEGER, 
    name TEXT
)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS dislikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    vacancy_title TEXT
)''')

cursor.execute('SELECT COUNT(*) FROM tariffs')
if cursor.fetchone()[0] == 0:
    cursor.executemany('INSERT INTO tariffs VALUES (?, ?, ?, ?, ?)', [
        ('basic', 'fiat', 50, 150, 'Стартовый'),
        ('pro', 'fiat', 200, 450, 'Профи'),
        ('stars_100', 'stars', 100, 100, 'Пакет ⭐️')
    ])
conn.commit()

user_resumes = {}
temp_vacancies = {}

# --- ИНТЕГРАЦИЯ С HEADHUNTER API ---
async def fetch_hh_vacancies(keywords: str):
    url = "https://api.hh.ru/vacancies"
    params = {
        "text": keywords,
        "search_field": "name",
        "period": 10,
        "per_page": 5,
        "order_by": "relevance"
    }
    headers = {"User-Agent": "LemusCareerBot/1.0"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("items", [])
        except Exception as e:
            logging.error(f"HH API error: {e}")
    return []

# --- MIDDLEWARE ДЛЯ ТРЕКИНГА АКТИВНОСТИ ---
class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute('UPDATE users SET last_active_date = ? WHERE user_id = ?', (today, user_id))
        conn.commit()
        return await handler(event, data)

dp.message.middleware(ActivityMiddleware())
dp.callback_query.middleware(ActivityMiddleware())

# --- СОСТОЯНИЯ FSM ---
class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    choosing_cv_for_search = State()
    choosing_cv_for_adapt = State()
    waiting_for_vacancy_adapt = State()
    choosing_cv_for_apply = State()
    waiting_for_vacancy_apply = State()
    choosing_cv_for_skillgap = State()
    waiting_for_vacancy_skillgap = State()
    choosing_cv_for_audit = State()
    choosing_cv_for_mock = State()
    waiting_for_vacancy_mock = State()
    mock_in_progress = State()
    
    admin_add_balance_user = State()
    admin_add_balance_amount = State()
    admin_edit_t_req = State()
    admin_edit_t_price = State()

# --- КЛАВИАТУРА ---
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

def get_balance(user_id):
    cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    res = cursor.fetchone()
    return res[0] if res else 0

def add_balance(user_id, amount):
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()

async def check_and_deduct(user_id, message: types.Message) -> bool:
    if get_balance(user_id) <= 0:
        await message.answer("⚠️ Твои запросы закончились! Нажми «💎 Оплата и Баланс» или пригласи друзей.")
        return False
    add_balance(user_id, -1)
    return True

def extract_text_from_pdf(file_path):
    return "".join([page.extract_text() or "" for page in PdfReader(file_path).pages])

def extract_text_from_docx(file_path):
    return "\n".join([p.text for p in Document(file_path).paragraphs])

# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("adminlemus71"))
async def cmd_admin(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_ID): return
    
    today = datetime.now().strftime('%Y-%m-%d')
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM users WHERE is_paid = 1')
    paid_users = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM users WHERE last_active_date = ?', (today,))
    daily_visits = cursor.fetchone()[0]
    cursor.execute('SELECT COUNT(*) FROM applications')
    total_apps = cursor.fetchone()[0]
    
    builder = InlineKeyboardBuilder()
    builder.button(text="⚙️ Настройка тарифов", callback_data="admin_tariffs")
    builder.button(text="💰 Выдать запросы", callback_data="admin_give_balance")
    builder.adjust(1)

    text = (f"👑 **Панель Администратора**\n\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"💰 Платных подписчиков: {paid_users}\n"
            f"📈 Посещений за сегодня: {daily_visits}\n"
            f"📝 Откликов в CRM: {total_apps}")
    await message.answer(text, reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_give_balance")
async def admin_give_bal_btn(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID): return
    await state.set_state(CareerState.admin_add_balance_user)
    await callback.message.answer("Введи **ID пользователя** (только цифры):")
    await callback.answer()

@dp.message(CareerState.admin_add_balance_user)
async def admin_add_user(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("⚠️ Ошибка: ID должен состоять только из цифр.")
        return
    await state.update_data(target=int(message.text))
    await state.set_state(CareerState.admin_add_balance_amount)
    await message.answer("Сколько запросов начислить?")

@dp.message(CareerState.admin_add_balance_amount)
async def admin_add_amt(message: types.Message, state: FSMContext):
    if not message.text or (not message.text.isdigit() and not (message.text.startswith('-') and message.text[1:].isdigit())):
        await message.answer("⚠️ Ошибка: Количество запросов должно быть числом.")
        return
    data = await state.get_data()
    add_balance(data['target'], int(message.text))
    await message.answer(f"✅ Начислено {message.text} запросов юзеру {data['target']}.")
    await state.clear()

@dp.callback_query(F.data == "admin_tariffs")
async def admin_show_tariffs(callback: types.CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID): return
    builder = InlineKeyboardBuilder()
    cursor.execute("SELECT id, name, requests, price, type FROM tariffs")
    for t_id, name, req, price, t_type in cursor.fetchall():
        sign = "₽" if t_type == "fiat" else "⭐️"
        builder.button(text=f"{name}: {req} зап. / {price}{sign}", callback_data=f"adm_edt_tar_{t_id}")
    builder.adjust(1)
    await callback.message.edit_text("📋 **Список тарифов:**", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_edt_tar_"))
async def admin_edit_tariff_menu(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID): return
    t_id = callback.data.replace("adm_edt_tar_", "")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Изменить кол-во запросов", callback_data=f"adm_set_req_{t_id}")
    builder.button(text="💵 Изменить цену", callback_data=f"adm_set_prc_{t_id}")
    builder.button(text="⬅️ Назад", callback_data="admin_tariffs")
    builder.adjust(1)
    await callback.message.edit_text(f"Что меняем?", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_set_req_"))
async def admin_ask_req(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(edit_t_id=callback.data.replace("adm_set_req_", ""))
    await state.set_state(CareerState.admin_edit_t_req)
    await callback.message.answer("Новое количество запросов:")
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_set_prc_"))
async def admin_ask_prc(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(edit_t_id=callback.data.replace("adm_set_prc_", ""))
    await state.set_state(CareerState.admin_edit_t_price)
    await callback.message.answer("Новая цена:")
    await callback.answer()

@dp.message(CareerState.admin_edit_t_req, F.text)
async def admin_save_req(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not message.text.isdigit(): return await message.answer("⚠️ Нужен численный формат.")
    cursor.execute("UPDATE tariffs SET requests = ? WHERE id = ?", (int(message.text), data['edit_t_id']))
    conn.commit()
    await message.answer("✅ Готово!")
    await state.clear()

@dp.message(CareerState.admin_edit_t_price, F.text)
async def admin_save_prc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not message.text.isdigit(): return await message.answer("⚠️ Нужен численный формат.")
    cursor.execute("UPDATE tariffs SET price = ? WHERE id = ?", (int(message.text), data['edit_t_id']))
    conn.commit()
    await message.answer("✅ Готово!")
    await state.clear()

# --- СТАРТ И РЕФЕРАЛКА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    user_id = message.from_user.id
    today = datetime.now().strftime('%Y-%m-%d')
    
    cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
    if not cursor.fetchone():
        ref_id = int(command.args) if command.args and command.args.isdigit() else None
        if ref_id and ref_id != user_id:
            add_balance(ref_id, 30)
            try: await bot.send_message(ref_id, "🎉 По твоей ссылке пришел друг! +30 запросов.")
            except: pass
        cursor.execute('INSERT INTO users (user_id, referrer_id, balance, last_active_date) VALUES (?, ?, 30, ?)', (user_id, ref_id, today))
        conn.commit()

    welcome_text = (
        "👋 **Привет! Я твой продвинутый карьерный AI-советник.**\n\n"
        "🛠 **Как работать:**\n"
        "1️⃣ Нажми **«📤 Загрузить»** и отправь резюме (до 5 шт).\n"
        "2️⃣ Используй **«🔍 Поиск вакансий»** для подбора свежих позиций на HH.ru.\n"
        "3️⃣ Отправляй вакансии в **«✍️ Отклик»** для создания Cover Letters.\n"
        "4️⃣ Тренируйся в **«🎤 Тренажер собеседований»**.\n\n"
        "🎁 У тебя есть **30 бесплатных запросов**."
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    await message.answer("🤖 Бот помогает искать вакансии по HH.ru, адаптировать резюме, проходить аудит, писать отклики и тренироваться на собеседованиях.")

@dp.message(F.text == "🎁 Пригласить друга")
async def cmd_referral(message: types.Message):
    link = f"https://t.me/{(await bot.get_me()).username}?start={message.from_user.id}"
    await message.answer(f"🎁 Даю **30 запросов** тебе и другу!\n\nТвоя ссылка:\n`{link}`", parse_mode="Markdown")

@dp.message(F.text == "💎 Оплата и Баланс")
async def cmd_balance(message: types.Message):
    bal = get_balance(message.from_user.id)
    builder = InlineKeyboardBuilder()
    cursor.execute("SELECT id, requests, price, name FROM tariffs WHERE type='fiat'")
    for t_id, req, price, name in cursor.fetchall():
        builder.button(text=f"💳 {name} ({req} зап.) - {price}₽", callback_data=f"pay_fiat_{t_id}")
    cursor.execute("SELECT id, requests, price, name FROM tariffs WHERE type='stars'")
    for t_id, req, price, name in cursor.fetchall():
        builder.button(text=f"⭐️ {name} ({req} зап.) - {price} ⭐️", callback_data=f"pay_stars_{t_id}")
    builder.adjust(1)
    await message.answer(f"💰 Баланс: **{bal} запросов**.", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("pay_fiat_"))
async def process_pay_fiat(callback: types.CallbackQuery):
    if not PAYMENT_TOKEN: return await callback.answer("⚠️ Не настроено.", show_alert=True)
    tariff = cursor.execute("SELECT requests, price, name FROM tariffs WHERE id=?", (callback.data.replace("pay_fiat_", ""),)).fetchone()
    if not tariff: return
    await bot.send_invoice(callback.from_user.id, title="Пакет", description=tariff[2], payload=f"req_{tariff[0]}", provider_token=PAYMENT_TOKEN, currency="RUB", prices=[types.LabeledPrice(label=tariff[2], amount=tariff[1]*100)])
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_pay_stars(callback: types.CallbackQuery):
    tariff = cursor.execute("SELECT requests, price, name FROM tariffs WHERE id=?", (callback.data.replace("pay_stars_", ""),)).fetchone()
    if not tariff: return
    await bot.send_invoice(callback.from_user.id, title="Пакет", description=tariff[2], payload=f"req_{tariff[0]}", provider_token="", currency="XTR", prices=[types.LabeledPrice(label=tariff[2], amount=tariff[1])])
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    req_amount = int(message.successful_payment.invoice_payload.replace("req_", ""))
    add_balance(message.from_user.id, req_amount)
    cursor.execute('UPDATE users SET is_paid = 1 WHERE user_id = ?', (message.from_user.id,))
    conn.commit()
    await message.answer(f"🎉 Начислено {req_amount} запросов.")

async def show_cv_selector(message: types.Message, state: FSMContext, state_to_set, prompt_text: str):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("⚠️ Сначала загрузи резюме через кнопку '📤 Загрузить'.", reply_markup=get_main_keyboard())
    await state.set_state(state_to_set)
    builder = InlineKeyboardBuilder()
    for idx, name in enumerate(resumes.keys()):
        builder.button(text=f"📄 {name}", callback_data=f"use_cv:{idx}")
    builder.adjust(1)
    await message.answer(prompt_text, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("disl_"))
async def handle_dislike(callback: types.CallbackQuery):
    vac_id = callback.data.replace("disl_", "")
    title = temp_vacancies.get(vac_id, "Вакансия")
    cursor.execute('INSERT INTO dislikes (user_id, vacancy_title) VALUES (?, ?)', (callback.from_user.id, title))
    conn.commit()
    await callback.message.edit_text(f"🚫 Скрыто: «{title}». Буду учитывать при поиске.")
    await callback.answer()

@dp.callback_query(F.data.startswith("use_cv:"))
async def process_cv_selection(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    cv_idx = int(callback.data.split(":")[1])
    resumes = user_resumes.get(callback.from_user.id, {})
    cv_name = list(resumes.keys())[cv_idx]
    cv_text = resumes[cv_name]
    await state.update_data(cv_text=cv_text, cv_name=cv_name)

    if current_state == CareerState.choosing_cv_for_search.state:
        await callback.message.edit_text(f"🔍 Сканирую HeadHunter для: {cv_name}...")
        if await check_and_deduct(callback.from_user.id, callback.message):
            cursor.execute('SELECT vacancy_title FROM dislikes WHERE user_id = ? ORDER BY id DESC LIMIT 10', (callback.from_user.id,))
            dislikes_str = ", ".join([row[0] for row in cursor.fetchall()]) or "Нет"

            # ИСПОЛЬЗУЕМ СТАБИЛЬНУЮ МОДЕЛЬ gemini-2.0-flash
            prompt = f"Сформируй 1-3 ключевых слова для поиска на HeadHunter. Верни ТОЛЬКО слова через пробел. Исключи: {dislikes_str}.\n\nРЕЗЮМЕ:\n{cv_text[:1000]}"
            res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
            keywords = res.text.strip().replace('"', '').replace("'", "")
            
            vacancies = await fetch_hh_vacancies(keywords)
            if vacancies:
                await callback.message.edit_text(f"🔥 **Вакансии по запросу:** `{keywords}`", parse_mode="Markdown")
                for v in vacancies:
                    name, vac_id, url = v.get("name", ""), str(v.get("id", "0")), v.get("alternate_url", "")
                    employer = v.get("employer", {}).get("name", "")
                    temp_vacancies[vac_id] = name
                    builder = InlineKeyboardBuilder()
                    builder.button(text="👎 Мимо", callback_data=f"disl_{vac_id}")
                    await callback.message.answer(f"🏢 **{employer}**\n💼 [{name}]({url})", reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))
            else:
                await callback.message.edit_text(f"По запросу `{keywords}` ничего не найдено.", parse_mode="Markdown")
        await state.clear()
    elif current_state == CareerState.choosing_cv_for_adapt.state:
        await state.set_state(CareerState.waiting_for_vacancy_adapt)
        await callback.message.edit_text(f"Отправь описание вакансии для адаптации резюме:")
    elif current_state == CareerState.choosing_cv_for_apply.state:
        await state.set_state(CareerState.waiting_for_vacancy_apply)
        await callback.message.edit_text(f"Отправь текст вакансии для отклика:")
    elif current_state == CareerState.choosing_cv_for_skillgap.state:
        await state.set_state(CareerState.waiting_for_vacancy_skillgap)
        await callback.message.edit_text(f"Отправь текст вакансии для анализа навыков:")
    elif current_state == CareerState.choosing_cv_for_mock.state:
        await state.set_state(CareerState.waiting_for_vacancy_mock)
        await callback.message.edit_text(f"Отправь текст вакансии для тренировки:")
    elif current_state == CareerState.choosing_cv_for_audit.state:
        await callback.message.edit_text(f"📋 Аудит резюме:")
        if await check_and_deduct(callback.from_user.id, callback.message):
            prompt = f"Проведи аудит резюме, подсвети сильные стороны и точки роста:\n\n{cv_text}"
            res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
            await callback.message.answer(res.text)
        await state.clear()
    await callback.answer()

@dp.message(F.text == "📁 Мои резюме")
async def list_resumes(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("📂 Пусто.")
    await message.answer("📂 Резюме:\n" + "\n".join([f"• {n}" for n in resumes.keys()]))

@dp.message(F.text == "📤 Загрузить")
async def upload_resume_start(message: types.Message, state: FSMContext):
    if len(user_resumes.get(message.from_user.id, {})) >= 5: return await message.answer("⚠️ Максимум 5 резюме.")
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл (PDF или .docx).", reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_resume_document(message: types.Message, state: FSMContext):
    doc = message.document
    if not (doc.file_name.endswith('.pdf') or doc.file_name.endswith('.docx')): return await message.answer("⚠️ Только PDF/Word!")
    path = f"temp_{message.from_user.id}_{doc.file_name}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    try: text = extract_text_from_pdf(path) if doc.file_name.endswith('.pdf') else extract_text_from_docx(path)
    except Exception as e:
        if os.path.exists(path): os.remove(path)
        return await message.answer(f"⚠️ Ошибка: {e}")
    user_resumes.setdefault(message.from_user.id, {})[doc.file_name] = text
    await state.clear()
    os.remove(path)
    await message.answer(f"✅ Сохранено: {doc.file_name}", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def start_search(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_search, "Выбери резюме для поиска:")

@dp.message(F.text == "🛠 Адаптация резюме")
async def start_adapt(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_adapt, "Выбери резюме для адаптации:")

@dp.message(F.text == "✍️ Отклик")
async def start_apply(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_apply, "Выбери резюме для отклика:")

@dp.message(F.text == "📊 Skill Gap")
async def start_skillgap(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_skillgap, "Выбери резюме для анализа:")

@dp.message(F.text == "📋 Аудит резюме")
async def start_audit(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_audit, "Выбери резюме для аудита:")

@dp.message(F.text == "🎤 Тренажер собеседований")
async def start_mock(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_mock, "Выбери резюме для тренировки:")

@dp.message(CareerState.waiting_for_vacancy_adapt, F.text)
async def adapt_cv(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Адаптируй резюме под вакансию:\n\nРЕЗЮМЕ:\n{data['cv_text']}\n\nВАКАНСИЯ:\n{message.text}")
    await message.answer(res.text, reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_apply, F.text)
async def gen_cover_letter(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Напиши отклик на вакансию:\n\nРЕЗЮМЕ:\n{data['cv_text']}\n\nВАКАНСИЯ:\n{message.text}")
    cursor.execute('INSERT INTO applications (user_id, company_name, status) VALUES (?, ?, ?)', (message.from_user.id, message.text[:30], 'Отправлено'))
    conn.commit()
    await message.answer(f"{res.text}\n\n📌 Сохранено в трекер.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_skillgap, F.text)
async def process_skillgap(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Сравни резюме и вакансию, укажи пробелы в навыках:\n\nРЕЗЮМЕ:\n{data['cv_text']}\n\nВАКАНСИЯ:\n{message.text}")
    await message.answer(res.text, reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_mock, F.text)
async def start_mock_interview(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Ты HR. Задай 1-й вопрос из 5 по резюме и вакансии:\n\nРЕЗЮМЕ:\n{data['cv_text'][:1000]}\n\nВАКАНСИЯ:\n{message.text[:1000]}")
    await state.update_data(mock_step=1, mock_history=f"HR: {res.text}\n")
    await state.set_state(CareerState.mock_in_progress)
    await message.answer(res.text, reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.mock_in_progress, F.text)
async def continue_mock_interview(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    step, history = data['mock_step'], data['mock_history'] + f"Кандидат: {message.text}\n"
    if step < 5:
        step += 1
        res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Продолжи собеседование ({step}/5). История:\n{history}")
        await state.update_data(mock_step=step, mock_history=history + f"HR: {res.text}\n")
        await message.answer(res.text)
    else:
        res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Собеседование окончено. История:\n{history}\nДай фидбек.")
        await message.answer(f"🏁 Итог:\n\n{res.text}", reply_markup=get_main_keyboard())
        await state.clear()

@dp.message(F.text == "📌 Трекер откликов")
async def show_tracker(message: types.Message):
    rows = cursor.execute('SELECT id, company_name, status FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 15', (message.from_user.id,)).fetchall()
    if not rows: return await message.answer("В трекере пусто.")
    builder = InlineKeyboardBuilder()
    for r in rows: builder.button(text=f"[{r[2]}] {r[1]}", callback_data=f"trk_menu:{r[0]}")
    builder.adjust(1)
    await message.answer("📌 Отклики:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("trk_menu:"))
async def tracker_item_menu(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for st in ["Отправлено", "HR-интервью", "Тестовое", "Оффер", "Отказ"]: 
        builder.button(text=st, callback_data=f"trk_set:{callback.data.split(':')[1]}:{st}")
    builder.adjust(2)
    await callback.message.edit_text("Новый статус:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("trk_set:"))
async def tracker_set_status(callback: types.CallbackQuery):
    _, app_id, new_status = callback.data.split(":", 2)
    cursor.execute('UPDATE applications SET status = ? WHERE id = ?', (new_status, app_id))
    conn.commit()
    await callback.message.edit_text(f"✅ Статус: {new_status}")

@dp.message(F.text)
async def handle_any_text(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        return await message.answer("⚠️ Ожидается ввод данных. Нажми /start для сброса.")
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Ты карьерный AI-консультант. Ответь:\n\n{message.text}")
    await message.answer(res.text, reply_markup=get_main_keyboard())

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен с моделью gemini-2.0-flash!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())