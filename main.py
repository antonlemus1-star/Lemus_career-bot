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

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect('tracker.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, referrer_id INTEGER, balance INTEGER DEFAULT 30, is_paid INTEGER DEFAULT 0, last_active_date TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, company_name TEXT, status TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS tariffs (id TEXT PRIMARY KEY, type TEXT, requests INTEGER, price INTEGER, name TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS dislikes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, vacancy_title TEXT)')
conn.commit()

user_resumes = {}
temp_vacancies = {}

# --- ИНТЕГРАЦИЯ HH.RU ---
async def fetch_hh_vacancies(keywords: str):
    url = "https://api.hh.ru/vacancies"
    params = {"text": keywords, "search_field": "name", "period": 10, "per_page": 5, "order_by": "relevance"}
    headers = {"User-Agent": "LemusCareerBot/1.0"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("items", [])
        except: return []
    return []

# --- MIDDLEWARE ---
class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        cursor.execute('UPDATE users SET last_active_date = ? WHERE user_id = ?', (datetime.now().strftime('%Y-%m-%d'), event.from_user.id))
        conn.commit()
        return await handler(event, data)

dp.message.middleware(ActivityMiddleware())
dp.callback_query.middleware(ActivityMiddleware())

# --- FSM ---
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

# --- БАЛАНС И УТИЛИТЫ ---
def get_balance(user_id):
    res = cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,)).fetchone()
    return res[0] if res else 0

def add_balance(user_id, amount):
    cursor.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()

async def check_and_deduct(user_id, message: types.Message) -> bool:
    if get_balance(user_id) <= 0:
        await message.answer("⚠️ Твои запросы закончились!")
        return False
    add_balance(user_id, -1)
    return True

async def execute_ai(message: types.Message, prompt: str):
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
        await message.answer(res.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}")

def extract_text_from_pdf(file_path):
    return "".join([page.extract_text() or "" for page in PdfReader(file_path).pages])

def extract_text_from_docx(file_path):
    return "\n".join([p.text for p in Document(file_path).paragraphs])

# --- СТАРТ И ПОМОЩЬ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,)).fetchone():
        cursor.execute('INSERT INTO users (user_id, balance, last_active_date) VALUES (?, 30, ?)', (user_id, datetime.now().strftime('%Y-%m-%d')))
        conn.commit()
    await message.answer("👋 Привет! Я твой карьерный AI-помощник. Загрузи резюме и начни поиск.", reply_markup=get_main_keyboard())

@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: types.Message):
    await message.answer("🤖 Бот ищет свежие вакансии с HH.ru, обучает поиск по кнопке «Мимо», проводит аудит резюме, пишет отклики и тренирует на собеседованиях.")

@dp.message(F.text == "🎁 Пригласить друга")
async def cmd_referral(message: types.Message):
    link = f"https://t.me/{(await bot.get_me()).username}?start={message.from_user.id}"
    await message.answer(f"🎁 Даю **30 запросов** тебе и другу!\n\nСсылка:\n`{link}`", parse_mode="Markdown")

# --- ВЫБОР РЕЗЮМЕ ---
async def show_cv_selector(message: types.Message, state: FSMContext, state_to_set, prompt_text: str):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("⚠️ Сначала загрузи резюме через кнопку '📤 Загрузить'.")
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
    await callback.message.edit_text(f"🚫 Скрыто: «{title}». Учту при следующем поиске.")
    await callback.answer()

@dp.callback_query(F.data.startswith("use_cv:"))
async def process_cv_selection(callback: types.CallbackQuery, state: FSMContext):
    cv_idx = int(callback.data.split(":")[1])
    resumes = user_resumes.get(callback.from_user.id, {})
    cv_name = list(resumes.keys())[cv_idx]
    cv_text = resumes[cv_name]
    await state.update_data(cv_text=cv_text, cv_name=cv_name)
    current_state = await state.get_state()
    
    if current_state == CareerState.choosing_cv_for_search.state:
        await callback.message.edit_text(f"🔍 Сканирую HeadHunter для: {cv_name}...")
        if await check_and_deduct(callback.from_user.id, callback.message):
            dislikes = [row[0] for row in cursor.execute('SELECT vacancy_title FROM dislikes WHERE user_id = ?', (callback.from_user.id,)).fetchall()]
            prompt = f"Выдели 3 ключевых слова для поиска на HH.ru. Верни ТОЛЬКО слова через пробел. Исключи: {', '.join(dislikes)}.\n\nРЕЗЮМЕ:\n{cv_text[:1000]}"
            res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
            keywords = res.text.strip().replace('"', '').replace("'", "")
            
            vacs = await fetch_hh_vacancies(keywords)
            if vacs:
                await callback.message.edit_text(f"🔥 **Вакансии по запросу:** `{keywords}`", parse_mode="Markdown")
                for v in vacs:
                    v_id = str(v['id'])
                    temp_vacancies[v_id] = v['name']
                    builder = InlineKeyboardBuilder()
                    builder.button(text="👎 Мимо", callback_data=f"disl_{v_id}")
                    await callback.message.answer(f"🏢 {v.get('employer',{}).get('name')}\n💼 [{v['name']}]({v['alternate_url']})", reply_markup=builder.as_markup(), parse_mode="Markdown", link_preview_options=types.LinkPreviewOptions(is_disabled=True))
            else:
                await callback.message.edit_text(f"По запросу `{keywords}` ничего не найдено.", parse_mode="Markdown")
        await state.clear()
    elif current_state == CareerState.choosing_cv_for_audit.state:
        if await check_and_deduct(callback.from_user.id, callback.message):
            await execute_ai(callback.message, f"Проведи глубокий аудит резюме, подсвети клише и точки роста:\n\n{cv_text}")
        await state.clear()
    elif current_state == CareerState.choosing_cv_for_adapt.state:
        await state.set_state(CareerState.waiting_for_vacancy_adapt)
        await callback.message.edit_text("Отправь текст вакансии для адаптации:")
    elif current_state == CareerState.choosing_cv_for_apply.state:
        await state.set_state(CareerState.waiting_for_vacancy_apply)
        await callback.message.edit_text("Отправь текст вакансии для написания отклика:")
    elif current_state == CareerState.choosing_cv_for_skillgap.state:
        await state.set_state(CareerState.waiting_for_vacancy_skillgap)
        await callback.message.edit_text("Отправь текст вакансии для анализа навыков:")
    elif current_state == CareerState.choosing_cv_for_mock.state:
        await state.set_state(CareerState.waiting_for_vacancy_mock)
        await callback.message.edit_text("Отправь текст вакансии для тренировки на собеседовании:")
    await callback.answer()

# --- МЕНЮ ---
@dp.message(F.text == "📁 Мои резюме")
async def list_resumes(message: types.Message):
    resumes = user_resumes.get(message.from_user.id, {})
    if not resumes: return await message.answer("📂 Список резюме пуст.")
    await message.answer("📂 Твои резюме:\n" + "\n".join([f"• {n}" for n in resumes.keys()]))

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
        return await message.answer(f"⚠️ Ошибка чтения файла: {e}")
    user_resumes.setdefault(message.from_user.id, {})[doc.file_name] = text
    await state.clear()
    os.remove(path)
    await message.answer(f"✅ Сохранено: {doc.file_name}", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Поиск вакансий")
async def start_search(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_search, "Выбери резюме для поиска вакансий:")

@dp.message(F.text == "🛠 Адаптация резюме")
async def start_adapt(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_adapt, "Выбери резюме для адаптации:")

@dp.message(F.text == "✍️ Отклик")
async def start_apply(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_apply, "Выбери резюме для отклика:")

@dp.message(F.text == "📊 Skill Gap")
async def start_skillgap(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_skillgap, "Выбери резюме для анализа навыков:")

@dp.message(F.text == "📋 Аудит резюме")
async def start_audit(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_audit, "Выбери резюме для аудита:")

@dp.message(F.text == "🎤 Тренажер собеседований")
async def start_mock(message: types.Message, state: FSMContext):
    await show_cv_selector(message, state, CareerState.choosing_cv_for_mock, "Выбери резюме для тренировки на собеседовании:")

# --- ОБРАБОТЧИКИ ВАКАНСИЙ ---
@dp.message(CareerState.waiting_for_vacancy_adapt, F.text)
async def adapt_cv(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    await execute_ai(message, f"Адаптируй резюме под вакансию:\n\nРЕЗЮМЕ:\n{data['cv_text']}\n\nВАКАНСИЯ:\n{message.text}")
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_apply, F.text)
async def gen_cover_letter(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Напиши сопроводительное письмо:\n\nРЕЗЮМЕ:\n{data['cv_text']}\n\nВАКАНСИЯ:\n{message.text}")
    cursor.execute('INSERT INTO applications (user_id, company_name, status) VALUES (?, ?, ?)', (message.from_user.id, message.text[:30], 'Отправлено'))
    conn.commit()
    await message.answer(f"{res.text}\n\n📌 Добавлено в трекер откликов.", reply_markup=get_main_keyboard())
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_skillgap, F.text)
async def process_skillgap(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    await execute_ai(message, f"Сравни резюме и вакансию, укажи пробелы в навыках:\n\nРЕЗЮМЕ:\n{data['cv_text']}\n\nВАКАНСИЯ:\n{message.text}")
    await state.clear()

@dp.message(CareerState.waiting_for_vacancy_mock, F.text)
async def start_mock_interview(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Ты жесткий нанимающий менеджер. Задай 1-й профильный вопрос из 5:\n\nРЕЗЮМЕ:\n{data['cv_text'][:1000]}\n\nВАКАНСИЯ:\n{message.text[:1000]}")
    await state.update_data(mock_step=1, mock_history=f"HR: {res.text}\n")
    await state.set_state(CareerState.mock_in_progress)
    await message.answer(res.text, reply_markup=types.ReplyKeyboardRemove())

@dp.message(CareerState.mock_in_progress, F.text)
async def continue_mock_interview(message: types.Message, state: FSMContext):
    if not await check_and_deduct(message.from_user.id, message): return
    data = await state.get_data()
    step, history = data['mock_step'], data['mock_history'] + f"Кандидат: {message.text}\n"
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    if step < 5:
        step += 1
        res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Продолжаем собеседование ({step}/5). История:\n{history}")
        await state.update_data(mock_step=step, mock_history=history + f"HR: {res.text}\n")
        await message.answer(res.text)
    else:
        res = ai_client.models.generate_content(model='gemini-2.0-flash', contents=f"Собеседование завершено. История:\n{history}\nДай развернутый фидбек.")
        await message.answer(f"🏁 Фидбек по собеседованию:\n\n{res.text}", reply_markup=get_main_keyboard())
        await state.clear()

# --- CRM ТРЕКЕР ---
@dp.message(F.text == "📌 Трекер откликов")
async def show_tracker(message: types.Message):
    rows = cursor.execute('SELECT id, company_name, status FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 15', (message.from_user.id,)).fetchall()
    if not rows: return await message.answer("В трекере пока пусто.")
    builder = InlineKeyboardBuilder()
    for r in rows: builder.button(text=f"[{r[2]}] {r[1]}", callback_data=f"trk_menu:{r[0]}")
    builder.adjust(1)
    await message.answer("📌 Твои отклики:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("trk_menu:"))
async def tracker_item_menu(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for st in ["Отправлено", "HR-интервью", "Тестовое", "Оффер", "Отказ"]: 
        builder.button(text=st, callback_data=f"trk_set:{callback.data.split(':')[1]}:{st}")
    builder.adjust(2)
    await callback.message.edit_text("Измени статус:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("trk_set:"))
async def tracker_set_status(callback: types.CallbackQuery):
    _, app_id, new_status = callback.data.split(":", 2)
    cursor.execute('UPDATE applications SET status = ? WHERE id = ?', (new_status, app_id))
    conn.commit()
    await callback.message.edit_text(f"✅ Статус изменен на: {new_status}")

# --- ОБЩИЙ ЧАТ ---
@dp.message(F.text)
async def handle_any_text(message: types.Message, state: FSMContext):
    # Убран жесткий перехват кнопок меню, теперь текст обрабатывается корректно
    await execute_ai(message, message.text)

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())