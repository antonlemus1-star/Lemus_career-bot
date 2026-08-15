import asyncio
import logging
import os
import sys
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from google import genai
from google.genai import types as genai_types

# Загружаем переменные окружения (токены и ключи подтягиваются из Render)
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    print("Ошибка: не задан BOT_TOKEN в переменных окружения!")
    sys.exit(1)

# Инициализируем бота и диспетчер
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализируем клиент Google Gemini (новый стандарт SDK)
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Обработчик команды /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "👋 Привет! Я твой персональный карьерный AI-помощник.\n\n"
        "Я готов помочь тебе с подготовкой к собеседованиям, разбором резюме "
        "и поиском новых карьерных возможностей. Напиши мне свой вопрос или задачу!"
    )
    await message.answer(welcome_text)

# Обработчик всех текстовых сообщений (общение с Gemini)
@dp.message(F.text)
async def handle_message(message: types.Message):
    if not ai_client:
        await message.answer("⚠️ Ошибка: Не настроен GEMINI_API_KEY на сервере.")
        return

    # Отправляем статус печати, пока нейросеть думает
    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        # Запрос к модели Gemini 2.5 Flash
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
        await message.answer(answer_text)
    except Exception as e:
        await message.answer(f"⚠️ Произошла ошибка при обращении к ИИ: {e}")

# Главная функция запуска бота
async def main():
    logging.basicConfig(level=logging.INFO)
    print("Бот успешно запущен и ожидает сообщения...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())