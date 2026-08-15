import asyncio
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from google import genai
from google.genai import types as genai_types

# Загружаем переменные окружения из Render
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    print("Ошибка: не задан BOT_TOKEN в переменных окружения!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Состояния для пошаговых сценариев (FSM)
class CareerState(StatesGroup):
    waiting_for_resume = State()          # Ожидание резюме для оценки / поиска
    waiting_for_vacancy_match = State()   # Ожидание резюме + вакансии
    waiting_for_adaptation_resume = State() # Ожидание резюме для адаптации
    waiting_for_adaptation_job = State()    # Ожидание текста вакансии для адаптации

# Главная клавиатура меню
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Оценить резюме")
    builder.button(text="🔍 Поиск вакансий по резюме")
    builder.button(text="🛠 Адаптировать резюме под вакансию")
    builder.button(text="💡 Подготовка к интервью")
    builder.button(text="ℹ️ Помощь")
    builder.adjust(1, 2, 1, 1)
    return builder.as_markup(resize_keyboard=True)

# Команда /start
@dp.message(Command("start"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 Привет! Я твой персональный карьерный AI-помощник.\n\n"
        "Выбирай нужную функцию с помощью кнопок внизу экрана:"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

# --- 1. ОЦЕНИТЬ РЕЗЮМЕ ---
@dp.message(F.text == "📋 Оценить резюме")
async def start_evaluate_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_resume)
    await message.answer(
        "📄 Отправь текст своего резюме (скопируй и вставь сюда сообщением), и я сделаю детальный разбор и дам рекомендации по улучшению.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(CareerState.waiting_for_resume, F.text)
async def process_evaluate_resume(message: types.Message, state: FSMContext):
    await state.clear()
    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY.", reply_markup=get_main_keyboard())
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"Проведи глубокий профессиональный анализ этого резюме. Выдели сильные стороны, слабые места и дай конкретные советы по улучшению:\n\n{message.text}",
            config=genai_types.GenerateContentConfig(
                system_instruction="Ты строгий и профессиональный HR-директор ведущей IT/телеком компании."
            ),
        )
        await message.answer(response.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

# --- 2. ПОИСК ВАКАНСИЙ ПО РЕЗЮМЕ ---
@dp.message(F.text == "🔍 Поиск вакансий по резюме")
async def start_match_vacancies(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_vacancy_match)
    await message.answer(
        "🔍 Отправь текст своего резюме, и я проанализирую твой опыт, после чего предложу наиболее подходящие карьерные направления, роли и ключевые слова для поиска.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(CareerState.waiting_for_vacancy_match, F.text)
async def process_match_vacancies(message: types.Message, state: FSMContext):
    await state.clear()
    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY.", reply_markup=get_main_keyboard())
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=f"На основе опыта этого резюме подбери лучшие карьерные роли, подходящие направления для поиска работы и составь профиль идеальной вакансии:\n\n{message.text}",
            config=genai_types.GenerateContentConfig(
                system_instruction="Ты эксперт по карьерному консультированию и подбору топ-менеджеров и специалистов."
            ),
        )
        await message.answer(response.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

# --- 3. АДАПТИРОВАТЬ РЕЗЮМЕ ПОД ВАКАНСИЮ ---
@dp.message(F.text == "🛠 Адаптировать резюме под вакансию")
async def start_adapt_resume(message: types.Message, state: FSMContext):
    await state.set_state(CareerState.waiting_for_adaptation_resume)
    await message.answer(
        "Шаг 1 из 2: Сначала отправь текст **своего текущего резюме**.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(CareerState.waiting_for_adaptation_resume, F.text)
async def process_adapt_resume_get_cv(message: types.Message, state: FSMContext):
    await state.update_data(resume_text=message.text)
    await state.set_state(CareerState.waiting_for_adaptation_job)
    await message.answer("Шаг 2 из 2: Отличро! Теперь отправь текст **описания вакансии**, под которую нужно подстроиться.")

@dp.message(CareerState.waiting_for_adaptation_job, F.text)
async def process_adapt_resume_get_job(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    resume_text = user_data.get("resume_text")
    await state.clear()

    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY.", reply_markup=get_main_keyboard())
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        prompt = (
            f"Адаптируй следующее резюме под конкретную вакансию так, чтобы подчеркнуть релевантный опыт, "
            f"использовать ключевые слова из описания вакансии и повысить шансы на проход HR-фильтра.\n\n"
            f"РЕЗЮМЕ:\n{resume_text}\n\n"
            f"ОПИСАНИЕ ВАКАНСИИ:\n{message.text}"
        )
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction="Ты профессиональный карьерный писатель и эксперт по оптимизации резюме под ATS-системы."
            ),
        )
        await message.answer(response.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

# --- 4. ПОДГОТОВКА К ИНТЕРВЬЮ ---
@dp.message(F.text == "💡 Подготовка к интервью")
async def btn_interview(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Напиши название позиции и компании, к которой готовишься, и я составлю список каверзных вопросов с подсказками, как на них отвечать.",
        reply_markup=get_main_keyboard()
    )

# --- ОБЩИЙ ОБРАБОТЧИК ТЕКСТА ---
@dp.message(F.text)
async def handle_message(message: types.Message, state: FSMContext):
    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY на сервере.", reply_markup=get_main_keyboard())
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=message.text,
            config=genai_types.GenerateContentConfig(
                system_instruction="Ты профессиональный карьерный консультант. Отвечай конструктивно и по делу."
            ),
        )
        await message.answer(response.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот успешно запущен со всеми функциями...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())