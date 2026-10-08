import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import html
import datetime
import aiohttp
import requests
import urllib.parse
from aiohttp import web
from docx import Document
from google import genai
from google.genai import types as gtypes

try:
    import pymupdf as fitz
except ImportError:
    import fitz

try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot_v32")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("🔴 BOT_TOKEN не задан!")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TELEGRAM_API = "https://api.telegram.org/bot" + BOT_TOKEN
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

GEMINI_MODEL_CANDIDATES = list(dict.fromkeys([
    os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
]))
GROQ_MODEL = "llama-3.1-8b-instant"

_working_model = {"name": None}
HTTP = None
TASKS = set()
temp_vacancies = {}
user_states = {}
user_adapt_target = {}
user_search_cache = {}
interview_sessions = {}
user_skillgap_cache = {}

# ============================================================
# 🎓 КУРСЫ ДЛЯ ПРЕМИУМ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================
COURSES = {
    "resume": {
        "title": "🎓 Резюме за 1 час",
        "description": "Пошаговый курс по созданию резюме, которое не отсеят роботы",
        "lessons": [
            {
                "title": "Урок 1: Структура резюме, которое пройдёт ATS",
                "content": """📚 УРОК 1: Структура резюме, которое пройдёт ATS

🎯 Цель: Понять как устроены системы автоматического отбора (ATS) и почему 70% резюме отсеиваются ещё до того, как их увидит человек.

💡 ГЛАВНОЕ:
Роботы на hh, в Сбербанке, Яндексе и других компаниях сканируют резюме по ключевым словам. Если в вашей вакансии написано "проектное управление", а у вас "менеджмент проектов" — робот может вас не увидеть.

✅ ПРАВИЛЬНАЯ СТРУКТУРА (именно в этом порядке):

1️⃣ ФИО и контакты (не креативьте, просто: Имя Фамилия, телефон, email)
2️⃣ Желаемая должность (точная, как в вакансии)
3️⃣ Краткое резюме / Summary (3-4 предложения о вашем главном УТП)
4️⃣ Ключевые навыки (списком, через запятую — роботы это любят)
5️⃣ Опыт работы (от последнего к первому)
6️⃣ Образование
7️⃣ Дополнительно (языки, сертификаты)

❌ ЧАСТЫЕ ОШИБКИ:
• Креативные заголовки ("Опытнейший профессионал") — робот их не понимает
• Таблицы и колонки — ломают парсинг
• Фото в файле (для hh не нужно)
• Формат .doc вместо .docx

📝 ЗАДАНИЕ:
Откройте своё резюме и проверьте:
1. Точное ли название должности как в целевой вакансии?
2. Есть ли раздел "Ключевые навыки" списком?
3. Нет ли таблиц и колонок?

⏱ Время выполнения: 10 минут"""
            },
            {
                "title": "Урок 2: Опыт через достижения, а не обязанности",
                "content": """📚 УРОК 2: Опыт через достижения

🎯 Цель: Научиться описывать опыт так, чтобы нанимающий менеджер увидел ценность, а не просто список обязанностей.

💡 ГЛАВНОЕ:
Разница между "делал" и "сделал" — это разница между "ещё один кандидат" и "хотим его взять".

❌ ПЛОХО (обязанности):
• Участвовал в разработке проектов
• Помогал с внедрением систем
• Отвечал за работу с клиентами

✅ ХОРОШО (достижения):
• Разработал и запустил 3 проекта с нуля, увеличив выручку на 47%
• Внедрил CRM-систему, сократив время обработки заявок на 35%
• Удержал 98% ключевых клиентов в период реструктуризации

📐 ФОРМУЛА ДОСТИЖЕНИЯ:
[Глагол действия] + [Что сделал] + [Измеримый результат]

Глаголы силы:
• Увеличил / Сократил / Запустил / Внедрил
• Автоматизировал / Оптимизировал / Построил
• Разработал / Создал / Внедрил / Запустил

🔢 ГДЕ ВЗЯТЬ ЦИФРЫ:
• Выручка / прибыль / экономия
• Время (сократил на Х часов/дней)
• Проценты (конверсия, удержание, рост)
• Количество (клиентов, проектов, сотрудников)
• Масштаб (бюджет, команда, территория)

Если точных цифр нет — оценивайте примерно, но честно.

📝 ЗАДАНИЕ:
Перепишите 3 пункта опыта из вашего резюме по формуле достижений. Используйте цифры.

⏱ Время выполнения: 15 минут"""
            },
            {
                "title": "Урок 3: Ключевые слова и ATS-оптимизация",
                "content": """📚 УРОК 3: Ключевые слова и ATS-оптимизация

🎯 Цель: Научиться встраивать правильные ключевые слова, чтобы роботы вас не отсеивали.

💡 ГЛАВНОЕ:
Каждая вакансия — это список требований. Ваша задача — отразить эти требования в резюме максимально близко по формулировкам.

🔍 КАК СОБРАТЬ КЛЮЧЕВЫЕ СЛОВА:

1️⃣ Откройте 5-10 целевых вакансий
2️⃣ Выпишите повторяющиеся требования:
   • Технические навыки (языки, инструменты)
   • Методологии (Agile, Scrum, Waterfall)
   • Типы задач (внедрение, миграция, оптимизация)
   • Отраслевые термины

3️⃣ Создайте список из 20-30 ключевых слов
4️⃣ Распределите их по резюме:
   • В "Ключевые навыки" — список
   • В опыт работы — в контексте достижений
   • В Summary — 3-5 самых важных

⚠️ ВАЖНО:
• Не вставляйте слова "для галочки" — пишите в контексте
• Используйте точные формулировки из вакансий
• Включайте и русские, и английские варианты

📋 ПРИМЕР СЕКЦИИ НАВЫКОВ:

Ключевые навыки:
• Проектное управление / Project Management
• Agile, Scrum, Kanban
• Внедрение информационных систем
• Управление бюджетом (до 100 млн руб)
• Ведение переговоров на уровне топ-менеджмента
• Управление командой до 15 человек

📝 ЗАДАНИЕ:
Соберите список ключевых слов из 5 вакансий и добавьте их в раздел "Ключевые навыки" вашего резюме.

⏱ Время выполнения: 15 минут"""
            },
            {
                "title": "Урок 4: Сопроводительное письмо за 10 минут",
                "content": """📚 УРОК 4: Сопроводительное письмо за 10 минут

🎯 Цель: Научиться писать сопроводительные, на которые отвечают.

💡 ГЛАВНОЕ:
90% сопроводительных писем — это шаблонный мусор: "Прошу рассмотреть мою кандидатуру". Их не читают.

Ваше письмо должно быть коротким, конкретным и цепляющим.

📐 ФОРМУЛА ИДЕАЛЬНОГО СОПРОВОДИТЕЛЬНОГО (4-5 предложений):

1️⃣ ЗАЦЕПКА (1 предложение)
Покажите что вы изучили компанию/вакансию:
"Увидел, что в [компания] ищут специалиста для [конкретная задача из вакансии]..."

2️⃣ РЕЛЕВАНТНЫЙ ОПЫТ (1-2 предложения)
Покажите что вы уже делали похожее:
"В прошлом квартале я [конкретное достижение с цифрами]..."

3️⃣ ЦЕННОСТЬ (1 предложение)
Покажите что вы дадите компании:
"Готов помочь вам [конкретный результат]..."

4️⃣ ПРИЗЫВ К ДЕЙСТВИЮ (1 предложение)
"Когда удобно созвониться на 15 минут?"

❌ ЧТО НЕ ПИСАТЬ:
• "Прошу рассмотреть мою кандидатуру"
• "Я коммуникабельный, стрессоустойчивый..."
• Пересказ резюме
• Больше 150 слов

✅ ПРИМЕР:

"Мария, добрый день!
Увидел, что в Сбере ищут менеджера проектов для развития экосистемы СНГ. В прошлом году я запустил 3 B2B-проекта в регионе (облако, Big Data), увеличив выручку на 47%. Готов обсудить, как мой опыт закроет ваши задачи по выходу на новые рынки. Когда удобно созвониться на 15 минут?"

📝 ЗАДАНИЕ:
Напишите сопроводительное письмо для конкретной вакансии по формуле выше. Используйте бота — он поможет сгенерировать питч.

⏱ Время выполнения: 10 минут"""
            },
            {
                "title": "Урок 5: Финальная проверка и стратегия отправки",
                "content": """📚 УРОК 5: Финальная проверка и стратегия отправки

🎯 Цель: Проверить резюме перед отправкой и понять как правильно его рассылать.

✅ ЧЕК-ЛИСТ ПЕРЕД ОТПРАВКОЙ:

📄 Формат:
• Файл в формате .docx или .pdf
• Размер до 2 МБ
• Название файла: "Фамилия_Имя_Должность" (не "резюме_финал_2")

📝 Содержание:
• Нет орфографических ошибок (проверьте дважды)
• Контакты актуальны
• Даты опыта сходятся
• Нет пустых разделов

🎯 Соответствие:
• Название должности как в вакансии
• Ключевые слова из вакансии присутствуют
• Опыт описан через достижения

📤 СТРАТЕГИЯ ОТПРАВКИ:

1️⃣ НЕ откликайтесь на всё подряд
Выберите 5-10 целевых вакансий в неделю

2️⃣ Адаптируйте резюме под каждую вакансию
Используйте функцию "Адаптация резюме" в боте

3️⃣ Отправляйте в правильное время
Вторник-четверг, 10:00-14:00 (не понедельник утром!)

4️⃣ Пишите сопроводительное
Даже 3 предложения повышают отклик в 2 раза

5️⃣ Отслеживайте отклики
Используйте Трекер в боте

📊 ОЖИДАЕМЫЕ ЦИФРЫ:
• Из 10 откликов: 3-5 просмотров
• Из 3-5 просмотров: 1-2 ответа
• Из 1-2 ответов: 0-1 собеседование

Это нормально. Поиск работы — воронка.

🎉 ПОЗДРАВЛЯЮ!
Вы прошли курс "Резюме за 1 час". Ваше резюме готово к бою!

Следующий шаг: Курс "Собеседование без стресса" или "Переговоры о зарплате".

⏱ Время выполнения: 20 минут"""
            }
        ]
    },
    "interview": {
        "title": "🎤 Собеседование без стресса",
        "description": "Как пройти любое собеседование уверенно",
        "lessons": [
            {
                "title": "Урок 1: Подготовка к собеседованию",
                "content": """📚 УРОК 1: Подготовка к собеседованию

🎯 Цель: Понять что происходит на собеседовании и как к нему подготовиться за 24 часа.

💡 ГЛАВНОЕ:
Собеседование — это не экзамен. Это переговоры двух сторон. Компания выбирает вас, но и ВЫ выбираете компанию.

📋 ЧТО СДЕЛАТЬ ЗА 24 ЧАСА:

1️⃣ ИЗУЧИТЕ КОМПАНИЮ (30 минут):
• Официальный сайт (что делают, кто клиенты)
• Новости за последние 3 месяца
• Отзывы сотрудников на Хабр Карьера / Dream Job
• Соцсети компании

2️⃣ ИЗУЧИТЕ ВАКАНСИЮ:
• Выпишите 3-5 главных требований
• Подумайте примеры из вашего опыта под каждое
• Приготовьте 2-3 вопроса о роли

3️⃣ ПОДГОТОВЬТЕ САМОПРЕЗЕНТАЦИЮ:
Классический вопрос "Расскажите о себе" требует заготовки на 2 минуты:
• Кто вы (1 предложение)
• Ваш опыт (2-3 предложения)
• Главное достижение (1-2 предложения)
• Почему эта роль (1 предложение)

4️⃣ ТЕХНИЧЕСКИЕ МОМЕНТЫ:
• Проверьте связь (если онлайн)
• Подготовьте воду и блокнот
• Оденьтесь чуть лучше чем требуется

🎯 ТИПИЧНЫЕ ВОПРОСЫ НА ПОДГОТОВКУ:

• Расскажите о себе
• Почему уходите с текущего места?
• Почему хотите работать именно у нас?
• Ваши сильные/слабые стороны?
• Где вы видите себя через 5 лет?
• Расскажите о конфликте на работе и как его решили
• Ваша самая большая неудача?

📝 ЗАДАНИЕ:
Подготовьте ответы на все вопросы выше. Запишите их в заметки.

⏱ Время выполнения: 45 минут"""
            },
            {
                "title": "Урок 2: Как отвечать на каверзные вопросы",
                "content": """📚 УРОК 2: Каверзные вопросы

🎯 Цель: Научиться отвечать на неудобные вопросы без паники.

💡 ГЛАВНОЕ:
Каверзные вопросы проверяют не ваши знания, а вашу реакцию. Спокойный уверенный ответ важнее идеального содержания.

🔥 ТОП-5 КАВЕРЗНЫХ ВОПРОСОВ:

1️⃣ "Почему вы ушли с прошлого места?"
❌ Плохо: "Начальник был идиот"
✅ Хорошо: "Достиг потолка в развитии, ищу новые вызовы и рост"

2️⃣ "Какая ваша самая большая слабость?"
❌ Плохо: "Я перфекционист" (все это говорят)
✅ Хорошо: "Раньше плохо делегировал, но научился: теперь трачу 30% времени на обучение команды, и это даёт результат"

3️⃣ "Почему мы должны взять именно вас?"
✅ Формула: Опыт + Достижение + Мотивация
"Потому что у меня есть опыт [Х], я достиг [результат с цифрами], и мне интересна именно ваша задача [конкретика]"

4️⃣ "Где вы видите себя через 5 лет?"
❌ Плохо: "На вашем месте"
✅ Хорошо: "Хочу вырасти в эксперта уровня [Х], вести проекты масштаба [У] и развивать команду"

5️⃣ "Расскажите о своей самой большой неудаче"
✅ Формула: Ситуация → Действия → Урок
"В проекте [Х] я недооценил сроки. Понял это через неделю, сразу поднял вопрос, пересобрали план. Вывод: теперь закладываю +20% на риски"

📝 ЗАДАНИЕ:
Выберите 3 самых сложных вопроса из списка и запишите свои ответы на диктофон. Прослушайте и улучшите.

⏱ Время выполнения: 30 минут"""
            },
            {
                "title": "Урок 3: Вопросы о зарплате и ожиданиях",
                "content": """📚 УРОК 3: Вопросы о зарплате

🎯 Цель: Научиться отвечать на вопросы о зарплате без потери денег.

💡 ГЛАВНОЕ:
Первый, кто называет цифру — проигрывает в переговорах. Но на собеседовании избежать вопроса сложно.

🎯 СТРАТЕГИЯ ОТВЕТА:

Вариант 1: "Какая у вас вилка?"
"Мне интересно понять, какая вилка предусмотрена для этой позиции. Можете сориентировать?"

Вариант 2: Диапазон
"Ориентируюсь на диапазон [Х-У], в зависимости от полного пакета: бонусов, ДМС, опционов"

Вариант 3: "От чего зависит?"
"Зависит от задач. Если роль предполагает [Х], то [цифра]. Если [У], то [цифра]"

📊 КАК ОПРЕДЕЛИТЬ СВОЮ СТОИМОСТЬ:

1️⃣ Изучите рынок:
• Хабр Карьера — опросы зарплат
• Доу.ру — зарплаты в ИТ
• Простые расчёты: текущая × 1.2-1.5

2️⃣ Добавьте стоимость "бонусов":
• ДМС = +5-10%
• Опционы = +10-30%
• Удалёнка = +10% (экономия на офисе)

3️⃣ Назовите цифру выше желаемой на 15-20%

📝 ЗАДАНИЕ:
Определите свою рыночную стоимость и подготовьте диапазон для ответа.

⏱ Время выполнения: 20 минут"""
            },
            {
                "title": "Урок 4: Как произвести впечатление",
                "content": """📚 УРОК 4: Как произвести впечатление

🎯 Цель: Выделиться среди других кандидатов.

💡 ГЛАВНОЕ:
Запоминаются не те, кто лучше всех ответил на вопросы. Запоминаются те, кто задал лучшие вопросы.

🎯 СИЛЬНЫЕ ВОПРОСЫ РАБОТОДАТЕЛЮ:

О роли:
• "Как выглядит успех в этой роли через 3 месяца?"
• "Какая главная задача будет стоять в первые 90 дней?"
• "Какие метрики используются для оценки успеха?"

О команде:
• "Кто мой непосредственный руководитель? Как с ним работается?"
• "Какая атмосфера в команде?"
• "Как распределяются задачи?"

О компании:
• "Что вам больше всего нравится в работе здесь?"
• "Какая главная проблема компании сейчас?"
• "Как компания относится к ошибкам сотрудников?"

🚀 КАК ВЫДЕЛИТЬСЯ:

1️⃣ ПРИНЕСИТЕ ЧТО-ТО ДОПОЛНИТЕЛЬНОЕ:
• Распечатанное резюме (даже если онлайн)
• Портфолио / примеры работ
• Блокнот с записями

2️⃣ ПОКАЖИТЕ ИНТЕРЕС:
• Упомяните конкретный продукт компании
• Спросите о планах развития
• Покажите что изучили сайт

3️⃣ БУДЬТЕ ЭНЕРГИЧНЫМ:
• Улыбайтесь (даже на видео)
• Говорите уверенно
• Не бойтесь пауз

📝 ЗАДАНИЕ:
Подготовьте 5 вопросов для вашего следующего собеседования.

⏱ Время выполнения: 15 минут"""
            },
            {
                "title": "Урок 5: После собеседования",
                "content": """📚 УРОК 5: После собеседования

🎯 Цель: Правильно завершить процесс и получить оффер.

💡 ГЛАВНОЕ:
Собеседование не заканчивается когда вы выходите из офиса. Фоллоу-ап повышает шансы на 30%.

📋 ЧТО СДЕЛАТЬ ПОСЛЕ СОБЕСЕДОВАНИЯ:

1️⃣ В ТЕЧЕНИЕ 24 ЧАСОВ:
Напишите благодарность:

"Добрый день, [Имя]!
Спасибо за время и интересную беседу. Очень впечатлила задача [конкретика]. Ещё раз подтверждаю свой интерес к роли. Буду рад продолжить общение.
С уважением, [Имя]"

2️⃣ ЧЕРЕЗ 3-5 ДНЕЙ (если тишина):
Вежливый фоллоу-ап:

"Добрый день, [Имя]!
Хотел уточнить статус по моей кандидатуре. Готов ответить на дополнительные вопросы или предоставить рекомендации.
С уважением, [Имя]"

3️⃣ ЕСЛИ ПОЛУЧИЛИ ОФФЕР:
• Не соглашайтесь сразу
• Возьмите 2-3 дня на раздумья
• Сравните с другими предложениями
• Проверьте условия

4️⃣ ЕСЛИ ОТКАЗАЛИ:
• Попросите обратную связь
• Сохраняйте контакт
• Не воспринимайте лично

🎉 ПОЗДРАВЛЯЮ!
Вы прошли курс "Собеседование без стресса".

⏱ Время выполнения: 20 минут"""
            }
        ]
    },
    "salary": {
        "title": "💰 Переговоры о зарплате",
        "description": "Как получить максимум от оффера",
        "lessons": [
            {
                "title": "Урок 1: Определите свою рыночную стоимость",
                "content": """📚 УРОК 1: Определите свою стоимость

🎯 Цель: Понять сколько вы стоите на рынке и обосновать это цифрами.

💡 ГЛАВНОЕ:
Зарплата — это не то, что вам "нужно". Это то, что рынок готов платить за ваш набор навыков.

📊 КАК ОПРЕДЕЛИТЬ СТОИМОСТЬ:

1️⃣ ОПРОСЫ И АНАЛИТИКА:
• Хабр Карьера — опросы зарплат в ИТ
• Доу.ру — зарплаты разработчиков
• Уровень.ру — зарплаты по компаниям
• Простые калькуляторы на hh

2️⃣ СОБЕСЕДОВАНИЯ КАК РАЗВЕДКА:
Сходите на 2-3 собеседования даже если не хотите менять работу. Спросите вилку.

3️⃣ ФОРМУЛА РАСЧЁТА:

Базовая стоимость = средняя по рынку
+ Премия за опыт = +10-20% за каждые 2 года сверх минимума
+ Премия за редкие навыки = +15-30%
+ Премия за результаты = +10-25% если есть цифры
- Дисконт за срочность = -10-20% если нужно срочно

4️⃣ УЧТИТЕ ПОЛНЫЙ ПАКЕТ:

Зарплата ≠ деньги на руки

Полный пакет включает:
• Оклад
• Бонусы (годовой, квартальный, проектный)
• ДМС (экономия 50-150к в год)
• Опционы / акции
• Обучение (курсы, конференции)
• Удалёнка (экономия на транспорте и еде)
• Гибкий график
• Отпуск (28+ дней = больше денег)

📝 ЗАДАНИЕ:
Рассчитайте свою рыночную стоимость по формуле выше. Запишите минимальную, желаемую и "мечту" цифры.

⏱ Время выполнения: 30 минут"""
            },
            {
                "title": "Урок 2: Когда и как говорить о зарплате",
                "content": """📚 УРОК 2: Когда говорить о зарплате

🎯 Цель: Выбрать правильное время и способ обсуждения зарплаты.

💡 ГЛАВНОЕ:
Лучшее время для разговора о зарплате — когда работодатель уже принял решение вас нанять, но ещё не назвал цифру.

📅 ЭТАПЫ ПЕРЕГОВОРОВ:

ЭТАП 1: Первый контакт (скрининг)
Вопрос: "Какая у вас ожидаемая зарплата?"
Ответ: "Мне интересно понять вашу вилку. Можете сориентировать?"

ЭТАП 2: Первое собеседование
Если настаивают:
"Ориентируюсь на диапазон [Х-У], в зависимости от полного пакета"

ЭТАП 3: Финальное собеседование
Здесь уже можно обсуждать детали:
"Учитывая мой опыт [Х] и результаты [У], я ориентируюсь на [цифра]"

ЭТАП 4: Оффер
Время для торга:
"Спасибо за оффер! Могу ли я уточнить несколько моментов..."

🎯 ПРАВИЛА ПЕРЕГОВОРОВ:

1️⃣ НИКОГДА не называйте первым
2️⃣ Всегда давайте ДИАПАЗОН
3️⃣ Верхняя граница = ваша мечта
4️⃣ Ссылайтесь на рынок
5️⃣ Не извиняйтесь

📝 ЗАДАНИЕ:
Подготовьте скрипт ответа на вопрос о зарплате для каждого из 4 этапов.

⏱ Время выполнения: 20 минут"""
            },
            {
                "title": "Урок 3: Техники переговоров",
                "content": """📚 УРОК 3: Техники переговоров

🎯 Цель: Освоить конкретные приёмы торга.

💡 ГЛАВНОЕ:
Переговоры о зарплате — это не конфликт. Это поиск взаимовыгодного решения.

🔧 ТЕХНИКА 1: "ЯКОРЬ"

Первый названный ценой — это якорь. От него отталкиваются.

Если работодатель назвал 120к, а вы хотите 160к:
"Спасибо за предложение. Учитывая мой опыт [Х] и результаты [У], я ориентировался на 160-170к. Давайте обсудим, как мы можем к этому прийти"

🔧 ТЕХНИКА 2: "ПАУЗА"

После того как назвали цифру — молчите.
Не оправдывайтесь. Просто ждите ответа. Тишина работает на вас.

🔧 ТЕХНИКА 3: "АЛЬТЕРНАТИВЫ"

Всегда имейте запасные варианты:
• Другой оффер
• Контр-аргументы (опыт, результаты)
• Другие компании в процессе

🔧 ТЕХНИКА 4: "УСЛОВИЯ"

Торгуйтесь не только за оклад:
• Годовой бонус
• Опционы / акции
• ДМС для семьи
• Обучение / конференции
• Удалёнка
• Гибкий график
• Отпуск

🔧 ТЕХНИКА 5: "КОНТРОФФЕР"

Если текущий работодатель сделал контроффер:
• Сравните честно: не только деньги, но и рост, задачи, команду
• Контроффер часто = временное решение
• Подумайте что будет через 6 месяцев

📝 ЗАДАНИЕ:
Потренируйтесь отвечать на 3 фразы давления перед зеркалом или с другом.

⏱ Время выполнения: 25 минут"""
            },
            {
                "title": "Урок 4: Торг за бонусы и условия",
                "content": """📚 УРОК 4: Торг за бонусы и условия

🎯 Цель: Получить максимум от полного пакета компенсации.

💡 ГЛАВНОЕ:
Оклад — это только 60-70% от реальной стоимости. Остальное — бонусы, льготы и условия.

💰 ЧТО МОЖНО ВЫТОРГОВАТЬ:

1️⃣ ГОДОВОЙ БОНУС:
• Обычно 10-30% от оклада
• Уточните условия получения
• Попросите прописать в договоре

2️⃣ ОПЦИОНЫ / АКЦИИ:
• Особенно важно для стартапов и ИТ
• Уточните вестинг (когда станут доступны)

3️⃣ ДМС:
• Для вас и семьи
• Стоматология
• Психолог
• Спорт

4️⃣ ОБУЧЕНИЕ:
• Курсы, конференции, книги
• Бюджет: 50-200к в год

5️⃣ УСЛОВИЯ РАБОТЫ:
• Удалёнка (полная / гибридная)
• Гибкий график
• Парковка

6️⃣ ОТПУСК:
• Стандарт 28 дней
• Можно договориться о 30-35

📋 КАК ВЕСТИ ТОРГ ЗА ПАКЕТ:

ШАГ 1: Получите базовый оффер
ШАГ 2: Уточните все детали
ШАГ 3: Сравните с вашими ожиданиями
ШАГ 4: Предложите варианты
ШАГ 5: Получите письменное подтверждение

📝 ЗАДАНИЕ:
Составьте список из 5 пунктов, которые вы хотите выторговать кроме оклада.

⏱ Время выполнения: 15 минут"""
            },
            {
                "title": "Урок 5: Контр-оффер и финальное решение",
                "content": """📚 УРОК 5: Контр-оффер и финальное решение

🎯 Цель: Принять лучшее решение и правильно завершить переговоры.

💡 ГЛАВНОЕ:
Контроффер — это предложение от текущего работодателя, чтобы вас удержать.

📊 КОГДА КОНТРОФФЕР ХОРОШ:

✅ Оставайтесь, если:
• Деньги — единственная проблема
• Вам нравится команда и задачи
• Есть рост и развитие
• Вы не нашли лучшего предложения

❌ Уходите, если:
• Проблема в руководстве или культуре
• Вы выгорели
• Нет роста
• Вас не ценят

📋 КАК ПРИНИМАТЬ ФИНАЛЬНОЕ РЕШЕНИЕ:

ШАГ 1: Возьмите паузу (2-3 дня)
ШАГ 2: Сравните все варианты
ШАГ 3: Поговорите с близкими
ШАГ 4: Примите решение

📝 КАК ОТКАЗАТЬСЯ ОТ КОНТРОФФЕРА:

"Спасибо за предложение и за то, что цените мой вклад. Я много думал и решил двигаться дальше. Ухожу с благодарностью и готов помочь с передачей дел"

📝 КАК ПРИНЯТЬ ОФФЕР:

"Спасибо! Принимаю ваше предложение с зарплатой [Х] и условиями [У]. Когда выходим?"

🎉 ПОЗДРАВЛЯЮ!
Вы прошли курс "Переговоры о зарплате".

Удачи в переговорах! 💪

⏱ Время выполнения: 15 минут"""
            }
        ]
    }
}

# ============================================================
# 📝 ШАБЛОНЫ СОПРОВОДИТЕЛЬНЫХ ПИСЕМ (10 штук)
# ============================================================
COVER_LETTER_TEMPLATES = [
    {
        "name": "🎯 Классический (для любой позиции)",
        "content": """Добрый день, [Имя]!

Увидел вакансию [позиция] в [компания] и очень заинтересовался.

Мой опыт в [область] составляет [Х] лет. За это время я [главное достижение с цифрами].

Готов обсудить, как мой опыт поможет решить ваши задачи по [конкретная задача из вакансии].

Когда удобно созвониться на 15 минут?

С уважением,
[Имя]"""
    },
    {
        "name": "🔥 Цепляющий (для стартапов)",
        "content": """[Имя], добрый день!

Увидел, что [компания] ищет [позиция]. Это именно то, чем я горю.

В прошлом году я [конкретное достижение с цифрами]. Готов повторить и улучшить этот результат у вас.

Есть пара идей, как можно [решить задачу из вакансии]. Могу рассказать на коротком созвоне.

Когда удобно?

[Имя]"""
    },
    {
        "name": "💼 Для крупных корпораций (Сбер, Яндекс, Тинькофф)",
        "content": """Добрый день, [Имя]!

Меня зовут [Имя], я [должность] с опытом [Х] лет в [область].

Увидел вакансию [позиция] в [компания]. Мой опыт в [конкретная область] и результаты ([цифры]) соответствуют вашим требованиям.

Особенно интересна задача [конкретика из вакансии] — я решал похожую в [предыдущая компания].

Готов обсудить детали на встрече.

С уважением,
[Имя]"""
    },
    {
        "name": "🚀 Для перехода из другой отрасли",
        "content": """Добрый день, [Имя]!

Я [Х] лет работал в [отрасль], где научился [навык 1] и [навык 2].

Теперь хочу применить этот опыт в [новая отрасль]. Ваш проект [конкретика] идеально подходит для этого.

В [предыдущая компания] я [достижение, релевантное новой роли].

Готов рассказать, как мой опыт из [отрасль] поможет вам в [задача].

Когда удобно созвониться?

[Имя]"""
    },
    {
        "name": "📊 Для аналитиков и дата-сайентистов",
        "content": """Добрый день, [Имя]!

Увидел вакансию [позиция] в [компания].

Мой опыт: [Х] лет в анализе данных, [конкретный инструмент/язык].

Недавний проект: [краткое описание с метриками]. Например, я [конкретный результат с цифрами].

Интересна ваша задача [конкретика из вакансии]. Готов показать примеры работ на встрече.

Когда удобно?

[Имя]"""
    },
    {
        "name": "👥 Для менеджеров и руководителей",
        "content": """Добрый день, [Имя]!

Меня зовут [Имя], я руководитель с опытом [Х] лет в [область].

В [компания] я управлял командой из [Х] человек и достиг [результат с цифрами].

Вижу, что [компания] ищет [позиция] для [задача]. Мой опыт в [конкретная область] и результаты ([цифры]) помогут достичь ваших целей.

Готов обсудить, как могу усилить вашу команду.

С уважением,
[Имя]"""
    },
    {
        "name": "🎓 Для джуниоров и смены карьеры",
        "content": """Добрый день, [Имя]!

Меня зовут [Имя]. Я начинающий [должность] с большим желанием расти.

Прошёл [курсы/обучение], где научился [навыки]. В качестве проекта сделал [конкретный результат].

Понимаю, что у меня нет опыта [Х] лет, но я быстро учусь и мотивирован. Готов выполнить тестовое задание.

Буду благодарен за возможность обсудить позицию.

[Имя]"""
    },
    {
        "name": "💻 Для разработчиков",
        "content": """Добрый день, [Имя]!

Увидел вакансию [позиция] в [компания].

Мой стек: [языки/фреймворки]. Опыт [Х] лет.

Недавний проект: [краткое описание]. Например, я [конкретный результат: ускорил, оптимизировал, внедрил].

Интересен ваш проект [конкретика]. Готов показать код или портфолио на встрече.

Когда удобно?

[Имя]"""
    },
    {
        "name": "🌟 Для отклика на пост в соцсетях",
        "content": """[Имя], добрый день!

Увидел ваш пост о поиске [позиция] в [компания].

Я [кратко о себе: Х лет в области, главное достижение].

Мой опыт в [конкретная область] и результаты ([цифры]) соответствуют вашим требованиям.

Готов рассказать подробнее на коротком созвоне. Когда удобно?

[Имя]"""
    },
    {
        "name": "📧 Короткий (для мессенджеров)",
        "content": """[Имя], добрый день!

Увидел вакансию [позиция]. Мой опыт [Х] лет в [область], [главное достижение].

Готов обсудить детали. Когда удобно созвониться на 10 минут?

[Имя]"""
    }
]

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, 
    username TEXT,
    balance INTEGER DEFAULT 7,
    unlimited_until TIMESTAMP,
    daily_count INTEGER DEFAULT 0,
    last_active_date TEXT,
    referred_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT,
    text TEXT,
    active INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hidden_vacancies (
    user_id INTEGER,
    vacancy_id TEXT,
    PRIMARY KEY (user_id, vacancy_id)
);
CREATE TABLE IF NOT EXISTS liked_vacancies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    vacancy_id TEXT,
    title TEXT,
    status TEXT DEFAULT 'Откликнулся'
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS social_shares (
    user_id INTEGER,
    network TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, network)
);
CREATE TABLE IF NOT EXISTS free_actions (
    user_id INTEGER,
    action_type TEXT,
    used_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, action_type)
);
CREATE TABLE IF NOT EXISTS premium_access (
    user_id INTEGER PRIMARY KEY,
    unlocked_courses TEXT DEFAULT '',
    unlocked_templates INTEGER DEFAULT 0,
    analytics_enabled INTEGER DEFAULT 0,
    early_access INTEGER DEFAULT 0
);
""")
conn.commit()


def register_user(user_id: int, username: str, referrer_id: int = None) -> bool:
    cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    if cur.fetchone():
        return False
    if referrer_id == user_id:
        referrer_id = None
    if referrer_id:
        cur.execute("SELECT 1 FROM users WHERE user_id=?", (referrer_id,))
        if not cur.fetchone():
            referrer_id = None
    initial_balance = 7
    cur.execute(
        "INSERT INTO users (user_id, username, balance, referred_by) VALUES (?, ?, ?, ?)",
        (user_id, username, initial_balance, referrer_id)
    )
    conn.commit()
    if referrer_id:
        cur.execute("UPDATE users SET balance = balance + 7 WHERE user_id=?", (referrer_id,))
        conn.commit()
    return True


def get_user_data(user_id: int):
    cur.execute("SELECT balance, unlimited_until, daily_count, last_active_date FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        return {"balance": 7, "unlimited_until": None, "daily_count": 0, "last_active_date": ""}
    return {"balance": row[0], "unlimited_until": row[1], "daily_count": row[2], "last_active_date": row[3]}


def is_premium_user(user_id: int) -> bool:
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        return True
    data = get_user_data(user_id)
    if data["unlimited_until"]:
        cur.execute("SELECT datetime('now') < datetime(?)", (data["unlimited_until"],))
        if cur.fetchone()[0]:
            return True
    return False


def check_free_action(user_id: int, action_type: str, max_free: int = 1) -> bool:
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        return True
    if is_premium_user(user_id):
        return True
    
    cur.execute("SELECT used_count FROM free_actions WHERE user_id=? AND action_type=?", 
                (user_id, action_type))
    row = cur.fetchone()
    
    if not row:
        cur.execute("INSERT INTO free_actions (user_id, action_type, used_count) VALUES (?, ?, 1)", 
                    (user_id, action_type))
        conn.commit()
        return True
    
    if row[0] < max_free:
        cur.execute("UPDATE free_actions SET used_count = used_count + 1 WHERE user_id=? AND action_type=?", 
                    (user_id, action_type))
        conn.commit()
        return True
    
    return False


def get_free_action_count(user_id: int, action_type: str) -> int:
    cur.execute("SELECT used_count FROM free_actions WHERE user_id=? AND action_type=?", 
                (user_id, action_type))
    row = cur.fetchone()
    return row[0] if row else 0


def spend_balance(user_id: int, cost: int = 1) -> bool:
    if ADMIN_ID != 0 and user_id == ADMIN_ID:
        return True
    if is_premium_user(user_id):
        return True
        
    data = get_user_data(user_id)
    unlimited_until = data["unlimited_until"]
    if unlimited_until:
        cur.execute("SELECT datetime('now') < datetime(?)", (unlimited_until,))
        is_active_sub = cur.fetchone()[0]
        if is_active_sub:
            last_date = data["last_active_date"]
            daily_count = data["daily_count"]
            today_str = datetime.date.today().isoformat()
            if last_date != today_str:
                cur.execute("UPDATE users SET daily_count=1, last_active_date=? WHERE user_id=?", (today_str, user_id))
                conn.commit()
                return True
            elif daily_count < 50:
                cur.execute("UPDATE users SET daily_count = daily_count + 1 WHERE user_id=?", (user_id,))
                conn.commit()
                return True
            else:
                return False
    balance = data["balance"]
    if balance < cost:
        return False
    cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (cost, user_id))
    conn.commit()
    return True


def admin_add_balance(user_id: int, amount: int) -> int:
    cur.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
    conn.commit()
    return get_user_data(user_id)["balance"]


def admin_set_unlimited(user_id: int, days: int = 10):
    cur.execute("UPDATE users SET unlimited_until = datetime('now', '+' || ? || ' days') WHERE user_id=?", (days, user_id))
    conn.commit()


def add_resume(user_id: int, name: str, text: str):
    cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (user_id,))
    cur.execute("INSERT INTO resumes (user_id, name, text, active) VALUES (?,?,?,1)",
                (user_id, name, text[:25000]))
    conn.commit()


def list_resumes(user_id: int):
    cur.execute("SELECT id, name, active FROM resumes WHERE user_id=? ORDER BY id", (user_id,))
    return [{"id": r[0], "name": r[1], "active": r[2]} for r in cur.fetchall()]


def get_active_resume(user_id: int) -> str:
    cur.execute("SELECT text FROM resumes WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT text FROM resumes WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,))
        row = cur.fetchone()
    return row[0] if row else ""


def get_resume_by_id(user_id: int, resume_id: int) -> str:
    cur.execute("SELECT text FROM resumes WHERE id=? AND user_id=?", (resume_id, user_id))
    row = cur.fetchone()
    return row[0] if row else ""


def hide_vacancy(user_id: int, vacancy_id: str):
    cur.execute("INSERT OR IGNORE INTO hidden_vacancies (user_id, vacancy_id) VALUES (?, ?)", (user_id, vacancy_id))
    conn.commit()


def is_vacancy_hidden(user_id: int, vacancy_id: str) -> bool:
    cur.execute("SELECT 1 FROM hidden_vacancies WHERE user_id=? AND vacancy_id=?", (user_id, vacancy_id))
    return cur.fetchone() is not None


def like_vacancy(user_id: int, vacancy_id: str, title: str):
    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Откликнулся')",
                (user_id, vacancy_id, title))
    conn.commit()


# ---------------- ИИ-слой ----------------
def _openai_compat(prompt: str, base: str, key: str, model: str) -> str:
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "temperature": 0.7,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def ai_generate(prompt: str):
    if client:
        cands = [_working_model["name"]] if _working_model["name"] else GEMINI_MODEL_CANDIDATES
        for m in cands:
            try:
                resp = client.models.generate_content(
                    model=m, contents=prompt,
                    config=gtypes.GenerateContentConfig(temperature=0.7))
                if resp is not None and resp.text:
                    _working_model["name"] = m
                    log.info("AI ok: gemini/%s", m)
                    return resp.text
            except Exception as e:
                log.warning("Gemini model %s failed: %s", m, str(e)[:100])
    if GROQ_KEY:
        try:
            log.info("AI ok: groq/%s", GROQ_MODEL)
            return _openai_compat(prompt, "https://api.groq.com/openai/v1", GROQ_KEY, GROQ_MODEL)
        except Exception as e:
            log.warning("Groq failed: %s", str(e)[:100])
    if OPENROUTER_KEY:
        try:
            log.info("AI ok: openrouter/qwen")
            return _openai_compat(prompt, "https://openrouter.ai/api/v1", OPENROUTER_KEY, "qwen/qwen-2.5-7b-instruct:free")
        except Exception as e:
            log.warning("OpenRouter failed: %s", str(e)[:100])
    return None


# ---------------- Извлечение текста ----------------
def rtf_to_text(raw: str) -> str:
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: bytes.fromhex(m.group(1)).decode("cp1251", errors="ignore"), raw)
    text = re.sub(r"\\[a-z]+-?\d* ?", " ", text)
    text = re.sub(r"[{}]", "", text)
    return html.unescape(text).strip()


def extract_text(path: str, file_name: str) -> str:
    fn = file_name.lower()
    text_content = ""
    try:
        if fn.endswith(".pdf"):
            doc = fitz.open(path)
            text_content = "\n".join(page.get_text("text") for page in doc)
        elif fn.endswith(".docx"):
            doc = Document(path)
            text_content = "\n".join(p.text for p in doc.paragraphs if p.text)
        elif fn.endswith(".rtf"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text_content = rtf_to_text(f.read())
        elif fn.endswith(".txt"):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text_content = f.read()
    except Exception as e:
        log.error("extract_text failed for %s: %s", file_name, e)
    return text_content.strip()


# ---------------- Telegram helpers ----------------
def bg(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    def _handle_task_result(task):
        TASKS.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            coro_name = getattr(coro, '__name__', str(coro))
            log.error(f"Фоновая ошибка в задаче {coro_name}: {e}", exc_info=True)
    t.add_done_callback(_handle_task_result)
    return t


_seen_updates = set()


def is_duplicate(update_id) -> bool:
    if update_id is None:
        return False
    if update_id in _seen_updates:
        return True
    _seen_updates.add(update_id)
    if len(_seen_updates) > 5000:
        for uid in sorted(_seen_updates)[:-2500]:
            _seen_updates.discard(uid)
    return False


async def send_single_message(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with HTTP.post(f"{TELEGRAM_API}/sendMessage", json=payload) as resp:
        result = await resp.json()
    if not result.get("ok") and parse_mode:
        payload.pop("parse_mode", None)
        async with HTTP.post(f"{TELEGRAM_API}/sendMessage", json=payload) as resp:
            result = await resp.json()
    return result


async def send_telegram(chat_id, text: str, reply_markup=None, parse_mode="Markdown"):
    if not text:
        return
    if len(text) <= 4000:
        return await send_single_message(chat_id, text, reply_markup, parse_mode)
    parts = []
    current_part = ""
    for paragraph in text.split("\n\n"):
        if len(current_part) + len(paragraph) + 2 < 3800:
            current_part += ("\n\n" if current_part else "") + paragraph
        else:
            if current_part:
                parts.append(current_part)
            current_part = paragraph
    if current_part:
        parts.append(current_part)
    for i, p in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        await send_single_message(chat_id, p, markup, parse_mode)
        await asyncio.sleep(0.3)


async def send_document_bytes(chat_id, file_bytes: bytes, filename: str, caption: str = "",
                              content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    if caption:
        form.add_field("caption", caption[:1024])
        form.add_field("parse_mode", "Markdown")
    form.add_field("document", file_bytes, filename=filename, content_type=content_type)
    async with HTTP.post(f"{TELEGRAM_API}/sendDocument", data=form) as resp:
        await resp.json()


async def http_edit_message_text(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with HTTP.post(f"{TELEGRAM_API}/editMessageText", json=payload) as resp:
        await resp.json()


async def answer_callback(cb_id: str):
    try:
        async with HTTP.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id}) as resp:
            await resp.json()
    except Exception:
        pass


def get_keyboard(is_admin=False):
    kb = [
        [{"text": "📁 Мои резюме"}, {"text": "📥 Загрузить резюме"}],
        [{"text": "🔍 Поиск вакансий"}, {"text": "🌐 Вакансии из Сетки"}],
        [{"text": "🔗 Разобрать вакансию"}, {"text": "🕵️ Найти HR / ЛПР"}],
        [{"text": "🛠 Адаптация резюме"}, {"text": "📋 Аудит резюме"}],
        [{"text": "📊 Анализ навыков (Skill Gap)"}, {"text": "📝 Короткие Питчи"}],
        [{"text": "🎤 Тренажер собеседований"}, {"text": "📌 Трекер откликов"}],
        [{"text": "🎓 Курсы (Премиум)"}, {"text": "📝 Шаблоны писем (Премиум)"}],
        [{"text": "📊 Аналитика (Премиум)"}, {"text": "🎯 План поиска (Премиум)"}],
        [{"text": "💎 Оплата и Баланс"}, {"text": "⏰ Продлить доступ"}],
        [{"text": "🎁 Бонусы (Репост & Друзья)"}, {"text": "💬 Обратная связь"}],
        [{"text": "🚀 Запустить бота"}, {"text": "ℹ️ Помощь"}],
    ]
    if is_admin:
        kb.append([{"text": "👑 Админ-панель"}, {"text": "📩 Сообщения от пользователей"}])
    return {"keyboard": kb, "resize_keyboard": True}


# ---------------- hh.ru парсинг (с антибаном) ----------------
async def hh_api_search(query: str):
    try:
        async with HTTP.get("https://api.hh.ru/vacancies",
                            params={"text": query, "area": "1", "per_page": "50"},
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}) as resp:
            if resp.status == 429:
                log.warning("hh.ru rate limit hit, waiting 5 seconds")
                await asyncio.sleep(5)
                return None
            if resp.status != 200:
                log.warning(f"hh.ru API returned {resp.status}")
                return None
            data = await resp.json()
        items = []
        for i in data.get("items", []):
            salary = i.get("salary")
            sal_str = ""
            if salary:
                frm = salary.get("from")
                to = salary.get("to")
                cur_s = salary.get("currency", "RUR")
                if frm and to: sal_str = f"💰 {frm} – {to} {cur_s}"
                elif frm: sal_str = f"💰 от {frm} {cur_s}"
                elif to: sal_str = f"💰 до {to} {cur_s}"
            items.append({
                "id": i.get("id"),
                "name": i.get("name"),
                "company": (i.get("employer") or {}).get("name"),
                "salary": sal_str,
                "url": i.get("alternate_url") or f"https://hh.ru/vacancy/{i.get('id')}"
            })
        return items or None
    except Exception as e:
        log.warning("hh API failed: %s", str(e)[:150])
        return None


async def hh_scrape_search(query: str):
    try:
        async with HTTP.get("https://hh.ru/search/vacancy",
                            params={"text": query, "area": "1", "items_on_page": "50"},
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}) as resp:
            if resp.status != 200:
                log.warning(f"hh scrape returned {resp.status}")
                return None
            page = await resp.text()
        match = re.search(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', page, re.S)
        if not match:
            return None
        data = json.loads(html.unescape(match.group(1)))
        items = (data.get("vacancySearchResult") or {}).get("vacancies") or []
        out = []
        for it in items[:50]:
            vid = it.get("vacancyId") or it.get("id")
            sal = it.get("salary")
            sal_str = f"💰 {sal}" if sal else ""
            out.append({
                "id": vid,
                "name": it.get("name"),
                "company": (it.get("company") or {}).get("name"),
                "salary": sal_str,
                "url": f"https://hh.ru/vacancy/{vid}"
            })
        return out or None
    except Exception as e:
        log.warning("hh scrape failed: %s", str(e)[:150])
        return None


# 🆕 УЛУЧШЕННАЯ ФУНКЦИЯ С 5-УРОВНЕВЫМ ПАРСИНГОМ
async def get_vacancy_full_details(vacancy_id: str) -> dict:
    result = {
        "id": vacancy_id, "title": "", "company": "", "description": "",
        "skills": "", "contact_name": "", "contact_email": "", "contact_phone": "",
        "url": f"https://hh.ru/vacancy/{vacancy_id}"
    }
    
    # === УРОВЕНЬ 1: Официальный API с retry ===
    for attempt in range(3):
        try:
            async with HTTP.get(f"https://api.hh.ru/vacancies/{vacancy_id}",
                                headers={
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                    "Accept": "application/json",
                                    "Accept-Language": "ru,en;q=0.9"
                                }) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    description = re.sub(r'<[^>]+>', '', data.get("description", ""))
                    skills = ", ".join([s.get("name", "") for s in data.get("key_skills", [])])
                    contacts = data.get("contacts") or {}
                    employer = data.get("employer") or {}
                    result["title"] = data.get("name", "") or ""
                    result["company"] = employer.get("name", "") or ""
                    result["description"] = description
                    result["skills"] = skills
                    result["contact_name"] = contacts.get("name", "") or ""
                    result["contact_email"] = contacts.get("email", "") or ""
                    contact_phones = contacts.get("phones", [])
                    if contact_phones:
                        ph = contact_phones[0]
                        result["contact_phone"] = f"+{ph.get('country', '')}{ph.get('city', '')}{ph.get('number', '')}" if ph.get("number") else ph.get("formatted", "")
                    log.info(f"Level 1 (API): company='{result['company']}', title='{result['title']}'")
                    break
                elif resp.status == 429:
                    log.warning(f"Rate limit hit, waiting... (attempt {attempt+1})")
                    await asyncio.sleep(5)
                else:
                    log.warning(f"API returned {resp.status}")
                    break
        except Exception as e:
            log.warning(f"API attempt {attempt+1} failed: {e}")
            if attempt < 2:
                await asyncio.sleep(2)

    # === УРОВЕНЬ 2: Мобильная версия (проще парсится) ===
    if not result["company"] or not result["description"]:
        try:
            async with HTTP.get(f"https://m.hh.ru/vacancy/{vacancy_id}",
                                headers={
                                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                    "Accept-Language": "ru,en;q=0.9"
                                }) as resp:
                if resp.status == 200:
                    page = await resp.text()
                    title_match = re.search(r'<h1[^>]*class="[^"]*vacancy-title[^"]*"[^>]*>(.*?)</h1>', page, re.S)
                    if title_match:
                        result["title"] = html.unescape(title_match.group(1)).strip()
                    
                    company_match = re.search(r'<a[^>]*class="[^"]*employer-name[^"]*"[^>]*>(.*?)</a>', page, re.S)
                    if company_match:
                        result["company"] = html.unescape(company_match.group(1)).strip()
                    
                    desc_match = re.search(r'<div[^>]*class="[^"]*vacancy-description[^"]*"[^>]*>(.*?)</div>', page, re.S)
                    if desc_match:
                        result["description"] = re.sub(r'<[^>]+>', '', html.unescape(desc_match.group(1))).strip()
                    
                    log.info(f"Level 2 (mobile): company='{result['company']}', title='{result['title']}'")
        except Exception as e:
            log.warning(f"Mobile scrape failed: {e}")

    # === УРОВЕНЬ 3: Десктопная версия с улучшенным парсингом ===
    if not result["company"] or not result["description"]:
        try:
            async with HTTP.get(f"https://hh.ru/vacancy/{vacancy_id}",
                                headers={
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                                    "Accept-Language": "ru,en;q=0.9",
                                    "Referer": "https://hh.ru/search/vacancy"
                                }) as resp:
                if resp.status == 200:
                    page = await resp.text()
                    
                    # Ищем JSON-LD (структурированные данные)
                    jsonld_match = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S)
                    if jsonld_match:
                        try:
                            jsonld = json.loads(html.unescape(jsonld_match.group(1)))
                            if isinstance(jsonld, dict):
                                if not result["title"] and jsonld.get("title"):
                                    result["title"] = jsonld["title"]
                                if not result["company"] and jsonld.get("hiringOrganization", {}).get("name"):
                                    result["company"] = jsonld["hiringOrganization"]["name"]
                                if not result["description"] and jsonld.get("description"):
                                    result["description"] = re.sub(r'<[^>]+>', '', jsonld["description"])
                        except json.JSONDecodeError:
                            pass
                    
                    # Ищем в <script> с данными страницы
                    script_matches = re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', page, re.S)
                    for script_content in script_matches:
                        try:
                            page_data = json.loads(html.unescape(script_content))
                            vacancy_block = page_data.get("vacancy", {}) or page_data.get("vacancyView", {}) or {}
                            if not result["title"]:
                                result["title"] = vacancy_block.get("name", "") or vacancy_block.get("title", "") or ""
                            if not result["company"]:
                                employer = vacancy_block.get("company", {}) or vacancy_block.get("employer", {}) or {}
                                result["company"] = employer.get("name", "") or ""
                            if not result["description"]:
                                result["description"] = re.sub(r'<[^>]+>', '', vacancy_block.get("description", ""))
                            if result["company"] and result["description"]:
                                break
                        except json.JSONDecodeError:
                            continue
                    
                    # Fallback: ищем через regex в HTML
                    if not result["title"]:
                        title_match = re.search(r'<title>(.*?)(?:\s*[-–|]\s*.*)?</title>', page, re.S)
                        if title_match:
                            title_text = html.unescape(title_match.group(1)).strip()
                            if title_text and title_text != "Вакансия не найдена":
                                result["title"] = title_text
                    
                    if not result["company"]:
                        company_patterns = [
                            r'"employer":\s*\{"name":\s*"([^"]+)"',
                            r'"company":\s*\{"name":\s*"([^"]+)"',
                            r'class="employer-name"[^>]*>([^<]+)<',
                            r'работодатель[^>]*>([^<]+)<',
                        ]
                        for pattern in company_patterns:
                            match = re.search(pattern, page, re.S | re.I)
                            if match:
                                result["company"] = html.unescape(match.group(1)).strip()
                                break
                    
                    log.info(f"Level 3 (desktop): company='{result['company']}', title='{result['title']}'")
        except Exception as e:
            log.warning(f"Desktop scrape failed: {e}")

    # === УРОВЕНЬ 4: ИИ-анализ (если есть хоть какое-то описание) ===
    if not result["company"] and result["description"]:
        extract_prompt = (
            "Проанализируй описание вакансии и вытащи:\n"
            "1. Название компании-работодателя (например: Сбер, Яндекс, Альфа-Банк).\n"
            "2. Точное название должности.\n\n"
            f"Описание:\n{result['description'][:2000]}\n\n"
            "Выдай ТОЛЬКО JSON: {\"company\": \"...\", \"title\": \"...\"}"
        )
        try:
            ai_response = await asyncio.to_thread(ai_generate, extract_prompt)
            if ai_response:
                clean = ai_response.replace("```json", "").replace("```", "").strip()
                json_match = re.search(r'\{.*\}', clean, re.S)
                if json_match:
                    parsed = json.loads(json_match.group(0))
                    if not result["company"] and parsed.get("company"):
                        result["company"] = parsed["company"]
                    if not result["title"] and parsed.get("title"):
                        result["title"] = parsed["title"]
                    log.info(f"Level 4 (AI): company='{result['company']}', title='{result['title']}'")
        except Exception as e:
            log.warning(f"AI extraction failed: {e}")

    # === УРОВЕНЬ 5: Regex по первым словам описания ===
    if not result["company"] and result["description"]:
        match = re.match(r'^([А-ЯA-Z][а-яa-zA-Z\s\-\.\"]+?)\s+(ищет|приглашает|нанимает|разыскивает|требует)',
                         result["description"], re.IGNORECASE)
        if match:
            result["company"] = match.group(1).strip()
            log.info(f"Level 5 (regex): company='{result['company']}'")

    if not result["title"]:
        result["title"] = "Позиция"
    
    return result


async def get_vacancy_details(vacancy_id: str) -> str:
    data = await get_vacancy_full_details(vacancy_id)
    if not data:
        return ""
    return f"Требования и описание:\n{data['description']}\n\nКлючевые навыки: {data['skills']}"


async def live_search_recruiter(company: str, contact_name: str = "") -> list:
    results = []
    if not DDGS_AVAILABLE:
        return results
    search_queries = []
    if contact_name:
        search_queries.append(f"{contact_name} {company} рекрутер HR LinkedIn")
        search_queries.append(f"{contact_name} {company} TenChat")
    else:
        search_queries.append(f"IT рекрутер {company} LinkedIn site:linkedin.com")
        search_queries.append(f"HR менеджер {company} TenChat site:tenchat.ru")
    try:
        def sync_search():
            found = []
            with DDGS() as ddgs:
                for query in search_queries[:3]:
                    try:
                        for r in ddgs.text(query, max_results=3):
                            url = r.get("href", "") or r.get("link", "")
                            if url:
                                found.append({"url": url, "title": r.get("title", ""),
                                              "snippet": r.get("body", ""), "query": query})
                    except Exception as e:
                        log.warning(f"DDG query failed: {query}, {e}")
            return found
        results = await asyncio.to_thread(sync_search)
    except Exception as e:
        log.warning(f"Live search failed: {e}")
    return results


async def aggressive_recruiter_search(chat_id: int, company: str, position: str = "", contact_hint: str = "") -> dict:
    found_contacts = []
    search_log = []
    search_queries = []
    if contact_hint:
        search_queries.append(f"{contact_hint} {company} email контакты HR")
        search_queries.append(f"{contact_hint} {company} telegram")
    search_queries.extend([
        f"{company} отдел кадров контакты сайт",
        f"{company} пресс-служба контакты для резюме",
        f"{company} рекрутер {position} контакты",
        f"{company} HR manager email",
        f"{company} корпоративная почта формат шаблон",
    ])
    search_queries = search_queries[:5]
    if DDGS_AVAILABLE:
        def sync_aggressive_search():
            results = []
            with DDGS() as ddgs:
                for query in search_queries:
                    try:
                        for r in ddgs.text(query, max_results=3):
                            results.append({"url": r.get("href", "") or r.get("link", ""),
                                            "title": r.get("title", ""),
                                            "snippet": r.get("body", ""), "query": query})
                    except Exception as e:
                        log.warning(f"Aggressive search failed for {query}: {e}")
            return results
        try:
            raw_results = await asyncio.to_thread(sync_aggressive_search)
            search_log.append(f"✅ Найдено {len(raw_results)} результатов в поисковых системах")
            if raw_results:
                snippets_text = "\n".join([
                    f"URL: {r['url']}\nЗаголовок: {r['title']}\nТекст: {r['snippet']}"
                    for r in raw_results[:10]
                ])
                extract_prompt = (
                    f"Проанализируй результаты поиска и найди контакты компании '{company}'.\n"
                    f"Результаты:\n{snippets_text[:3000]}\n\n"
                    f"Формат:\nИМЯ: ...\nПОЧТА: ...\nТЕЛЕФОН: ...\nССЫЛКА: ...\nИСТОЧНИК: ...\n"
                    f"Если ничего не найдено — 'НЕ НАЙДЕНО'."
                )
                contacts_raw = await asyncio.to_thread(ai_generate, extract_prompt)
                if contacts_raw and "НЕ НАЙДЕНО" not in contacts_raw.upper():
                    lines = contacts_raw.split("\n")
                    current_contact = {}
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        if "ИМЯ:" in line:
                            if current_contact:
                                found_contacts.append(current_contact)
                            current_contact = {"name": line.replace("ИМЯ:", "").strip()}
                        elif "ПОЧТА:" in line:
                            current_contact["email"] = line.replace("ПОЧТА:", "").strip()
                        elif "ТЕЛЕФОН:" in line:
                            current_contact["phone"] = line.replace("ТЕЛЕФОН:", "").strip()
                        elif "ССЫЛКА:" in line:
                            current_contact["url"] = line.replace("ССЫЛКА:", "").strip()
                        elif "ИСТОЧНИК:" in line:
                            current_contact["source"] = line.replace("ИСТОЧНИК:", "").strip()
                    if current_contact:
                        found_contacts.append(current_contact)
                    search_log.append(f"✅ Извлечено {len(found_contacts)} контактов из результатов")
                else:
                    search_log.append("⚠️ Прямых контактов в результатах не найдено")
        except Exception as e:
            log.error(f"Aggressive search error: {e}")
            search_log.append(f"❌ Ошибка поиска: {str(e)[:100]}")
    email_templates = []
    if company and not found_contacts:
        email_prompt = (
            f"Для компании '{company}' сгенерируй 3-5 шаблонов корпоративных почтовых адресов. "
            f"Выдай ТОЛЬКО список, каждый с новой строки."
        )
        templates_raw = await asyncio.to_thread(ai_generate, email_prompt)
        if templates_raw:
            email_templates = [t.strip() for t in templates_raw.split("\n") if "@" in t and len(t.strip()) < 50][:5]
            if email_templates:
                search_log.append(f"✅ Сгенерировано {len(email_templates)} шаблонов корпоративных почт")
    return {
        "found": len(found_contacts) > 0, "contacts": found_contacts,
        "email_templates": email_templates, "search_log": "\n".join(search_log),
        "queries_used": search_queries
    }


# ---------------- Утилита парсинга ID вакансии ----------------
def extract_hh_vacancy_id(text: str) -> str:
    if not text:
        return None
    match = re.search(r'hh\.ru/vacanc(?:y|ies)/(\d+)', text)
    return match.group(1) if match else None


def is_vacancy_text(text: str) -> bool:
    if not text or len(text) < 300:
        return False
    if extract_hh_vacancy_id(text):
        return False
    if "http://" in text or "https://" in text:
        return False
    markers = ["обязанности", "требования", "условия", "ищет", "вакансия",
               "приглашает", "мы предлагаем", "мы ждём", "мы ждем",
               "должен иметь", "ваша миссия", "мы ищем", "оформление по тк", "соцпакет", "дмс"]
    text_lower = text.lower()
    match_count = sum(1 for m in markers if m in text_lower)
    return match_count >= 2


# ---------------- Разбор вакансии ----------------
async def analyze_hh_vacancy_deep(chat_id: int, user_id: int, user_input: str):
    if not spend_balance(user_id, cost=2):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов! (Требуется 2 запроса)")
        return

    await send_telegram(chat_id, "🔗 *Разбор вакансии:* Извлекаю ID и анализирую...")

    vacancy_id = extract_hh_vacancy_id(user_input)
    if not vacancy_id:
        await send_telegram(chat_id,
            "⚠️ Не смог найти ссылку на вакансию.\n\n"
            "💡 *Лайфхак:* Если ссылка не работает — просто скопируй полный текст вакансии из приложения hh и пришли сюда. Бот разберёт его через ИИ!")
        return

    vac_data = await get_vacancy_full_details(vacancy_id)
    if not vac_data:
        await send_telegram(chat_id, "⚠️ Не удалось получить данные вакансии.")
        return

    company = vac_data["company"] or ""
    title = vac_data["title"] or "Позиция"
    contact_name = vac_data["contact_name"]
    contact_email = vac_data["contact_email"]
    contact_phone = vac_data["contact_phone"]
    description = vac_data["description"]

    extracted_contact = ""
    if not contact_name and description:
        extract_prompt = (
            f"Проанализируй описание вакансии и найди имя контактного лица.\n"
            f"Если есть — выдай ТОЛЬКО имя. Если нет — напиши 'не найдено'.\n\n"
            f"Описание:\n{description[:3000]}"
        )
        extracted_contact = await asyncio.to_thread(ai_generate, extract_prompt)
        if extracted_contact and "не найдено" in extracted_contact.lower():
            extracted_contact = ""
        elif extracted_contact:
            extracted_contact = extracted_contact.strip().strip('"').strip("'")

    if not company or company.strip() == "":
        resume = get_active_resume(user_id) or "Резюме не указано"
        await send_telegram(chat_id,
            f"⚠️ *Не удалось определить компанию по ссылке.*\n"
            f"hh.ru иногда блокирует автоматический доступ.\n\n"
            f"💼 Позиция: `{title}`\n\n"
            f"💡 *Что можно сделать:*\n"
            f"1️⃣ Напиши название компании следующим сообщением (например: `Сбер`)\n"
            f"2️⃣ ИЛИ скопируй полный текст вакансии из приложения hh и пришли сюда — бот разберёт его через ИИ!")
        user_states[user_id] = "waiting_for_company_correction"
        user_search_cache[user_id] = {
            "pending_vacancy_id": vacancy_id,
            "vacancy_title": title, "vacancy_description": description, "resume": resume
        }
        return

    final_report = f"🏢 *Компания:* {company}\n💼 *Позиция:* {title}\n\n"
    final_report += "📇 *Контакты из вакансии:*\n"
    final_report += f"• Имя: {contact_name or extracted_contact or '❌ не указано'}\n"
    final_report += f"• Email: {contact_email or '❌ не указан'}\n"
    final_report += f"• Телефон: {contact_phone or '❌ не указан'}\n"

    aggressive_results = None
    if not contact_name and not contact_email and not contact_phone and not extracted_contact:
        await send_telegram(chat_id, "🔍 *Контакты не найдены!* Запускаю агрессивный поиск...")
        aggressive_results = await aggressive_recruiter_search(chat_id, company, title, "")
        final_report += f"\n🔎 *Результаты агрессивного поиска:*\n{aggressive_results['search_log']}\n"
        if aggressive_results["found"]:
            final_report += "\n✅ *Найденные контакты:*\n"
            for i, c in enumerate(aggressive_results["contacts"][:3], 1):
                final_report += f"{i}. *{c.get('name', 'Имя не указано')}*\n"
                if c.get("email"):
                    final_report += f"   📧 {c['email']}\n"
                if c.get("phone"):
                    final_report += f"   📱 {c['phone']}\n"
                if c.get("url"):
                    final_report += f"   🔗 {c['url']}\n"
                if c.get("source"):
                    final_report += f"   📄 Источник: {c['source']}\n"
                final_report += "\n"
        if aggressive_results.get("email_templates"):
            final_report += "\n📧 *Возможные шаблоны корпоративных почт:*\n"
            for template in aggressive_results["email_templates"]:
                final_report += f"• `{template}`\n"

    search_results = []
    if DDGS_AVAILABLE and (contact_name or extracted_contact):
        search_results = await live_search_recruiter(company, contact_name or extracted_contact)
        if search_results:
            final_report += f"\n🌐 *Найдено профилей через живой поиск:* {len(search_results)}\n"
            for i, r in enumerate(search_results[:3], 1):
                final_report += f"{i}. {r['title']}\n   `{r['url']}`\n"

    resume = get_active_resume(user_id) or "Резюме не указано"
    pitch_prompt = (
        f"Напиши короткий питч (4-5 строк) для рекрутера компании '{company}' на позицию '{title}'.\n"
        f"Имя рекрутера: {contact_name or extracted_contact or 'неизвестно'}\n"
        f"Резюме: {resume[:1500]}\n\n"
        f"Стиль: от равного к равному, цепляюще, с оцифрованными результатами. Выдай ТОЛЬКО текст."
    )
    pitch = await asyncio.to_thread(ai_generate, pitch_prompt)
    if pitch:
        final_report += f"\n📝 *Персональный питч для отправки:*\n\n{pitch}"

    encoded_company = urllib.parse.quote(company)
    encoded_name = urllib.parse.quote(contact_name or extracted_contact or "")
    inline_kb = []
    if encoded_name:
        inline_kb.append([{"text": "🔍 LinkedIn (по имени)",
                           "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_name}%22+%22{encoded_company}%22"}])
        inline_kb.append([{"text": "🔍 TenChat (по имени)",
                           "url": f"https://www.google.com/search?q=site:tenchat.ru+%22{encoded_name}%22+%22{encoded_company}%22"}])
    else:
        inline_kb.append([{"text": "🔍 Искать HR в LinkedIn",
                           "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_company}%22+HR+OR+recruiter"}])
        inline_kb.append([{"text": "🔍 Искать HR в TenChat",
                           "url": f"https://www.google.com/search?q=site:tenchat.ru+%22{encoded_company}%22+HR"}])
    inline_kb.append([{"text": "🔍 Поиск в Telegram",
                       "url": f"https://www.google.com/search?q=site:t.me+%22{encoded_company}%22+%23вакансия"}])
    inline_kb.append([{"text": "🌐 Карьерный сайт компании",
                       "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded_company}%22+контакты"}])
    if aggressive_results and aggressive_results["found"]:
        for c in aggressive_results["contacts"][:2]:
            if c.get("url"):
                inline_kb.append([{"text": f"👤 {c.get('name', 'Контакт')[:30]}", "url": c["url"]}])
    if search_results:
        for r in search_results[:2]:
            inline_kb.append([{"text": f"🔗 {r['title'][:35]}", "url": r["url"]}])

    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Разобрана: Контакт')",
                (user_id, vacancy_id, f"{title} ({company})"))
    conn.commit()
    final_report += "\n\n📌 _Вакансия добавлена в Трекер откликов._"
    await send_telegram(chat_id, final_report, {"inline_keyboard": inline_kb})


# ---------------- Разбор текста вакансии ----------------
async def analyze_vacancy_text(chat_id: int, user_id: int, vacancy_text: str):
    if not spend_balance(user_id, cost=2):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов! (Требуется 2 запроса)")
        return
    await send_telegram(chat_id, "📄 *Разбор текста вакансии:* Извлекаю компанию и должность через ИИ...")
    extract_prompt = (
        "Проанализируй текст вакансии и вытащи:\n"
        "1. Название компании.\n2. Название должности.\n3. Имя контактного лица (если есть).\n\n"
        f"Текст:\n{vacancy_text[:4000]}\n\n"
        "Формат JSON: {\"company\": \"...\", \"title\": \"...\", \"contact_name\": \"...\"}"
    )
    try:
        ai_response = await asyncio.to_thread(ai_generate, extract_prompt)
        if not ai_response:
            await send_telegram(chat_id, "⚠️ ИИ недоступен.")
            return
        clean = ai_response.replace("```json", "").replace("```", "").strip()
        json_match = re.search(r'\{.*\}', clean, re.S)
        if not json_match:
            await send_telegram(chat_id, "⚠️ Не удалось разобрать ответ ИИ.")
            return
        parsed = json.loads(json_match.group(0))
        company = parsed.get("company", "").strip()
        title = parsed.get("title", "Позиция").strip()
        contact_name = parsed.get("contact_name", "").strip()
    except Exception as e:
        log.error(f"Vacancy text parsing failed: {e}")
        await send_telegram(chat_id, "⚠️ Ошибка парсинга.")
        return

    if not company:
        await send_telegram(chat_id,
            f"⚠️ *Не удалось определить компанию из текста.*\n💼 Позиция: `{title}`\n\nНапиши название компании следующим сообщением.")
        user_states[user_id] = "waiting_for_company_correction"
        user_search_cache[user_id] = {
            "pending_vacancy_id": f"text_{int(datetime.datetime.now().timestamp())}",
            "vacancy_title": title, "vacancy_description": vacancy_text[:3000],
            "resume": get_active_resume(user_id) or "Резюме не указано"
        }
        return

    await send_telegram(chat_id, f"🏢 *Распознано:* `{company}` | `{title}`\n🔍 Запускаю агрессивный поиск...")
    final_report = f"🏢 *Компания:* {company}\n💼 *Позиция:* {title}\n\n"
    final_report += f"📇 *Контакты:* Имя: {contact_name or '❌ не указано'}\n"
    aggressive_results = await aggressive_recruiter_search(chat_id, company, title, contact_name)
    final_report += f"\n🔎 *Результаты поиска:*\n{aggressive_results['search_log']}\n"
    if aggressive_results["found"]:
        final_report += "\n✅ *Найденные контакты:*\n"
        for i, c in enumerate(aggressive_results["contacts"][:3], 1):
            final_report += f"{i}. *{c.get('name', 'Имя')}*\n"
            if c.get("email"): final_report += f"   📧 {c['email']}\n"
            if c.get("phone"): final_report += f"   📱 {c['phone']}\n"
            if c.get("url"): final_report += f"   🔗 {c['url']}\n"
            final_report += "\n"
    if aggressive_results.get("email_templates"):
        final_report += "\n📧 *Шаблоны почт:*\n"
        for t in aggressive_results["email_templates"]:
            final_report += f"• `{t}`\n"

    resume = get_active_resume(user_id) or "Резюме не указано"
    pitch_prompt = (
        f"Напиши короткий питч (4-5 строк) для рекрутера '{company}' на позицию '{title}'.\n"
        f"Имя: {contact_name or 'неизвестно'}\nРезюме: {resume[:1500]}\nСтиль: от равного к равному."
    )
    pitch = await asyncio.to_thread(ai_generate, pitch_prompt)
    if pitch:
        final_report += f"\n📝 *Питч:*\n\n{pitch}"

    encoded_company = urllib.parse.quote(company)
    encoded_name = urllib.parse.quote(contact_name or "")
    inline_kb = []
    if encoded_name:
        inline_kb.append([{"text": "🔍 LinkedIn (по имени)",
                           "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_name}%22+%22{encoded_company}%22"}])
    else:
        inline_kb.append([{"text": "🔍 Искать HR в LinkedIn",
                           "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_company}%22+HR"}])
        inline_kb.append([{"text": "🔍 Искать HR в TenChat",
                           "url": f"https://www.google.com/search?q=site:tenchat.ru+%22{encoded_company}%22+HR"}])
    inline_kb.append([{"text": "🔍 Поиск в Telegram",
                       "url": f"https://www.google.com/search?q=site:t.me+%22{encoded_company}%22+%23вакансия"}])
    inline_kb.append([{"text": "🌐 Карьерный сайт компании",
                       "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded_company}%22+контакты"}])
    if aggressive_results["found"]:
        for c in aggressive_results["contacts"][:2]:
            if c.get("url"):
                inline_kb.append([{"text": f"👤 {c.get('name', '')[:30]}", "url": c["url"]}])

    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Разобрана: Контакт')",
                (user_id, f"text_{int(datetime.datetime.now().timestamp())}", f"{title} ({company})"))
    conn.commit()
    final_report += "\n\n📌 _Вакансия добавлена в Трекер._"
    await send_telegram(chat_id, final_report, {"inline_keyboard": inline_kb})


# ---------------- Разбор постов из Сетки ----------------
async def analyze_setka_post(chat_id: int, user_id: int, post_text: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ У вас закончились запросы!")
        return
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    await send_telegram(chat_id, "🌐 Читаю пост нанимателя...")
    prompt = (
        "Ты — карьерный стратег. Пользователь нашел пост о найме в «Сетке».\n"
        "1. Оцени соответствие резюме в %.\n2. Сильные стороны.\n3. Идеальное сообщение для лички.\n\n"
        f"--- ПОСТ ---\n{post_text[:3000]}\n\n--- РЕЗЮМЕ ---\n{resume[:5000]}"
    )
    result = await asyncio.to_thread(ai_generate, prompt)
    if not result:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    title_match = re.search(r'(ищем|вакансия|требуется|позиция)\s+([^\n\.,]+)', post_text, re.IGNORECASE)
    role_title = title_match.group(0)[:40] if title_match else "Вакансия из Сетки"
    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Сетка: Контакт')",
                (user_id, "setka_" + str(int(datetime.datetime.now().timestamp())), role_title))
    conn.commit()
    await send_telegram(chat_id, f"🌐 *Разбор предложения из Сетки:*\n\n{result}")


# ---------------- Вывод вакансий ----------------
async def send_vacancies_page(chat_id: int, user_id: int, page: int = 0):
    cached = user_search_cache.get(user_id)
    if not cached or not cached.get("items"):
        await send_telegram(chat_id, "💡 Список вакансий устарел. Нажмите «🔍 Поиск вакансий».")
        return
    items = cached["items"]
    page_size = 15
    start = page * page_size
    end = start + page_size
    chunk = items[start:end]
    if not chunk:
        await send_telegram(chat_id, "🏁 Больше нет новых вакансий!")
        return
    await send_telegram(chat_id, f"📄 Вакансии {start + 1}–{min(end, len(items))} из {len(items)}:")
    for v in chunk:
        vid = str(v["id"])
        name = v.get("name") or "Вакансия"
        comp = v.get("company") or "Компания"
        sal = v.get("salary") or ""
        match_score = v.get("match_score", 0)
        match_reason = v.get("match_reason", "релевантно профилю")
        temp_vacancies[vid] = {"title": name, "employer": comp}
        comp_lower = comp.lower()
        is_top = any(tc in comp_lower for tc in ["сбер", "мтс", "яндекс", "т-банк", "тинькофф", "втб", "альфа",
                                                  "билайн", "мегафон", "ростелеком", "первый бит", "газпром", "росатом"])
        badge = "⭐ *[ТОП-КОМПАНИЯ]*\n" if is_top else ""
        match_badge = f"🎯 Соответствие: {match_score}% ({match_reason})\n"
        sal_line = f"{sal}\n" if sal else ""
        markup = {"inline_keyboard": [
            [{"text": "👍 Откликнулся", "callback_data": f"like_{vid}"},
             {"text": "✍️ Сопроводительное", "callback_data": f"gen_{vid}"}],
            [{"text": "📊 Соответствие", "callback_data": f"match_{vid}"},
             {"text": "🎯 Питч для ЛПР", "callback_data": f"pitch_{vid}"}],
            [{"text": "🔗 Разобрать вакансию", "callback_data": f"deep_{vid}"},
             {"text": "🗑 Мусор", "callback_data": f"hide_{vid}"}]
        ]}
        await send_telegram(chat_id, f"{badge}🏢 *{comp}*\n💼 [{name}]({v.get('url')})\n{sal_line}{match_badge}", markup)
        await asyncio.sleep(0.2)
    if end < len(items):
        more_markup = {"inline_keyboard": [[{"text": "▶ Далее", "callback_data": f"page_{page + 1}"}]]}
        await send_telegram(chat_id, f"💡 Осталось {len(items) - end} вакансий.", more_markup)
    else:
        await send_telegram(chat_id, "🎉 Вы просмотрели всю выдачу!")


# ---------------- Поиск вакансий (с антибаном) ----------------
async def handle_search(chat_id: int, user_id: int, is_admin: bool):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ У вас закончились запросы!", get_keyboard(is_admin))
        return
    active_resume = get_active_resume(user_id)
    if not active_resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    await send_telegram(chat_id, "🔍 Анализирую резюме...")
    extract_prompt = (
        "Проанализируй резюме и напиши 3 подходящие должности для поиска. "
        "ТОЛЬКО названия через запятую.\n\n" + active_resume[:3000]
    )
    extracted = await asyncio.to_thread(ai_generate, extract_prompt)
    queries = [q.strip() for q in extracted.split(",") if q.strip()][:3] if extracted else ["Специалист", "Менеджер"]
    await send_telegram(chat_id, f"🎯 Запросы: *{', '.join(queries)}*\nСобираю вакансии...")
    all_items = []
    for idx, q in enumerate(queries):
        if idx > 0:
            await asyncio.sleep(2)
        res = await hh_scrape_search(q) or await hh_api_search(q)
        if res:
            all_items.extend(res)
        if len(all_items) >= 150:
            break
    if not all_items:
        await send_telegram(chat_id, "⚠️ Не удалось найти вакансии.", get_keyboard(is_admin))
        return
    stop_words = ["сборщик", "упаковщик", "кассир", "повар", "официант", "курьер", "продавец-консультант",
                  "сотрудник ресторана", "дворник", "грузчик"]
    top_companies = ["сбер", "мтс", "яндекс", "т-банк", "тинькофф", "втб", "альфа", "билайн", "мегафон",
                     "ростелеком", "первый бит", "газпром", "росатом"]
    unique_items = {}
    for v in all_items:
        vid = str(v["id"])
        if vid in unique_items:
            continue
        name_lower = (v.get("name") or "").lower()
        if any(sw in name_lower for sw in stop_words):
            continue
        if is_vacancy_hidden(user_id, vid):
            continue
        unique_items[vid] = v
    filtered_list = list(unique_items.values())
    filtered_list.sort(key=lambda x: 0 if any(tc in (x.get("company") or "").lower() for tc in top_companies) else 1)
    filtered_list = filtered_list[:45]
    scored_list = []
    for i in range(0, len(filtered_list), 15):
        batch = filtered_list[i:i + 15]
        vacancies_text = "\n".join([f"ID {v['id']}: {v.get('name')} в {v.get('company')}" for v in batch])
        quick_prompt = (
            "Оцени соответствие резюме для вакансий (0-100%).\n"
            f"Резюме:\n{active_resume[:2500]}\n\nВакансии:\n{vacancies_text}\n\n"
            "JSON: {\"ID\": {\"score\": 85, \"reason\": \"причина\"}}"
        )
        eval_res = await asyncio.to_thread(ai_generate, quick_prompt)
        parsed_batch = {}
        if eval_res:
            try:
                clean = eval_res.replace("```json", "").replace("```", "").strip()
                parsed_batch = json.loads(clean)
            except Exception as e:
                log.warning(f"Batch JSON error: {e}")
        for v in batch:
            vid = str(v["id"])
            v_data = parsed_batch.get(vid) or parsed_batch.get(int(vid)) or {}
            v["match_score"] = int(v_data.get("score", 60))
            v["match_reason"] = str(v_data.get("reason", "релевантный профиль"))
            scored_list.append(v)
        await asyncio.sleep(1)
    scored_list.sort(key=lambda x: (-x["match_score"],
                                     0 if any(tc in (x.get("company") or "").lower() for tc in top_companies) else 1))
    if not scored_list:
        await send_telegram(chat_id, "⚠️ Все вакансии отфильтрованы.", get_keyboard(is_admin))
        return
    user_search_cache[user_id] = {"items": scored_list}
    await send_telegram(chat_id, f"🔥 Нашел {len(scored_list)} вакансий:", get_keyboard(is_admin))
    await send_vacancies_page(chat_id, user_id, page=0)


# ---------------- Skill Gap ----------------
async def run_skill_gap_analysis(chat_id: int, user_id: int):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await send_telegram(chat_id, "📊 Провожу аудит навыков...")
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    current_date = datetime.date.today().strftime("%d.%m.%Y")
    prompt = (
        f"Дата: {current_date}. Проведи анализ навыков (Skill Gap) кандидата.\n"
        "1. Сильные компетенции 2. Зоны роста 3. Рекомендации.\n\n" + resume[:8000]
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    user_skillgap_cache[user_id] = analysis
    markup = {"inline_keyboard": [[{"text": "🚀 Исправить резюме", "callback_data": "fix_resume_from_gap"}]]}
    await send_telegram(chat_id, f"📊 *Анализ навыков:*\n\n{analysis}", markup)


async def run_fix_resume_by_gap(chat_id: int, user_id: int):
    if not check_free_action(user_id, "resume_fix", max_free=1):
        await send_telegram(
            chat_id,
            "🔒 *Бесплатный лимит исчерпан!*\n\n"
            "Вы уже использовали 1 бесплатное исправление резюме.\n"
            "Для продолжения купите *Безлимит* в меню «💎 Оплата и Баланс».\n\n"
            "💎 *Что вы получите в премиум версии:*\n"
            "• Безлимитные исправления резюме\n"
            "• 🎓 Доступ к 3 курсам по трудоустройству\n"
            "• 📝 10 шаблонов сопроводительных писем\n"
            "• 📊 Расширенная аналитика откликов\n"
            "• 🎯 Персональный план поиска работы на неделю"
        )
        return

    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов для переработки резюме!")
        return

    resume = get_active_resume(user_id)
    gap = user_skillgap_cache.get(user_id, "Усилить бизнес-метрики")
    await send_telegram(chat_id, "⚙️ Переписываю резюме...")
    prompt = (
        "Перепиши резюме по рекомендациям. Структура: ФИО, Контакты, Summary, Навыки, Опыт, Образование.\n"
        f"Рекомендации:\n{gap[:3000]}\n\nРезюме:\n{resume[:6000]}"
    )
    improved = await asyncio.to_thread(ai_generate, prompt)
    if not improved:
        await send_telegram(chat_id, "⚠️ Ошибка ИИ.")
        return
    try:
        doc = Document()
        for p in improved.split("\n"):
            clean_p = re.sub(r'[*#]', '', p).strip()
            if clean_p:
                doc.add_paragraph(clean_p)
        stream = io.BytesIO()
        doc.save(stream)
        add_resume(user_id, "HH_Optimized_Resume.docx", improved)
        await send_document_bytes(chat_id, stream.getvalue(), "HH_Optimized_Resume.docx",
                                  "💎 Ваше улучшенное резюме готово!")
    except Exception as e:
        log.error("DOCX error: %s", e)
        await send_telegram(chat_id, "⚠️ Ошибка файла.")


# ---------------- Сопроводительные и питчи ----------------
async def run_ai_generation(chat_id: int, user_id: int, vac_info: dict):
    await send_telegram(chat_id, f"✍️ Готовлю сопроводительное для *{vac_info.get('employer', 'компании')}*...")
    resume = get_active_resume(user_id) or "Опыт не указан."
    letter = await asyncio.to_thread(ai_generate,
        f"Напиши сопроводительное письмо на позицию '{vac_info.get('title', '')}' "
        f"в '{vac_info.get('employer', '')}'.\nРезюме:\n{resume}")
    if not letter:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📝 *Сопроводительное:*\n\n{letter}")


async def osint_search_manager(chat_id: int, user_id: int, target_info: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await send_telegram(chat_id, f"🕵️ *OSINT-поиск:* Анализирую {target_info}...")
    resume = get_active_resume(user_id) or "Резюме не указано"
    prompt = (
        f"Ты — эксперт по executive search. Цель: {target_info}\nРезюме: {resume[:2000]}\n\n"
        "1. Кто принимает решение о найме.\n2. 3 Google Dorks для LinkedIn/TenChat/TG.\n"
        "3. Короткое Cold DM сообщение.\n4. Лайфхаки поиска контактов."
    )
    result = await asyncio.to_thread(ai_generate, prompt)
    if not result:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    company_name = target_info.split(",")[0].strip().split()[0] if target_info else "Company"
    encoded_company = urllib.parse.quote(company_name)
    live_results = []
    if DDGS_AVAILABLE:
        live_results = await live_search_recruiter(company_name)
    links_kb = [
        [{"text": "🔍 HR в LinkedIn", "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_company}%22+HR"}],
        [{"text": "🔍 Посты в Telegram", "url": f"https://www.google.com/search?q=site:t.me+%22{encoded_company}%22+%23вакансия"}],
        [{"text": "🌐 Карьерный сайт", "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded_company}%22+вакансии"}]
    ]
    for r in live_results[:3]:
        links_kb.append([{"text": f"🔗 {r['title'][:35]}", "url": r["url"]}])
    live_section = ""
    if live_results:
        live_section = f"\n\n🌐 *Найдено профилей:* {len(live_results)}\n"
        for i, r in enumerate(live_results[:3], 1):
            live_section += f"{i}. {r['title']}\n   `{r['url']}`\n"
    await send_telegram(chat_id, f"🕵️ *Стратегия выхода на ЛПР:*\n\n{result}{live_section}", {"inline_keyboard": links_kb})


async def generate_pitch_from_menu(chat_id: int, user_id: int, target_info: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await send_telegram(chat_id, f"📝 Генерирую питч для {target_info}...")
    resume = get_active_resume(user_id) or "Резюме не указано"
    prompt = (
        f"Напиши короткий питч (4-5 строк) для HR-а. Цель: {target_info}\nРезюме: {resume[:2000]}\n"
        f"Стиль: от равного к равному, без канцеляризмов. ТОЛЬКО текст."
    )
    pitch = await asyncio.to_thread(ai_generate, prompt)
    if not pitch:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"🎯 *Питч для ЛПР:*\n\n{pitch}")


async def run_pitch_generation(chat_id: int, user_id: int, vac_info: dict):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await send_telegram(chat_id, f"🎯 Готовлю питч для *{vac_info.get('employer', 'компании')}*...")
    resume = get_active_resume(user_id) or "Опыт не указан."
    pitch = await asyncio.to_thread(ai_generate,
        f"Короткий питч (4-5 строк) для рекрутера '{vac_info.get('employer', '')}' на '{vac_info.get('title', '')}'.\n"
        f"Резюме: {resume[:2000]}\nСтиль: от равного к равному. ТОЛЬКО текст.")
    if not pitch:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"🎯 *Питч для ЛПР:*\n\n{pitch}")


async def run_vacancy_match(chat_id: int, user_id: int, vac_info: dict):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await send_telegram(chat_id, f"📊 Анализирую соответствие...")
    resume = get_active_resume(user_id) or "Резюме не найдено."
    prompt = (
        f"Оцени соответствие резюме вакансии '{vac_info.get('title', '')}' в '{vac_info.get('employer', '')}'.\n"
        f"% соответствия, сильные стороны, пробелы.\nРезюме:\n{resume}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📊 *Анализ соответствия:*\n\n{analysis}")


async def run_resume_adaptation(chat_id: int, user_id: int, resume_id: int, vacancy_text: str):
    if not check_free_action(user_id, "resume_adapt", max_free=1):
        await send_telegram(
            chat_id,
            "🔒 *Бесплатный лимит исчерпан!*\n\n"
            "Вы уже использовали 1 бесплатную адаптацию резюме.\n"
            "Для продолжения купите *Безлимит* в меню «💎 Оплата и Баланс»."
        )
        return

    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!", get_keyboard(ADMIN_ID != 0 and user_id == ADMIN_ID))
        return

    await send_telegram(chat_id, "🛠 Адаптирую резюме...")
    resume_text = get_resume_by_id(user_id, resume_id) or get_active_resume(user_id)
    if not resume_text:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    match = re.search(r'hh\.ru/vacancy/(\d+)', vacancy_text)
    if match:
        fetched_text = await get_vacancy_details(match.group(1))
        if fetched_text:
            vacancy_text = fetched_text
    current_date = datetime.date.today().strftime("%d.%m.%Y")
    prompt = (
        f"Дата: {current_date}. Перепиши резюме под вакансию. ТОЛЬКО текст, начиная с ФИО.\n"
        f"Вакансия:\n{vacancy_text[:3000]}\n\nРезюме:\n{resume_text[:6000]}"
    )
    adapted = await asyncio.to_thread(ai_generate, prompt)
    letter_prompt = (
        f"Короткое сопроводительное (до 1000 знаков).\n"
        f"Вакансия:\n{vacancy_text[:2000]}\n\nРезюме:\n{resume_text[:4000]}"
    )
    cover_letter = await asyncio.to_thread(ai_generate, letter_prompt)
    if not adapted:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    if "---" in adapted:
        adapted = adapted.split("---")[-1].strip()
    if cover_letter:
        await send_telegram(chat_id, f"📝 *Сопроводительное:*\n\n{cover_letter}")
    try:
        doc = Document()
        for p in adapted.split("\n"):
            clean_p = re.sub(r'[*#]', '', p).strip()
            if clean_p:
                doc.add_paragraph(clean_p)
        stream = io.BytesIO()
        doc.save(stream)
        await send_document_bytes(chat_id, stream.getvalue(), "Adapted_Resume.docx", "📄 Адаптированное резюме готово!")
    except Exception as e:
        log.error("DOCX error: %s", e)
        await send_telegram(chat_id, "⚠️ Ошибка файла.")


async def run_resume_audit(chat_id: int, user_id: int):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await send_telegram(chat_id, "📋 Провожу аудит резюме...")
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    audit = await asyncio.to_thread(ai_generate, f"Глубокий аудит резюме:\n{resume[:8000]}")
    rewrite = await asyncio.to_thread(ai_generate, f"Перепиши для позиций выше:\n{resume[:8000]}")
    if audit and rewrite:
        await send_telegram(chat_id, f"📋 *Аудит резюме:*\n\n{audit}")
        doc = Document()
        for p in rewrite.split("\n"):
            clean_p = re.sub(r'[*#]', '', p).strip()
            if clean_p:
                doc.add_paragraph(clean_p)
        stream = io.BytesIO()
        doc.save(stream)
        await send_document_bytes(chat_id, stream.getvalue(), "Resume_Pro.docx", "✅ Оптимизированное резюме готово!")
    else:
        await send_telegram(chat_id, "⚠️ Ошибка ИИ.")


# ============================================================
# 🎓 КУРСЫ, ШАБЛОНЫ, АНАЛИТИКА, ПЛАН ПОИСКА
# ============================================================

async def show_courses(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(
            chat_id,
            "🔒 *Курсы доступны только премиум-пользователям!*\n\n"
            "💎 *Купите Безлимит за 500 ⭐ и получите:*\n"
            "• 🎓 3 курса по 5 уроков каждый:\n"
            "   - «Резюме за 1 час»\n"
            "   - «Собеседование без стресса»\n"
            "   - «Переговоры о зарплате»\n"
            "• 📝 10 шаблонов сопроводительных писем\n"
            "• 📊 Расширенная аналитика откликов\n"
            "• 🎯 Персональный план поиска на неделю"
        )
        return
    
    courses_msg = "🎓 *Курсы для премиум-пользователей*\n\n"
    for course_id, course in COURSES.items():
        courses_msg += f"*{course['title']}*\n{course['description']}\n\n"
    
    courses_msg += "Выберите курс:"
    
    kb = {
        "inline_keyboard": [
            [{"text": "🎓 Резюме за 1 час", "callback_data": "course_resume"}],
            [{"text": "🎤 Собеседование без стресса", "callback_data": "course_interview"}],
            [{"text": "💰 Переговоры о зарплате", "callback_data": "course_salary"}]
        ]
    }
    await send_telegram(chat_id, courses_msg, kb)


async def show_course_lessons(chat_id: int, user_id: int, course_id: str):
    if course_id not in COURSES:
        await send_telegram(chat_id, "⚠️ Курс не найден.")
        return
    
    course = COURSES[course_id]
    lessons_msg = f"{course['title']}\n\n"
    lessons_msg += f"{course['description']}\n\n"
    lessons_msg += "📚 *Уроки:*\n"
    
    inline_kb = []
    for i, lesson in enumerate(course["lessons"], 1):
        lessons_msg += f"{i}. {lesson['title']}\n"
        inline_kb.append([{"text": lesson['title'][:35], "callback_data": f"lesson_{course_id}_{i}"}])
    
    await send_telegram(chat_id, lessons_msg, {"inline_keyboard": inline_kb})


async def show_lesson(chat_id: int, user_id: int, course_id: str, lesson_num: int):
    if course_id not in COURSES:
        await send_telegram(chat_id, "⚠️ Курс не найден.")
        return
    
    course = COURSES[course_id]
    if lesson_num < 1 or lesson_num > len(course["lessons"]):
        await send_telegram(chat_id, "⚠️ Урок не найден.")
        return
    
    lesson = course["lessons"][lesson_num - 1]
    
    nav_kb = []
    if lesson_num > 1:
        nav_kb.append({"text": f"← Урок {lesson_num - 1}", "callback_data": f"lesson_{course_id}_{lesson_num - 1}"})
    if lesson_num < len(course["lessons"]):
        nav_kb.append({"text": f"Урок {lesson_num + 1} →", "callback_data": f"lesson_{course_id}_{lesson_num + 1}"})
    
    markup = {"inline_keyboard": [nav_kb] if nav_kb else []}
    await send_telegram(chat_id, lesson["content"], markup)


async def show_cover_letter_templates(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(
            chat_id,
            "🔒 *Шаблоны доступны только премиум-пользователям!*\n\n"
            "💎 Купите Безлимит за 500 ⭐ и получите доступ к 10 шаблонам."
        )
        return
    
    templates_msg = "📝 *Шаблоны сопроводительных писем*\n\n"
    templates_msg += "Выберите шаблон:\n"
    
    inline_kb = []
    for i, template in enumerate(COVER_LETTER_TEMPLATES, 1):
        templates_msg += f"{i}. {template['name']}\n"
        inline_kb.append([{"text": template['name'][:35], "callback_data": f"template_{i}"}])
    
    await send_telegram(chat_id, templates_msg, {"inline_keyboard": inline_kb})


async def show_template(chat_id: int, user_id: int, template_num: int):
    if template_num < 1 or template_num > len(COVER_LETTER_TEMPLATES):
        await send_telegram(chat_id, "⚠️ Шаблон не найден.")
        return
    
    template = COVER_LETTER_TEMPLATES[template_num - 1]
    await send_telegram(chat_id, f"{template['name']}\n\n{template['content']}")


async def show_analytics(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(
            chat_id,
            "🔒 *Аналитика доступна только премиум-пользователям!*\n\n"
            "💎 Купите Безлимит за 500 ⭐ и получите детальную статистику."
        )
        return
    
    cur.execute("SELECT COUNT(*) FROM liked_vacancies WHERE user_id=?", (user_id,))
    total_vacancies = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM liked_vacancies WHERE user_id=? AND status LIKE '%Контакт%'", (user_id,))
    contacted = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM resumes WHERE user_id=?", (user_id,))
    total_resumes = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM free_actions WHERE user_id=?", (user_id,))
    total_actions = cur.fetchone()[0]
    
    conversion = (contacted / total_vacancies * 100) if total_vacancies > 0 else 0
    
    analytics_msg = (
        "📊 *Расширенная аналитика*\n\n"
        f"📋 *Всего разобрано вакансий:* {total_vacancies}\n"
        f"📧 *Установлено контактов:* {contacted}\n"
        f"📈 *Конверсия в контакт:* {conversion:.1f}%\n"
        f"📁 *Загружено резюме:* {total_resumes}\n"
        f"🎯 *Выполнено действий:* {total_actions}\n\n"
        f"💡 *Рекомендации:*\n"
    )
    
    if conversion < 10:
        analytics_msg += "• Низкая конверсия. Попробуйте улучшать резюме через «Аудит».\n"
    elif conversion < 30:
        analytics_msg += "• Средняя конверсия. Продолжайте в том же духе!\n"
    else:
        analytics_msg += "• Отличная конверсия! Вы молодец!\n"
    
    if total_resumes < 2:
        analytics_msg += "• Рекомендуем загрузить несколько версий резюме под разные позиции.\n"
    
    await send_telegram(chat_id, analytics_msg)


async def generate_job_search_plan(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(
            chat_id,
            "🔒 *План поиска доступен только премиум-пользователям!*\n\n"
            "💎 Купите Безлимит за 500 ⭐ и получите персональный план."
        )
        return
    
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    
    await send_telegram(chat_id, "🎯 Генерирую персональный план поиска работы на неделю...")
    
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    
    prompt = (
        f"Ты — карьерный стратег. Составь персональный план поиска работы на 7 дней для кандидата.\n"
        f"Резюме:\n{resume[:3000]}\n\n"
        "План должен включать:\n"
        "1. Конкретные действия на каждый день (понедельник-воскресенье)\n"
        "2. Сколько вакансий разбирать в день\n"
        "3. Кого искать и как выходить на ЛПР\n"
        "4. Какие документы готовить\n"
        "5. Когда отправлять отклики и фоллоу-апы\n"
        "6. Метрики успеха на неделю"
    )
    
    plan = await asyncio.to_thread(ai_generate, prompt)
    if not plan:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    
    await send_telegram(chat_id, f"🎯 *Персональный план поиска работы на неделю:*\n\n{plan}")


# ---------------- Обработка документов ----------------
async def handle_document(chat_id: int, user_id: int, document: dict, is_admin: bool):
    file_id = document["file_id"]
    file_name = document.get("file_name", "resume.pdf")
    try:
        async with HTTP.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}) as resp:
            file_info = await resp.json()
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            await send_telegram(chat_id, "⚠️ Не смог скачать файл.", get_keyboard(is_admin))
            return
        async with HTTP.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as f_resp:
            content = await f_resp.read()
    except Exception as e:
        log.error("download failed: %s", e)
        await send_telegram(chat_id, "⚠️ Ошибка скачивания.", get_keyboard(is_admin))
        return
    path = f"tmp_{user_id}_{file_name}"
    with open(path, "wb") as f:
        f.write(content)
    text_content = await asyncio.to_thread(extract_text, path, file_name)
    if os.path.exists(path):
        os.remove(path)
    if not text_content or not text_content.strip():
        await send_telegram(chat_id, "⚠️ Не удалось извлечь текст.", get_keyboard(is_admin))
        return
    add_resume(user_id, file_name, text_content)
    success_text = (
        f"✅ *Резюме «{file_name}» загружено!*\n\n"
        "💡 *Что можно сделать прямо сейчас:*\n"
        "1️⃣ *🔗 Разобрать вакансию* — кинь ссылку ИЛИ текст вакансии из мобильного приложения hh.\n"
        "2️⃣ *🕵️ Найти ЛПР* — прямой выход на нанимающего менеджера.\n"
        "3️⃣ *📊 Анализ навыков* — выявит пробелы и исправит резюме.\n"
        "4️⃣ *🎓 Курсы* — доступ к 3 курсам по трудоустройству (премиум).\n"
        "5️⃣ *⏰ Продлить доступ* — пополнить баланс или купить безлимит.\n\n"
        "🚀 *Лайфхак:* Просто кинь ссылку ИЛИ текст вакансии в чат — бот сам распознает!\n"
        "⚠️ *Если ссылка не работает* — скопируй полный текст вакансии из приложения и пришли его сюда."
    )
    await send_telegram(chat_id, success_text, get_keyboard(is_admin))


async def activate_resume(chat_id: int, user_id: int, rid: str):
    try:
        rid = int(rid)
    except ValueError:
        return
    cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (user_id,))
    cur.execute("UPDATE resumes SET active=1 WHERE id=? AND user_id=?", (rid, user_id))
    conn.commit()
    await send_telegram(chat_id, "✅ Резюме активировано.", get_keyboard(ADMIN_ID != 0 and user_id == ADMIN_ID))


# ---------------- Тренажер собеседований ----------------
async def start_interview_simulator(chat_id: int, user_id: int):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    await send_telegram(chat_id, "🎤 Начинаю тренировку. Будет 3 вопроса.")
    prompt = f"Задай первый каверзный вопрос на собеседовании по резюме:\n{resume[:5000]}"
    first_q = await asyncio.to_thread(ai_generate, prompt)
    if not first_q:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    interview_sessions[user_id] = {"question_count": 1, "history": []}
    user_states[user_id] = "interview_active"
    await send_telegram(chat_id, f"🎙 *Вопрос 1 из 3:*\n\n{first_q}")


async def handle_interview_answer(chat_id: int, user_id: int, answer_text: str):
    session = interview_sessions.get(user_id)
    if not session:
        user_states.pop(user_id, None)
        await send_telegram(chat_id, "⚠️ Сессия завершена. Запустите новую.")
        return
    q_count = session["question_count"]
    await send_telegram(chat_id, "🔎 Анализирую ответ...")
    prompt = (
        f"Ответ кандидата: {answer_text}\n"
        f"Дай фидбек и задай следующий вопрос (номер {q_count + 1} из 3). "
        f"Если это был 3-й вопрос — подведи итог."
    )
    feedback = await asyncio.to_thread(ai_generate, prompt)
    if q_count >= 3:
        user_states.pop(user_id, None)
        interview_sessions.pop(user_id, None)
        await send_telegram(chat_id, f"🏁 *Итоги тренировки:*\n\n{feedback}")
    else:
        session["question_count"] += 1
        await send_telegram(chat_id, f"💡 *Разбор и следующий вопрос:*\n\n{feedback}")


# ---------------- Оплата ----------------
async def send_stars_invoice(chat_id, amount_stars: int, title: str, payload: str):
    await HTTP.post(f"{TELEGRAM_API}/sendInvoice", json={
        "chat_id": chat_id, "title": title,
        "description": "Пополнение баланса карьерного агента",
        "payload": payload, "currency": "XTR",
        "prices": [{"label": "Stars", "amount": amount_stars}]
    })


# ---------------- Обработка сообщений ----------------
async def process_message(msg: dict):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    user_id = user.get("id", chat_id)
    username = user.get("username", "")
    text = (msg.get("text") or "").strip()
    document = msg.get("document")
    photo = msg.get("photo")

    referrer_id = None
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1:
            try:
                referrer_id = int(parts[1])
            except ValueError:
                pass
    register_user(user_id, username, referrer_id)
    is_admin = (ADMIN_ID != 0 and user_id == ADMIN_ID)

    if is_admin and text.startswith("/reply"):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3:
            try:
                target_uid = int(parts[1])
                reply_text = parts[2]
                await send_telegram(target_uid, f"💬 *От администратора:*\n\n{reply_text}")
                await send_telegram(chat_id, f"✅ Ответ отправлен `{target_uid}`.")
            except ValueError:
                await send_telegram(chat_id, "⚠️ Ошибка ID.")
        return

    if user_states.get(user_id) == "interview_active":
        bg(handle_interview_answer(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_setka_post":
        user_states.pop(user_id, None)
        bg(analyze_setka_post(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_hh_link":
        user_states.pop(user_id, None)
        if extract_hh_vacancy_id(text):
            bg(analyze_hh_vacancy_deep(chat_id, user_id, text))
        elif is_vacancy_text(text) or len(text) > 200:
            bg(analyze_vacancy_text(chat_id, user_id, text))
        else:
            await send_telegram(chat_id, "⚠️ Пришли ссылку на вакансию или полный текст вакансии.")
        return

    if user_states.get(user_id) == "waiting_for_company_correction":
        user_states.pop(user_id, None)
        new_company = text.strip()
        cached = user_search_cache.get(user_id, {})
        title = cached.get("vacancy_title", "Позиция")
        resume = cached.get("resume") or get_active_resume(user_id) or "Резюме не указано"
        await send_telegram(chat_id, f"🏢 *Компания:* `{new_company}`\n🔍 Запускаю агрессивный поиск...")
        aggressive_results = await aggressive_recruiter_search(chat_id, new_company, title, "")
        final_report = f"🏢 *Компания:* {new_company}\n💼 *Позиция:* {title}\n"
        final_report += f"\n🔎 *Результаты поиска:*\n{aggressive_results['search_log']}\n"
        if aggressive_results["found"]:
            final_report += "\n✅ *Найденные контакты:*\n"
            for i, c in enumerate(aggressive_results["contacts"][:3], 1):
                final_report += f"{i}. *{c.get('name', 'Имя')}*\n"
                if c.get("email"): final_report += f"   📧 {c['email']}\n"
                if c.get("phone"): final_report += f"   📱 {c['phone']}\n"
                if c.get("url"): final_report += f"   🔗 {c['url']}\n"
                final_report += "\n"
        if aggressive_results.get("email_templates"):
            final_report += "\n📧 *Шаблоны почт:*\n"
            for t in aggressive_results["email_templates"]:
                final_report += f"• `{t}`\n"
        pitch = await asyncio.to_thread(ai_generate,
            f"Питч (4-5 строк) для '{new_company}' на '{title}'.\nРезюме: {resume[:1500]}\nСтиль: от равного к равному.")
        if pitch:
            final_report += f"\n📝 *Питч:*\n\n{pitch}"
        encoded = urllib.parse.quote(new_company)
        inline_kb = [
            [{"text": "🔍 LinkedIn", "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded}%22+HR"}],
            [{"text": "🔍 TenChat", "url": f"https://www.google.com/search?q=site:tenchat.ru+%22{encoded}%22+HR"}],
            [{"text": "🌐 Карьерный сайт", "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded}%22+контакты"}]
        ]
        cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Разобрана')",
                    (user_id, f"manual_{int(datetime.datetime.now().timestamp())}", f"{title} ({new_company})"))
        conn.commit()
        await send_telegram(chat_id, final_report, {"inline_keyboard": inline_kb})
        return

    if user_states.get(user_id) == "waiting_for_osint_target":
        user_states.pop(user_id, None)
        
        if extract_hh_vacancy_id(text):
            if not get_active_resume(user_id):
                await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
                return
            bg(analyze_hh_vacancy_deep(chat_id, user_id, text))
            return
        
        if is_vacancy_text(text) or len(text) > 300:
            if not get_active_resume(user_id):
                await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
                return
            bg(analyze_vacancy_text(chat_id, user_id, text))
            return
        
        bg(osint_search_manager(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_pitch_target":
        user_states.pop(user_id, None)
        
        if extract_hh_vacancy_id(text):
            if not get_active_resume(user_id):
                await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
                return
            bg(analyze_hh_vacancy_deep(chat_id, user_id, text))
            return
        
        if is_vacancy_text(text) or len(text) > 300:
            if not get_active_resume(user_id):
                await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
                return
            bg(analyze_vacancy_text(chat_id, user_id, text))
            return
        
        bg(generate_pitch_from_menu(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_feedback":
        user_states.pop(user_id, None)
        cur.execute("INSERT INTO feedback (user_id, username, message) VALUES (?, ?, ?)", (user_id, username, text))
        conn.commit()
        await send_telegram(chat_id, "✅ Сообщение отправлено администратору.")
        await send_telegram(ADMIN_ID,
            f"📩 *Отзыв!*\nОт: @{username or 'нет'} (ID: `{user_id}`)\n\n{text}\n\n/reply {user_id} Текст")
        return

    if user_states.get(user_id) == "waiting_for_repost":
        user_states.pop(user_id, None)
        urls = re.findall(r'(https?://[^\s]+)', text) if text else []
        if urls:
            url = urls[0].lower()
            network = "Other"
            if "vk.com" in url: network = "VK"
            elif "linkedin" in url: network = "LinkedIn"
            elif "tenchat" in url: network = "TenChat"
            elif "setka" in url or "hh.ru" in url: network = "Сетка"
            elif "t.me" in url: network = "Telegram"
            cur.execute("SELECT 1 FROM social_shares WHERE user_id=? AND network=?", (user_id, network))
            if cur.fetchone():
                await send_telegram(chat_id, f"⚠️ Вы уже получали бонус за {network}.")
            else:
                cur.execute("INSERT INTO social_shares (user_id, network) VALUES (?, ?)", (user_id, network))
                conn.commit()
                admin_add_balance(user_id, 20)
                await send_telegram(chat_id, f"🎉 +20 запросов за пост в {network}.")
            return
        await send_telegram(chat_id, "⚠️ Ссылка не найдена.")
        return

    if photo and user_states.get(user_id) == "waiting_for_receipt":
        user_states.pop(user_id, None)
        await HTTP.post(f"{TELEGRAM_API}/forwardMessage", json={
            "chat_id": ADMIN_ID, "from_chat_id": chat_id, "message_id": msg["message_id"]
        })
        await send_telegram(chat_id, "✅ Чек отправлен администратору.")
        admin_markup = {"inline_keyboard": [
            [{"text": "✅ +50 запросов", "callback_data": f"paycred_{user_id}_50"}],
            [{"text": "⭐ Безлимит 10 дней", "callback_data": f"payunl_{user_id}"}]
        ]}
        await send_telegram(ADMIN_ID, f"📸 *Чек на проверку!*\nОт: @{username or 'нет'} (ID: `{user_id}`)", admin_markup)
        return

    if document:
        await handle_document(chat_id, user_id, document, is_admin)
        return
    if not text:
        return

    if is_admin and text.startswith("/add_credits"):
        parts = text.split()
        if len(parts) == 3:
            try:
                target_id = int(parts[1])
                amount = int(parts[2])
                new_bal = admin_add_balance(target_id, amount)
                await send_telegram(chat_id, f"✅ Пользователю `{target_id}` добавлено {amount}. Баланс: `{new_bal}`")
            except ValueError:
                await send_telegram(chat_id, "⚠️ Формат: `/add_credits ID AMOUNT`")
        return

    if user_states.get(user_id) == "waiting_for_adaptation_vacancy":
        rid = user_adapt_target.get(user_id)
        user_states.pop(user_id, None)
        user_adapt_target.pop(user_id, None)
        if not spend_balance(user_id, cost=1):
            await send_telegram(chat_id, "⚠️ Недостаточно запросов!", get_keyboard(is_admin))
            return
        bg(run_resume_adaptation(chat_id, user_id, rid, text))
        return

    # Автоопределение ссылки на вакансию
    if extract_hh_vacancy_id(text):
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(analyze_hh_vacancy_deep(chat_id, user_id, text))
        return

    # Автоопределение текста вакансии
    if is_vacancy_text(text):
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(analyze_vacancy_text(chat_id, user_id, text))
        return

    if text.startswith("/start") or text == "🚀 Запустить бота":
        if is_admin:
            welcome_text = "👋 Привет, Антон! Админ-режим активирован.\nОтправь файл резюме."
        else:
            welcome_text = (
                "👋 Привет! Я — твой ИИ-карьерный агент (Версия 3.2).\n\n"
                "🔥 *Главная фишка:* Работает и со ссылками hh.ru, и с текстом вакансии из мобильного приложения.\n\n"
                "💡 *Быстрый старт:*\n"
                "1️⃣ Отправь файл резюме (PDF или DOCX).\n"
                "2️⃣ Кинь ссылку ИЛИ скопируй текст вакансии из приложения hh.\n\n"
                "⚠️ *Если ссылка не работает* — просто скопируй полный текст вакансии из приложения и пришли сюда. Бот разберёт его через ИИ!\n\n"
                "🎁 *Баланс:* `7 запросов` бесплатно!\n"
                "🎓 *Премиум:* Курсы, шаблоны, аналитика, план поиска."
            )
        await send_telegram(chat_id, welcome_text, get_keyboard(is_admin))

    elif text == "🌐 Вакансии из Сетки":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_setka_post"
        await send_telegram(chat_id, "🌐 *Вакансии из Сетки*\n\nСкопируйте текст поста о найме и отправьте сюда.")

    elif text == "🔗 Разобрать вакансию":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_hh_link"
        await send_telegram(chat_id,
            "🔗 *Разбор вакансии*\n\n"
            "Пришли мне в следующем сообщении **ОДНО из двух**:\n"
            "1️⃣ Ссылку на вакансию hh.ru (например: `https://hh.ru/vacancy/12345678`)\n"
            "2️⃣ Полный текст вакансии из мобильного приложения (просто скопируй и вставь)\n\n"
            "⚠️ *Важно:* Если ссылка не распознаётся — пришли полный текст вакансии. Бот разберёт его через ИИ!")

    elif text == "🕵️ Найти HR / ЛПР":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_osint_target"
        await send_telegram(chat_id, 
            "🕵️ *Прямой выход на ЛПР*\n\n"
            "Напиши в следующем сообщении:\n"
            "• `Компания, должность` (например: `Сбер, Product Manager`)\n"
            "• ИЛИ просто кинь ссылку на вакансию / текст вакансии\n\n"
            "Бот сам определит что делать!")

    elif text == "📝 Короткие Питчи":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_pitch_target"
        await send_telegram(chat_id, 
            "📝 *Генерация питча*\n\n"
            "Напиши в следующем сообщении:\n"
            "• `Компания, должность` (например: `Яндекс, Data Scientist`)\n"
            "• ИЛИ кинь ссылку/текст вакансии\n\n"
            "Бот сам определит формат!")

    elif text == "🎓 Курсы (Премиум)":
        bg(show_courses(chat_id, user_id))

    elif text == "📝 Шаблоны писем (Премиум)":
        bg(show_cover_letter_templates(chat_id, user_id))

    elif text == "📊 Аналитика (Премиум)":
        bg(show_analytics(chat_id, user_id))

    elif text == "🎯 План поиска (Премиум)":
        bg(generate_job_search_plan(chat_id, user_id))

    elif text in ("👥 Пригласить друга", "🎁 Бонусы (Репост & Друзья)"):
        bot_info = await HTTP.get(f"{TELEGRAM_API}/getMe")
        bot_data = await bot_info.json()
        bot_username = bot_data.get("result", {}).get("username", "bot")
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        bonus_text = (
            "🎁 *Программа лояльности*\n\n"
            "👥 *Пригласить друга (+7 запросов)*\n"
            f"Ссылка:\n`{ref_link}`\n\n"
            "📢 *Поделиться в соцсетях (+20 запросов)*\n"
            "Опубликуй пост о боте и пришли ссылку."
        )
        kb = {"inline_keyboard": [[{"text": "🔗 Отправить ссылку на репост", "callback_data": "send_repost_proof"}]]}
        await send_telegram(chat_id, bonus_text, kb)

    elif text == "💬 Обратная связь":
        user_states[user_id] = "waiting_for_feedback"
        await send_telegram(chat_id, "💬 Напишите отзыв следующим сообщением.")

    elif text == "📩 Сообщения от пользователей":
        if not is_admin:
            return
        cur.execute("SELECT id, user_id, username, message, created_at FROM feedback ORDER BY id DESC LIMIT 10")
        rows = cur.fetchall()
        if not rows:
            await send_telegram(chat_id, "📭 Сообщений нет.")
            return
        feedbacks_msg = "📩 *Последние сообщения:*\n\n"
        for r in rows:
            feedbacks_msg += f"🆔 `{r[1]}` (@{r[2] or 'нет'})\n💬 {r[3]}\n⏱ `{r[4]}`\n/reply {r[1]} Текст\n\n"
        await send_telegram(chat_id, feedbacks_msg)

    elif text == "💎 Оплата и Баланс":
        if is_admin:
            status_str = "📊 Баланс: `∞ Безлимит` (Администратор)"
        else:
            data = get_user_data(user_id)
            status_str = f"📊 Баланс: `{data['balance']} запросов`"
            if data["unlimited_until"]:
                status_str += f"\n⭐ Безлимит до: `{data['unlimited_until']}`"
        balance_text = (
            f"💎 *Оплата и Баланс*\n\n{status_str}\n\n"
            "💳 *Тарифы:*\n"
            "1️⃣ Пакет «50 запросов»: 100 ⭐ ИЛИ 200 руб.\n"
            "2️⃣ Безлимит на 10 дней: 500 ⭐ ИЛИ 500 руб.\n"
            "   (Включает: курсы, шаблоны, аналитику, план поиска)\n\n"
            "🏦 СБП: `2202208459089018`\n"
            "_После перевода отправьте скриншот чека._"
        )
        kb = {"inline_keyboard": [
            [{"text": "⭐ 50 запросов (100 Звезд)", "callback_data": "buy_pack_stars"}],
            [{"text": "⭐ Безлимит 10 дней (500 Звезд)", "callback_data": "buy_unl_stars"}],
            [{"text": "📄 Отправить чек", "callback_data": "send_receipt"}]
        ]}
        await send_telegram(chat_id, balance_text, kb)

    elif text == "⏰ Продлить доступ":
        if is_admin:
            await send_telegram(chat_id, "👑 У тебя уже безлимит (Админ-режим).")
            return
        data = get_user_data(user_id)
        status_str = f"📊 Баланс: `{data['balance']} запросов`"
        if data["unlimited_until"]:
            status_str += f"\n⭐ Безлимит до: `{data['unlimited_until']}`"
        extend_text = (
            f"⏰ *Продление доступа*\n\n{status_str}\n\n"
            "💳 *Выберите вариант продления:*\n\n"
            "⭐ **Оплата Telegram Stars** (мгновенно):\n"
            "• 50 запросов — 100 ⭐\n"
            "• Безлимит на 10 дней — 500 ⭐\n"
            "  (Включает: курсы, шаблоны, аналитику, план поиска)\n\n"
            "🏦 **Оплата по СБП** (после подтверждения):\n"
            "• 50 запросов — 200 руб.\n"
            "• Безлимит на 10 дней — 500 руб.\n\n"
            "🎁 **Бесплатные способы:**\n"
            "• Пригласить друга — +7 запросов\n"
            "• Репост в соцсетях — +20 запросов"
        )
        kb = {"inline_keyboard": [
            [{"text": "⭐ 50 запросов (100 Звезд)", "callback_data": "buy_pack_stars"}],
            [{"text": "⭐ Безлимит 10 дней (500 Звезд)", "callback_data": "buy_unl_stars"}],
            [{"text": "📄 Отправить чек СБП", "callback_data": "send_receipt"}]
        ]}
        await send_telegram(chat_id, extend_text, kb)

    elif text == "🛠 Адаптация резюме":
        rows = list_resumes(user_id)
        if not rows:
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        kb = {"inline_keyboard": [[{"text": f"📄 {r['name']}", "callback_data": f"adaptsel_{r['id']}"}] for r in rows]}
        await send_telegram(chat_id, "🛠 Выберите резюме:", kb)

    elif text == "📋 Аудит резюме":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(run_resume_audit(chat_id, user_id))

    elif text == "📊 Анализ навыков (Skill Gap)":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(run_skill_gap_analysis(chat_id, user_id))

    elif text == "🎤 Тренажер собеседований":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(start_interview_simulator(chat_id, user_id))

    elif text == "📌 Трекер откликов":
        cur.execute("SELECT vacancy_id, title, status FROM liked_vacancies WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,))
        rows = cur.fetchall()
        if not rows:
            await send_telegram(chat_id, "📌 Трекер пуст.")
        else:
            tracker_msg = "📌 *Ваш трекер откликов:*\n\n"
            for r in rows:
                v_url = f"https://hh.ru/vacancy/{r[0]}" if not (str(r[0]).startswith("setka_") or str(r[0]).startswith("text_") or str(r[0]).startswith("manual_")) else "#"
                tracker_msg += f"• [{r[1]}]({v_url})\nСтатус: `{r[2]}`\n\n"
            await send_telegram(chat_id, tracker_msg)

    elif text == "ℹ️ Помощь":
        help_text = (
            "ℹ️ *Справка (Версия 3.2):*\n\n"
            "🚀 *Прямой выход на ЛПР:*\n"
            "• 🔗 *Разобрать вакансию* — кинь ссылку или текст вакансии.\n"
            "• 🕵️ *Найти ЛПР* — OSINT-поиск контактов.\n"
            "• 📝 *Короткие Питчи* — цепляющие сообщения.\n\n"
            "⚠️ *Если ссылка не работает:* скопируй полный текст вакансии из приложения hh и пришли сюда. Бот разберёт его через ИИ!\n\n"
            "📋 *Основной функционал:*\n"
            "• 📊 *Skill Gap* — аудит + исправление резюме.\n"
            "• 🌐 *Вакансии из Сетки* — разбор постов.\n"
            "• 🔍 *Поиск вакансий* — подбор с Match Rate.\n"
            "• 🎤 *Тренажер* — тренировка интервью.\n\n"
            "🎓 *Премиум функции (безлимит):*\n"
            "• 🎓 *Курсы* — 3 курса по трудоустройству.\n"
            "• 📝 *Шаблоны* — 10 шаблонов сопроводительных.\n"
            "• 📊 *Аналитика* — детальная статистика.\n"
            "• 🎯 *План поиска* — персональный план на неделю."
        )
        await send_telegram(chat_id, help_text, get_keyboard(is_admin))

    elif text in ("👑 Админ-панель", "/admin"):
        if not is_admin:
            return
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM resumes")
        total_resumes = cur.fetchone()[0]
        await send_telegram(chat_id, f"👑 *Админ-панель*\n\n👥 Пользователей: `{total_users}`\n📁 Резюме: `{total_resumes}`")

    elif text == "📁 Мои резюме":
        rows = list_resumes(user_id)
        if not rows:
            await send_telegram(chat_id, "💡 Нет резюме. Отправьте файл.")
        else:
            kb = {"inline_keyboard": [[{"text": f"{'✅ Активное' if r['active'] else '📄'} {r['name']}",
                                        "callback_data": f"act_{r['id']}"}] for r in rows]}
            await send_telegram(chat_id, "📁 *Ваши резюме:*", kb)

    elif text == "📥 Загрузить резюме":
        await send_telegram(chat_id, "📄 Отправьте файл резюме (PDF или DOCX).")

    elif text == "🔍 Поиск вакансий":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        else:
            bg(handle_search(chat_id, user_id, is_admin))

    else:
        await send_telegram(chat_id, "ℹ️ Воспользуйтесь меню ниже.", get_keyboard(is_admin))


# ---------------- Вебхук ----------------
async def telegram_webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="OK")
    if is_duplicate(data.get("update_id")):
        return web.Response(text="OK")
    if "pre_checkout_query" in data:
        pcq = data["pre_checkout_query"]
        await HTTP.post(f"{TELEGRAM_API}/answerPreCheckoutQuery", json={"pre_checkout_query_id": pcq["id"], "ok": True})
        return web.Response(text="OK")
    if "message" in data:
        msg = data["message"]
        if msg.get("successful_payment"):
            uid = msg.get("from", {}).get("id") or msg["chat"]["id"]
            payload = msg["successful_payment"].get("invoice_payload", "")
            if "unl" in payload:
                admin_set_unlimited(uid, 10)
                await send_telegram(uid, "🎉 Безлимит активирован! Доступ к курсам, шаблонам, аналитике и плану поиска.")
            else:
                admin_add_balance(uid, 50)
                await send_telegram(uid, "🎉 Начислено 50 запросов.")
        else:
            bg(process_message(msg))
    if "callback_query" in data:
        cb = data["callback_query"]
        message = cb.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        user_id = cb.get("from", {}).get("id") or chat_id
        message_id = message.get("message_id")
        data_str = cb.get("data", "") or ""
        bg(answer_callback(cb.get("id", "")))
        if chat_id and user_id:
            if data_str == "fix_resume_from_gap":
                bg(run_fix_resume_by_gap(chat_id, user_id))
            elif data_str.startswith("page_"):
                bg(send_vacancies_page(chat_id, user_id, page=int(data_str.split("_")[1])))
            elif data_str == "buy_pack_stars":
                bg(send_stars_invoice(chat_id, 100, "Пакет 50 запросов", "credits_50"))
            elif data_str == "buy_unl_stars":
                bg(send_stars_invoice(chat_id, 500, "Безлимит на 10 дней", "unl_10d"))
            elif data_str == "send_receipt":
                user_states[user_id] = "waiting_for_receipt"
                await send_telegram(chat_id, "📸 Отправьте скриншот чека.")
            elif data_str == "send_repost_proof":
                user_states[user_id] = "waiting_for_repost"
                await send_telegram(chat_id, "🔗 Отправьте ссылку на пост.")
            elif data_str == "course_resume":
                bg(show_course_lessons(chat_id, user_id, "resume"))
            elif data_str == "course_interview":
                bg(show_course_lessons(chat_id, user_id, "interview"))
            elif data_str == "course_salary":
                bg(show_course_lessons(chat_id, user_id, "salary"))
            elif data_str.startswith("lesson_"):
                parts = data_str.split("_")
                if len(parts) == 3:
                    course_id = parts[1]
                    lesson_num = int(parts[2])
                    bg(show_lesson(chat_id, user_id, course_id, lesson_num))
            elif data_str.startswith("template_"):
                template_num = int(data_str.split("_")[1])
                bg(show_template(chat_id, user_id, template_num))
            elif data_str.startswith("paycred_"):
                parts = data_str.split("_")
                admin_add_balance(int(parts[1]), int(parts[2]))
                await send_telegram(int(parts[1]), f"✅ Начислено {parts[2]} запросов.")
                await http_edit_message_text(chat_id, message_id, "✅ Чек одобрен.")
            elif data_str.startswith("payunl_"):
                parts = data_str.split("_")
                admin_set_unlimited(int(parts[1]), 10)
                await send_telegram(int(parts[1]), "✅ Безлимит активирован!")
                await http_edit_message_text(chat_id, message_id, "✅ Чек одобрен.")
            elif data_str.startswith("like_"):
                vid = data_str[5:]
                vac = temp_vacancies.get(vid, {"title": "Позиция"})
                like_vacancy(user_id, vid, vac["title"])
                await send_telegram(chat_id, f"📌 Вакансия добавлена в Трекер!")
            elif data_str.startswith("gen_"):
                if not spend_balance(user_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
                    return web.Response(text="OK")
                bg(run_ai_generation(chat_id, user_id, dict(temp_vacancies.get(data_str[4:], {}))))
            elif data_str.startswith("match_"):
                if not spend_balance(user_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
                    return web.Response(text="OK")
                bg(run_vacancy_match(chat_id, user_id, dict(temp_vacancies.get(data_str[6:], {}))))
            elif data_str.startswith("pitch_"):
                if not spend_balance(user_id, cost=1):
                    await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
                    return web.Response(text="OK")
                bg(run_pitch_generation(chat_id, user_id, dict(temp_vacancies.get(data_str[6:], {}))))
            elif data_str.startswith("deep_"):
                vid = data_str[5:]
                bg(analyze_hh_vacancy_deep(chat_id, user_id, f"https://hh.ru/vacancy/{vid}"))
            elif data_str.startswith("act_"):
                bg(activate_resume(chat_id, user_id, data_str[4:]))
            elif data_str.startswith("adaptsel_"):
                user_adapt_target[user_id] = int(data_str[9:])
                user_states[user_id] = "waiting_for_adaptation_vacancy"
                bg(http_edit_message_text(chat_id, message_id, "✅ Резюме выбрано! Отправьте ссылку на вакансию."))
            elif data_str.startswith("hide_"):
                hide_vacancy(user_id, data_str[5:])
                bg(http_edit_message_text(chat_id, message_id, "🗑 Вакансия скрыта."))
    return web.Response(text="OK")


# ---------------- Очистка памяти ----------------
async def cleanup_old_data():
    while True:
        try:
            user_search_cache.clear()
            temp_vacancies.clear()
            log.info("🧹 Memory cleanup completed")
        except Exception as e:
            log.error(f"Cleanup error: {e}")
        await asyncio.sleep(6 * 3600)


# ---------------- Запуск ----------------
async def main():
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        webhook_url = f"{render_url.rstrip('/')}/{BOT_TOKEN}"
        async with HTTP.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}") as resp:
            log.info("setWebhook: %s", (await resp.text())[:200])
    log.info("🚀 Bot v3.2 started successfully.")
    bg(cleanup_old_data())
    try:
        await asyncio.Event().wait()
    finally:
        await HTTP.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass