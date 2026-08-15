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
# Таблица для обучения ИИ (неподходящие вакансии)
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

# Хранилище резюме и кэш вакансий
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

# --- УПРАВЛЕНИЕ БАЛАНСОМ ---
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

# --- ЧТЕНИЕ ФАЙЛОВ ---
def extract_text_from_pdf(file_path):
    return "".join([page.extract_text() or "" for page in PdfReader(file_path).pages])

def extract_text_from_docx(file_path):
    return "\n".join([p.text for p in Document(file_path).paragraphs])

# --- ПАНЕЛЬ АДМИНИСТРАТОРА ---
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
    await callback.message.answer("Введи **ID пользователя** (только цифры), которому нужно начислить запросы:")
    await callback.answer()

@dp.message(CareerState.admin_add_balance_user)
async def admin_add_user(message: types.Message, state: FSMContext):
    # ЗАЩИТА: Проверяем, что введены именно цифры
    if not message.text or not message.text.isdigit():
        await message.answer("⚠️ Ошибка: ID пользователя должен состоять только из цифр.\nВведи корректный ID или нажми /start для отмены.")
        return
        
    await state.update_data(target=int(message.text))
    await state.set_state(CareerState.admin_add_balance_amount)
    await message.answer("Сколько запросов начислить?")

@dp.message(CareerState.admin_add_balance_amount)
async def admin_add_amt(message: types.Message, state: FSMContext):
    # ЗАЩИТА: Проверяем, что введено число
    if not message.text or (not message.text.isdigit() and not (message.text.startswith('-') and message.text[1:].isdigit())):
        await message.answer("⚠️ Ошибка: Количество запросов должно быть числом.\nВведи число или нажми /start для отмены.")
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
    await callback.message.edit_text("📋 **Список тарифов:**\nВыбери тариф, чтобы изменить его настройки.", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_edt_tar_"))
async def admin_edit_tariff_menu(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID): return
    t_id = callback.data.replace("adm_edt_tar_", "")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Изменить кол-во запросов", callback_data=f"adm_set_req_{t_id}")
    builder.button(text="💵 Изменить цену", callback_data=f"adm_set_prc_{t_id}")
    builder.button(text="⬅️ Назад", callback_data="admin_tariffs")
    builder.adjust(1)
    await callback.message.edit_text(f"Что меняем в тарифе?", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("adm_set_req_"))
async def admin_ask_req(callback: types.CallbackQuery, state: FSMContext):
    t_id = callback.data.replace("adm_set_req_", "")
    await state.update_data(edit_t_id=t_id)
    await state.set_state(CareerState.admin_edit_t_req)
    await callback.message.answer("Отправь **новое количество запросов** (просто число):")
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_set_prc_"))
async def admin_ask_prc(callback: types.CallbackQuery, state: FSMContext):
    t_id = callback.data.replace("adm_set_prc_", "")
    await state.update_data(edit_t_id=t_id)
    await state.set_state(CareerState.admin_edit_t_price)
    await callback.message.answer("Отправь **новую цену** (просто число):")
    await callback.answer()

@dp.message(CareerState.admin_edit_t_req, F.text)
async def admin_save_req(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not message.text.isdigit():
        return await message.answer("⚠️ Ошибка. Нужно отправить целое число.")
    cursor.execute("UPDATE tariffs SET requests = ? WHERE id = ?", (int(message.text), data['edit_t_id']))
    conn.commit()
    await message.answer("✅ Количество запросов успешно обновлено!")
    await state.clear()

@dp.message(CareerState.admin_edit_t_price, F.text)
async def admin_save_prc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if not message.text.isdigit():
        return await message.answer("⚠️ Ошибка. Нужно отправить целое число.")
    cursor.execute("UPDATE tariffs SET price = ? WHERE id = ?", (int(message.text), data['edit_t_id']))
    conn.commit()
    await message.answer("✅ Цена успешно обновлена!")
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
        "Я заменяю целую команду HR-специалистов. Помогу найти работу мечты быстрее и увереннее.\n\n"
        "🛠 **Как мы будем работать (Шаги):**\n"
        "1️⃣ Нажми **«📤 Загрузить»** и отправь мне свои резюме (до 5 шт).\n"
        "2️⃣ Используй **«🔍 Поиск вакансий»**, чтобы я сам нашел для тебя свежие вакансии на HH.ru.\n"
        "3️⃣ Отправляй мне описания вакансий через **«✍️ Отклик»**, чтобы я писал крутые Cover Letters.\n"
        "4️⃣ Заходи в **«🎤 Тренажер собеседований»**, чтобы подготовиться к каверзным вопросам рекрутера.\n\n"
        "🎁 Тебе начислено **30 бесплатных запросов**. Нажми **«ℹ️ Помощь»**, чтобы прочитать подробнее обо всех функциях!"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 **Что я умею:**\n\n"
        "🔍 **Поиск вакансий** — сам сканирую HH.ru по твоему резюме и выдаю 5 свежих релевантных вакансий с ссылками!\n\n"
        "📋 **Аудит резюме** — подсвечиваю клише и помогаю усилить формулировки.\n\n"
        "📊 **Skill Gap** — говорю, каких навыков тебе не хватает для вакансии и как это компенсировать.\n\n"
        "🎤 **Тренажер собеседований** — выступаю в роли HR: задаю 5 каверзных вопросов по пересечению твоего резюме и вакансии.\n\n"
        "📌 **Трекер откликов** — встроенная CRM для ведения статусов твоих собеседований.\n\n"
        "✍️ **Генерация отклика** — пишу идеальное сопроводительное письмо под конкретную вакансию."
    )
    await message.answer(help_text)

@dp.message(F.text == "🎁 Пригласить друга")
async def cmd_referral(message: types.Message):
    link = f"https://t.me/{(await bot.get_me()).username}?start={message.from_user.id}"
    await message.answer(f"🎁 Даю **30 запросов** тебе и другу!\n\nТвоя реферальная ссылка:\n`{link}`", parse_mode="Markdown")

# --- ДИНАМИЧЕСКИЙ БАЛАНС И ОПЛАТА ---
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
    await message.answer(f"💰 Доступно: **{bal} запросов**.\n\nВыбери удобный способ пополнения:", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("pay_fiat_"))
async def process_pay_fiat(callback: types.CallbackQuery):
    if not PAYMENT_TOKEN: return await callback.answer("⚠️ Оплата картами не настроена.", show_alert=True)
    t_id = callback.data.replace("pay_fiat_", "")
    cursor.execute("SELECT requests, price, name FROM tariffs WHERE id=?", (t_id,))
    tariff = cursor.fetchone()
    if not tariff: return
    req, price, name = tariff
    prices = [types.LabeledPrice(label=f"{name}", amount=price * 100)]
    await bot.send_invoice(callback.from_user.id, title="Пакет запросов", description=f"Пополнение на {req} запросов", payload=f"req_{req}", provider_token=PAYMENT_TOKEN, currency="RUB", prices=prices)
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_stars_"))
async def process_pay_stars(callback: types.CallbackQuery):
    t_id = callback.data.replace("pay_stars_", "")
    cursor.execute("SELECT requests, price, name FROM tariffs WHERE id=?", (t_id,))
    tariff = cursor.fetchone()
    if not tariff: return
    req, price, name = tariff
    prices = [types.LabeledPrice(label=f"{name}", amount=price)]
    await bot.send_invoice(callback.from_user.id, title="Пакет запросов", description=f"Пополнение на {req} запросов", payload=f"req_{req}", provider_token="", currency="XTR", prices=prices)
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
    await message.answer(f"🎉 Успешно! Начислено {req_amount} запросов.")

# --- УТИЛИТА: ВЫБОР РЕЗЮМЕ ---
async def show_cv_selector(message: types.Message, state: FSMContext, state_to_set, prompt_text: str):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("⚠️ Загрузи резюме (кнопка '📤 Загрузить').", reply_markup=get_main_keyboard())
    await state.set_state(state_to_set)
    builder = InlineKeyboardBuilder()
    for idx, name in enumerate(resumes.keys()):
        builder.button(text=f"📄 {name}", callback_data=f"use_cv:{idx}")
    builder.adjust(1)
    await message.answer(prompt_text, reply_markup=builder.as_markup())

# --- ОБРАБОТКА ОБУЧЕНИЯ БОТА ("МИМО") ---
@dp.callback_query(F.data.startswith("disl_"))
async def handle_dislike(callback: types.CallbackQuery):
    vac_id = callback.data.replace("disl_", "")
    title = temp_vacancies.get(vac_id, "Неизвестная вакансия")
    
    cursor.execute('INSERT INTO dislikes (user_id, vacancy_title) VALUES (?, ?)', (callback.from_user.id, title))
    conn.commit()
    
    await callback.message.edit_text(f"🚫 Вакансия скрыта.\nЯ запомнил, что **«{title}»** тебе не подходит, и буду отсеивать подобные в будущем.", parse_mode="Markdown")
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
        await callback.message.edit_text(f"🔍 Сканирую HeadHunter для резюме: {cv_name}...\nПодожди секунду, подбираю самые свежие вакансии.")
        if await check_and_deduct(callback.from_user.id, callback.message):
            
            cursor.execute('SELECT vacancy_title FROM dislikes WHERE user_id = ? ORDER BY id DESC LIMIT 10', (callback.from_user.id,))
            dislikes = [row[0] for row in cursor.fetchall()]
            dislikes_str = ", ".join(dislikes) if dislikes else "Нет"

            prompt = (f"Сформируй 1-3 самых точных ключевых слова (название должности или навык) для строки поиска HeadHunter. "
                      f"Верни ТОЛЬКО слова через пробел. "
                      f"ВАЖНО: Пользователь отметил, что ему НЕ подходят вакансии с названиями: {dislikes_str}. "
                      f"Используй оператор NOT, чтобы исключить их из поиска (например: Менеджер NOT продаж NOT b2b).\n\n"
                      f"РЕЗЮМЕ:\n{cv_text[:1000]}")
            
            res = ai_client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
            keywords = res.text.strip().replace('"', '').replace("'", "")
            
            vacancies = await fetch_hh_vacancies(keywords)
            
            if vacancies:
                await callback.message.edit_text(f"🔥 **Нашел свежие вакансии по запросу:** `{keywords}`\nВыбери подходящую, скопируй её текст и нажми «✍️ Отклик» в меню!", parse_mode="Markdown")
                
                for v in vacancies:
                    name = v.get("name", "Без названия")
                    vac_id = str(v.get("id", "0"))
                    url = v.get("alternate_url", "")
                    employer = v.get("employer", {}).get("name", "Компания не указана")
                    
                    salary = v.get("salary")
                    sal_text = "ЗП не указана"
                    if salary:
                        sal_text = f"от {salary.get('from', '')} до {salary.get('to', '')} {salary.get('currency', '')}".replace("от None", "").replace("до None", "").strip()
                    
                    temp_vacancies[vac_id] = name
                    
                    builder = InlineKeyboardBuilder()
                    builder.button(text="👎 Мимо (Не показывать)", callback_data=f"disl_{vac_id}")
                    
                    msg_text = f"🏢 **{employer}**\n💼 [{name}]({url})\n💰 {sal_text}"
                    await callback.message.answer(msg_text, reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))
            else:
                await callback.message.edit_text(f"К сожалению, по запросу `{keywords}` свежих вакансий не найдено. Попробуй обновить резюме.", parse_mode="Markdown")
        await state.clear()
        
    elif current_state == CareerState.choosing_cv_for_adapt.state:
        await state.set_state(CareerState.waiting_for_vacancy_adapt)
        await callback.message.edit_text(f"Выбрано: {cv_name}\n\n🛠 Отправь описание вакансии, и я перепишу твое резюме под нее:")
        
    elif current_state == CareerState.choosing_cv_for_apply.state:
        await state.set_state(CareerState.waiting_for_vacancy_apply)
        await callback.message.edit_text(f"Выбрано резюме: {cv_name}\n\n📝 Теперь отправь текст конкретной вакансии, чтобы я написал идеальный отклик (Cover Letter):")
        
    elif current_state == CareerState.choosing_cv_for_skillgap.state:
        await state.set_state(CareerState.waiting_for_vacancy_skillgap)
        await callback.message.edit_text(f"Выбрано: {cv_name}\n\n📊 Отправь текст вакансии для анализа пробелов:")
        
    elif current_state == CareerState.choosing_cv_for_mock.state:
        await state.set_state(CareerState.waiting_for_vacancy_mock)
        await callback.message.edit_text(f"Выбрано резюме: {cv_name}\n\n🎤 Отправь текст вакансии, на которую ты идешь. Тренажер задаст вопросы по ней:")
        
    elif current_state == CareerState.choosing_cv_for_audit.state:
        await callback.message.edit_text(f"📋 Провожу аудит резюме: {cv_name}...")
        if await check_and_deduct(callback.from_user.id, callback.message):
            prompt = f"Ты тактичный карьерный консультант. Проведи глубокий аудит этого резюме. Подсвети сильные стороны, укажи на точки роста и клише, предложи сильные формулировки.\n\n{cv_text}"
            await execute_gemini_prompt(callback.message, prompt)
        await state.clear()
    await callback.answer()

# --- ФУНКЦИИ ГЛАВНОГО МЕНЮ ---
@dp.message(F.text == "📁 Мои резюме")
async def list_resumes(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("📂 Пусто.", reply_markup=get_main_keyboard())
    await message.answer("📂 Твои резюме:\n" + "\n".join([f"• {n}" for n in resumes.keys()]))

@dp.message(F.text == "📤 Загрузить")
async def upload_resume_start(message: types.Message, state: FSMContext):
    if len(user_resumes.get(message.from_user.id, {})) >= 5: return await message.answer("⚠️ Максимум 5 резюме.")
    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer("📄 Отправь файл (PDF или .docx).", reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_resume_document(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    doc = message.document
    if not (doc.file_name.endswith('.pdf') or doc.file_name.endswith('.docx')): return await message.answer("⚠️ Только PDF/Word!")
    path = f"temp_{user_id}_{doc.file_name}"
    await bot.download(await bot.get_file(doc.file_id), destination=path)
    try: text = extract_text_from_pdf(path) if doc.file_name.endswith('.pdf') else extract_text_from_docx(path)
    except Exception as e:
        if os.path.exists(path): os.remove(path)
        return await message.answer(f"⚠️ Ошибка чтения: {e}")
    user_resumes.setdefault(user_id, {})[doc.file_name] = text
    await state.clear()
    os.remove(path)
    await message.answer(f"✅ Сохранено: {doc.file_name}", reply_markup=get_main_keyboard())

# --- ТОЧКИ ВХОДА ИИ ---
@dp.message(F.text == "🔍 Поиск вакансий")
async def start_search(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_search, "Какое резюме использовать для поиска вакансий?")

@dp.message(F.text == "🛠 Адаптация резюме")
async def start_adapt(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_adapt, "Какое резюме будем адаптировать?")

@dp.message(F.text == "✍️ Отклик")
async def start_apply(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_apply, "Выбери резюме, под которое пишется отклик:")

@dp.message(F.text == "📊 Skill Gap")
async def start_skillgap(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_skillgap, "Выбери резюме для анализа навыков:")

@dp.message(F.text == "📋 Аудит резюме")
async def start_audit(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_audit, "Какое резюме отправить на аудит?")

@dp.message(F.text == "🎤 Тренажер собеседований")
async def start_mock(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_mock, "С каким резюме пойдем на собеседование?")

# --- ОБРАБОТЧИКИ ВВОДА ВАКАНСИЙ ---
@dp.message(CareerState.waiting_for_vacancy_adapt, F.text)
async def adapt_cv(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    prompt = f"Адаптируй это резюме под вакансию. Выдели релевантный опыт и добавь ключевые слова из вакансии, чтобы пройти ATS фильтры.\n\nРЕЗЮМЕ:\n{data['cv_text']}\nВАКАНСИЯ:\n{message.text}"
    await execute_gemini_prompt(message, prompt)
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_apply, F.text)
async def gen_cover_letter(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    prompt = f"Напиши сильное сопроводительное письмо (Cover Letter) на основе этого резюме и требований вакансии.\nРЕЗЮМЕ:\n{data['cv_text']}\nВАКАНСИЯ:\n{message.text}"
    await execute_gemini_prompt(message, prompt)
    
    company_preview = message.text[:30].replace('\n', ' ') + "..."
    cursor.execute('INSERT INTO applications (user_id, company_name, status) VALUES (?, ?, ?)', (message.from_user.id, company_preview, 'Отправлено'))
    conn.commit()
    await message.answer(f"📌 Вакансия добавлена в Трекер.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_skillgap, F.text)
async def process_skillgap(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    prompt = f"Сравни резюме и вакансию. Каких навыков не хватает? Как это компенсировать?\n\nРЕЗЮМЕ:\n{data['cv_text']}\nВАКАНСИЯ:\n{message.text}"
    await execute_gemini_prompt(message, prompt)
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_mock, F.text)
async def start_mock_interview(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    prompt = f"Ты нанимающий менеджер. Вот резюме кандидата: {data['cv_text'][:1000]}\nА вот вакансия, на которую он претендует: {message.text[:1000]}\nОпираясь на пересечение этого опыта и требований, задай 1-й профильный вопрос из 5."
    try:
        res = ai_client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        await state.update_data(mock_step=1, mock_history=f"HR: {res.text}\n")
        await state.set_state(CareerState.mock_in_progress)
        await message.answer(res.text, reply_markup=types.ReplyKeyboardRemove())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}", reply_markup=get_main_keyboard())

@dp.message(CareerState.mock_in_progress, F.text)
async def continue_mock_interview(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    step, history = data['mock_step'], data['mock_history']
    history += f"Кандидат: {message.text}\n"
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    if step < 5:
        step += 1
        prompt = f"Продолжаем. {step}-й вопрос из 5. История:\n{history}\nДай оценку ответу и задай следующий вопрос, опираясь на вакансию и резюме."
        res = ai_client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        await state.update_data(mock_step=step, mock_history=history + f"HR: {res.text}\n")
        await message.answer(res.text)
    else:
        res = ai_client.models.generate_content(model='gemini-1.5-flash', contents=f"Конец. История:\n{history}\nДай развернутый фидбек по интервью. Укажи на сильные стороны и точки роста.")
        await message.answer(f"🏁 Завершено.\n\n{res.text}", reply_markup=get_main_keyboard())
        await state.clear()

# --- CRM ТРЕКЕР ---
@dp.message(F.text == "📌 Трекер откликов")
async def show_tracker(message: types.Message):
    cursor.execute('SELECT id, company_name, status FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 15', (message.from_user.id,))
    rows = cursor.fetchall()
    if not rows: return await message.answer("В трекере пусто.")
    builder = InlineKeyboardBuilder()
    for r in rows: builder.button(text=f"[{r[2]}] {r[1]}", callback_data=f"trk_menu:{r[0]}")
    builder.adjust(1)
    await message.answer("📌 Твои отклики:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("trk_menu:"))
async def tracker_item_menu(callback: types.CallbackQuery):
    app_id = callback.data.split(":")[1]
    builder = InlineKeyboardBuilder()
    for st in ["Отправлено", "HR-интервью", "Тестовое", "Оффер", "Отказ"]: 
        builder.button(text=st, callback_data=f"trk_set:{app_id}:{st}")
    builder.adjust(2)
    await callback.message.edit_text("Изменить статус:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("trk_set:"))
async def tracker_set_status(callback: types.CallbackQuery):
    _, app_id, new_status = callback.data.split(":", 2)
    cursor.execute('UPDATE applications SET status = ? WHERE id = ?', (new_status, app_id))
    conn.commit()
    await callback.message.edit_text(f"✅ Статус: {new_status}")

# --- БАЗОВЫЙ ИИ ---
async def execute_gemini_prompt(message: types.Message, prompt: str):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        res = ai_client.models.generate_content(model='gemini-1.5-flash', contents=prompt)
        await message.answer(res.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

# --- ОБЩИЙ ОБРАБОТЧИК ТЕКСТА (ЗАГЛУШКА И ИИ-ЧАТ) ---
@dp.message(F.text)
async def handle_any_text(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state is not None:
        await message.answer("⚠️ Я сейчас жду от тебя конкретный файл, текст или ID.\n\nЕсли хочешь отменить действие и начать заново, нажми /start.")
        return
    
    lower_text = message.text.lower()
    if "поиск" in lower_text and "вакансий" in lower_text:
        await start_search(message, state)
        return
    elif "адаптировать" in lower_text:
        await start_adapt(message, state)
        return
    elif "отклик" in lower_text:
        await start_apply(message, state)
        return
    elif "тренажер" in lower_text or "собеседован" in lower_text:
        await start_mock(message, state)
        return

    prompt = f"Ты опытный карьерный AI-консультант. Ответь на вопрос пользователя конструктивно и по делу:\n\n{message.text}"
    await execute_gemini_prompt(message, prompt)

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен! Исправлена ошибка 'invalid literal for int()'")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())