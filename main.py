import asyncio
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
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
dp = Dispatcher()

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Функция для создания кнопок меню под сообщением
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Оценить резюме")
    builder.button(text="🔍 Поиск вакансий")
    builder.button(text="💡 Подготовка к интервью")
    builder.button(text="ℹ️ Помощь")
    builder.adjust(2, 2)  # по 2 кнопки в ряду
    return builder.as_markup(resize_keyboard=True)

# Команда /start и кнопка помощи
@dp.message(Command("start"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_start(message: types.Message):
    welcome_text = (
        "👋 Привет! Я твой персональный карьерный AI-помощник.\n\n"
        "Используй кнопки меню внизу экрана для быстрого доступа к ключевым функциям, "
        "или просто напиши мне любой вопрос по поиску работы и карьере!"
    )
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

# Обработка нажатий на кнопки меню
@dp.message(F.text == "📋 Оценить резюме")
async def btn_resume(message: types.Message):
    await message.answer(
        "Пожалуйста, отправь текст своего резюме, и я дам по нему детальную обратную связь и подскажу, что можно улучшить.",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "🔍 Поиск вакансий")
async def btn_vacancies(message: types.Message):
    await message.answer(
        "Напиши ключевые слова (например: 'Руководитель отдела продаж' или 'Python Developer') и город/формат, и мы обсудим подходящие направления.",
        reply_markup=get_main_keyboard()
    )

@dp.message(F.text == "💡 Подготовка к интервью")
async def btn_interview(message: types.Message):
    await message.answer(
        "На какую позицию ты готовишься к собеседованию? Напиши название компании или роли, и я устрою тебе тренировочный прогон вопросов!",
        reply_markup=get_main_keyboard()
    )

# Обработка остальных текстовых сообщений (общение с нейросетью)
@dp.message(F.text)
async def handle_message(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY на сервере.")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=message.text,
            config=genai_types.GenerateContentConfig(
                system_instruction=(
                    "Ты профессиональный карьерный консультант, эксперт по трудоустройству "
                    "и развитию карьеры. Отвечай конструктивно, поддерживающе и по делу."
                ),
            ),
        )
        answer_text = response.text if response.text else "Не удалось получить ответ от модели."
        await message.answer(answer_text, reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Произошла ошибка при обращении к ИИ: {e}", reply_markup=get_main_keyboard())

async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот успешно запущен и готов к работе...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())