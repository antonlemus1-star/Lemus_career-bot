import asyncio
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from google import genai
from google.genai import types as genai_types
from pypdf import PdfReader
from docx import Document

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    print("Ошибка: не задан BOT_TOKEN в переменных окружения!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Хранилище резюме в памяти для каждого пользователя: {user_id: {resume_name: text}}
user_resumes = {}

class CareerState(StatesGroup):
    waiting_for_resume_file = State()
    waiting_for_vacancy_to_apply = State()
    choosing_resume_for_action = State()

def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📁 Мои резюме")
    builder.button(text="📤 Загрузить резюме")
    builder.button(text="✍️ Написать отклик на вакансию")
    builder.button(text="ℹ️ Помощь")
    builder.adjust(2, 1, 1)
    return builder.as_markup(resize_keyboard=True)

# Чтение текста из PDF
def extract_text_from_pdf(file_path):
    reader = PdfReader(file_path)
    text = ""
    for page in reader.pages:
        text += page.extract_text() or ""
    return text

# Чтение текста из Word (docx)
def extract_text_from_docx(file_path):
    doc = Document(file_path)
    text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
    return text

@dp.message(Command("start"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 Привет! Я твой продвинутый карьерный бот.\n\n"
        "Ты можешь загрузить до **5 резюме** в форматах PDF или Word (.docx), "
        "а я буду использовать их для подбора вакансий и генерации идеальных откликов."
    )
    await message.answer(text, reply_markup=get_main_keyboard())

# --- ЗАГРУЗКА РЕЗЮМЕ ---
@dp.message(F.text == "📤 Загрузить резюме")
async def upload_resume_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    current_count = len(user_resumes.get(user_id, {}))
    if current_count >= 5:
        await message.answer("⚠️ У тебя уже загружено максимальное количество резюме (5 штук).", reply_markup=get_main_keyboard())
        return

    await state.set_state(CareerState.waiting_for_resume_file)
    await message.answer(
        f"📄 Отправь файл резюме (PDF или Word). У тебя загружено: {current_count}/5.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(CareerState.waiting_for_resume_file, F.document)
async def process_resume_document(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    document = message.document
    file_name = document.file_name

    if not (file_name.endswith('.pdf') or file_name.endswith('.docx')):
        await message.answer("⚠️ Пожалуйста, отправь файл в формате **PDF** или **Word (.docx)**.")
        return

    file_info = await bot.get_file(document.file_id)
    file_path_downloaded = f"temp_{user_id}_{file_name}"
    await bot.download(file_info, destination=file_path_downloaded)

    try:
        if file_name.endswith('.pdf'):
            resume_text = extract_text_from_pdf(file_path_downloaded)
        else:
            resume_text = extract_text_from_docx(file_path_downloaded)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при чтении файла: {e}", reply_markup=get_main_keyboard())
        if os.path.exists(file_path_downloaded):
            os.remove(file_path_downloaded)
        await state.clear()
        return

    if os.path.exists(file_path_downloaded):
        os.remove(file_path_downloaded)

    if user_id not in user_resumes:
        user_resumes[user_id] = {}

    user_resumes[user_id][file_name] = resume_text
    await state.clear()

    await message.answer(
        f"✅ Резюме **{file_name}** успешно сохранено! Всего резюме: {len(user_resumes[user_id])}/5.",
        reply_markup=get_main_keyboard()
    )

# --- СПИСОК РЕЗЮМЕ ---
@dp.message(F.text == "📁 Мои резюме")
async def list_resumes(message: types.Message):
    user_id = message.from_user.id
    resumes = user_resumes.get(user_id, {})
    if not resumes:
        await message.answer("📂 У тебя пока нет загруженных резюме. Нажми «📤 Загрузить резюме».", reply_markup=get_main_keyboard())
        return

    text = "📂 Твои загруженные резюме:\n" + "\n".join([f"• {name}" for name in resumes.keys()])
    await message.answer(text, reply_markup=get_main_keyboard())

# --- НАПИСАТЬ ОТКЛИК ПОД ВАКАНСИЮ ---
@dp.message(F.text == "✍️ Написать отклик на вакансию")
async def select_resume_for_apply(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    resumes = user_resumes.get(user_id, {})
    if not resumes:
        await message.answer("⚠️ Сначала загрузи хотя бы одно резюме!", reply_markup=get_main_keyboard())
        return

    builder = InlineKeyboardBuilder()
    for name in resumes.keys():
        builder.button(text=f"📄 {name}", callback_data=f"apply_cv:{name}")
    builder.adjust(1)

    await message.answer("Выбери резюме, под которое нужно составить отклик:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("apply_cv:"))
async def process_selected_resume_for_apply(callback: types.CallbackQuery, state: FSMContext):
    resume_name = callback.data.split(":", 1)[1]
    await state.update_data(selected_resume=resume_name)
    await state.set_state(CareerState.waiting_for_vacancy_to_apply)
    await callback.message.edit_text(f"Выбрано резюме: **{resume_name}**.\n\nТеперь отправь описание вакансии (текстом), на которую нужно написать отклик:")
    await callback.answer()

@dp.message(CareerState.waiting_for_vacancy_to_apply, F.text)
async def generate_cover_letter(message: types.Message, state: FSMContext):
    user_data = await state.get_data()
    resume_name = user_data.get("selected_resume")
    user_id = message.from_user.id
    resume_text = user_resumes.get(user_id, {}).get(resume_name, "")
    await state.clear()

    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY.", reply_markup=get_main_keyboard())
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        prompt = (
            f"Используя мое резюме, напиши сильное, убедительное сопроводительное письмо (cover letter) "
            f"под указанную вакансию. Выдели ключевые достижения, релевантные этой позиции.\n\n"
            f"МОЕ РЕЗЮМЕ ({resume_name}):\n{resume_text}\n\n"
            f"ОПИСАНИЕ ВАКАНСИИ:\n{message.text}"
        )
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction="Ты эксперт по карьерному консультированию и написанию эффективных откликов на вакансии."
            ),
        )
        await message.answer(response.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

# Общий обработчик текста
@dp.message(F.text)
async def handle_message(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY.", reply_markup=get_main_keyboard())
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.0-flash',
            contents=message.text,
            config=genai_types.GenerateContentConfig(
                system_instruction="Ты профессиональный карьерный консультант."
            ),
        )
        await message.answer(response.text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка ИИ: {e}", reply_markup=get_main_keyboard())

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен с поддержкой загрузки PDF/Word резюме...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())