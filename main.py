import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import html
import datetime
import time
import base64
import zipfile
import hashlib
import hmac
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
log = logging.getLogger("career_bot_v43")

# ---------------- Конфиг ----------------
RAW_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_TOKEN = RAW_BOT_TOKEN.strip().strip('"').strip("'")

if not BOT_TOKEN or ":" not in BOT_TOKEN:
    raise SystemExit(f"🔴 BOT_TOKEN invalid! Got: '{RAW_BOT_TOKEN[:20]}...'")

log.info(f"✅ Using cleaned BOT_TOKEN starting with: {BOT_TOKEN[:10]}...")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
MINI_APP_URL = os.getenv("MINI_APP_URL", "")

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
BOT_USERNAME = "LemusCareer_Bot"
TASKS = set()
temp_vacancies = {}
user_states = {}
user_adapt_target = {}
user_search_cache = {}
interview_sessions = {}
user_skillgap_cache = {}

# ============================================================
# 🌐 CORS MIDDLEWARE
# ============================================================

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Requested-With",
    "Access-Control-Max-Age": "86400",
}


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=dict(CORS_HEADERS))
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    except Exception as e:
        log.error(f"Unhandled error on {request.path}: {e}", exc_info=True)
        response = web.json_response({"error": f"Server error: {str(e)[:200]}"}, status=500)
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response


async def parse_json_body(request):
    try:
        raw = await request.text()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ============================================================
# 🛡️ ЗАЩИТА И ОГРАНИЧЕНИЯ
# ============================================================

MAX_FILE_SIZE = 5 * 1024 * 1024

user_rate_limits = {}
RATE_LIMIT_REQUESTS = 15
RATE_LIMIT_WINDOW = 60


def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    if user_id not in user_rate_limits:
        user_rate_limits[user_id] = []
    user_rate_limits[user_id] = [t for t in user_rate_limits[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(user_rate_limits[user_id]) >= RATE_LIMIT_REQUESTS:
        return False
    user_rate_limits[user_id].append(now)
    return True


def validate_ai_response(response: str, min_length: int = 50) -> bool:
    if not response:
        return False
    if len(response.strip()) < min_length:
        return False
    garbage_patterns = ["я не могу", "извините, но", "к сожалению", "не могу помочь", "как ии"]
    response_lower = response.lower()
    for pattern in garbage_patterns:
        if response_lower.startswith(pattern):
            return False
    return True


CITY_TO_HH_AREA = {
    "москва": 1, "санкт-петербург": 2, "петербург": 2, "екатеринбург": 3,
    "новосибирск": 4, "ростов-на-дону": 5, "нижний новгород": 6, "казань": 7,
    "самара": 8, "уфа": 9, "краснодар": 10, "воронеж": 11, "челябинск": 12,
    "омск": 13, "пермь": 14, "волгоград": 15, "красноярск": 16,
    "вся россия": 113, "россия": 113,
}

# ============================================================
# 📊 МОНИТОРИНГ НАГРУЗКИ
# ============================================================
bot_metrics = {
    "ai_requests_this_minute": 0, "active_users_this_hour": set(),
    "avg_ai_response_time": 0, "errors_this_hour": 0,
    "last_alert_time": {}, "total_requests_today": 0,
    "last_day_reset": datetime.date.today().isoformat(),
}

LOAD_THRESHOLDS = {
    "ai_requests_per_minute": {"warn": 15, "critical": 25, "label": "Запросов к ИИ в минуту"},
    "active_users_per_hour": {"warn": 30, "critical": 60, "label": "Активных пользователей в час"},
    "cache_size": {"warn": 5000, "critical": 10000, "label": "Размер кэша в памяти"},
    "avg_ai_response_time": {"warn": 20, "critical": 40, "label": "Среднее время ответа ИИ (сек)"},
    "errors_per_hour": {"warn": 5, "critical": 15, "label": "Ошибок за час"},
    "active_tasks": {"warn": 50, "critical": 100, "label": "Одновременных задач"},
}


def track_ai_request():
    bot_metrics["ai_requests_this_minute"] += 1
    bot_metrics["total_requests_today"] += 1


def track_user_activity(user_id):
    bot_metrics["active_users_this_hour"].add(user_id)


def track_error():
    bot_metrics["errors_this_hour"] += 1


async def send_admin_alert(alerts: list, level: str):
    if ADMIN_ID == 0:
        return
    now = time.time()
    last_sent = bot_metrics["last_alert_time"].get(level, 0)
    if now - last_sent < 1800:
        return
    bot_metrics["last_alert_time"][level] = now
    level_emoji = {"warn": "🟡", "warning": "🟠", "critical": "🔴"}[level]
    level_text = {"warn": "ВНИМАНИЕ", "warning": "ПРЕДУПРЕЖДЕНИЕ", "critical": "КРИТИЧНО"}[level]
    msg = f"{level_emoji} *{level_text}: Бот приближается к пределу нагрузки!*\n\n📊 *Метрики:*\n"
    for emoji, label, value, threshold in alerts:
        msg += f"{emoji} {label}: *{value}* (порог: {threshold})\n"
    msg += (
        f"\n📈 *Текущая статистика:*\n"
        f"• Всего запросов сегодня: {bot_metrics['total_requests_today']}\n"
        f"• Активных пользователей за час: {len(bot_metrics['active_users_this_hour'])}\n"
        f"• Одновременных задач: {len(TASKS)}\n"
        f"• Среднее время ответа ИИ: {bot_metrics['avg_ai_response_time']:.1f} сек\n\n"
        f"💡 *Рекомендации:*\n"
    )
    if level == "critical":
        msg += "• Срочно: переход на платный тариф Render (больше CPU/RAM)\n• Проверьте логи на ошибки"
    else:
        msg += "• Мониторьте ситуацию в ближайшие 30 минут"
    try:
        async with HTTP.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": ADMIN_ID, "text": msg, "parse_mode": "Markdown"
        }) as resp:
            await resp.json()
        log.info(f"Admin alert sent: {level}")
    except Exception as e:
        log.error(f"Failed to send admin alert: {e}")


async def monitor_load():
    log.info("📊 Load monitor started")
    while True:
        try:
            await asyncio.sleep(60)
            today = datetime.date.today().isoformat()
            if bot_metrics["last_day_reset"] != today:
                bot_metrics["total_requests_today"] = 0
                bot_metrics["last_day_reset"] = today
            metrics_snapshot = {
                "ai_requests_per_minute": bot_metrics["ai_requests_this_minute"],
                "active_users_per_hour": len(bot_metrics["active_users_this_hour"]),
                "cache_size": len(temp_vacancies) + len(user_search_cache) + len(user_states),
                "avg_ai_response_time": bot_metrics["avg_ai_response_time"],
                "errors_per_hour": bot_metrics["errors_this_hour"],
                "active_tasks": len(TASKS),
            }
            log.info(f"📊 Metrics: AI={metrics_snapshot['ai_requests_per_minute']}/min, "
                     f"Users={metrics_snapshot['active_users_per_hour']}/hr, "
                     f"Cache={metrics_snapshot['cache_size']}, "
                     f"AI_time={metrics_snapshot['avg_ai_response_time']:.1f}s")
            critical_alerts = []
            warning_alerts = []
            warn_alerts = []
            for metric_name, value in metrics_snapshot.items():
                threshold = LOAD_THRESHOLDS.get(metric_name, {})
                critical_limit = threshold.get("critical", 0)
                warn_limit = threshold.get("warn", 0)
                label = threshold.get("label", metric_name)
                if critical_limit and value >= critical_limit:
                    critical_alerts.append(("🔴", label, value, critical_limit))
                elif warn_limit and value >= warn_limit * 0.9:
                    warning_alerts.append(("🟠", label, value, warn_limit))
                elif warn_limit and value >= warn_limit * 0.7:
                    warn_alerts.append(("🟡", label, value, warn_limit))
            if critical_alerts:
                await send_admin_alert(critical_alerts, "critical")
            elif warning_alerts:
                await send_admin_alert(warning_alerts, "warning")
            elif warn_alerts:
                await send_admin_alert(warn_alerts, "warn")
            bot_metrics["ai_requests_this_minute"] = 0
            if datetime.datetime.now().minute == 0:
                bot_metrics["active_users_this_hour"].clear()
                bot_metrics["errors_this_hour"] = 0
        except Exception as e:
            log.error(f"Monitor error: {e}")


# ============================================================
# 🎓 КУРСЫ (ВКЛЮЧАЯ АНТИКРИЗИСНЫЙ)
# ============================================================
COURSES = {
    "resume": {
        "title": "🎓 Резюме за 1 час",
        "description": "Пошаговый курс по созданию резюме, которое не отсеят роботы",
        "lessons": [
            {"title": "Урок 1: Структура резюме, которое пройдёт ATS", "content": "📚 УРОК 1: Структура резюме, которое пройдёт ATS\n\n🎯 Цель: Понять как устроены системы автоматического отбора (ATS).\n\n✅ ПРАВИЛЬНАЯ СТРУКТУРА:\n1️⃣ ФИО и контакты.\n2️⃣ Желаемая должность (точная, как в вакансии).\n3️⃣ Краткое резюме / Summary (3-4 предложения).\n4️⃣ Ключевые навыки (списком).\n5️⃣ Опыт работы (от последнего к первому).\n6️⃣ Образование.\n7️⃣ Дополнительно.\n\n❌ ЧАСТЫЕ ОШИБКИ:\n• Креативные заголовки — робот их не понимает.\n• Таблицы и колонки — ломают парсинг.\n• Формат .doc вместо .docx.\n\n📝 ЗАДАНИЕ: Проверьте своё резюме по чек-листу.\n⏱ Время: 10 минут"},
            {"title": "Урок 2: Опыт через достижения", "content": "📚 УРОК 2: Опыт через достижения.\n\n💡 ГЛАВНОЕ: Разница между \"делал\" и \"сделал\".\n\n📐 ФОРМУЛА ДОСТИЖЕНИЯ:\n[Глагол действия] + [Что сделал] + [Измеримый результат]\n\n📝 ЗАДАНИЕ: Перепишите 3 пункта опыта по формуле достижений.\n⏱ Время: 15 минут"},
            {"title": "Урок 3: Ключевые слова и ATS-оптимизация", "content": "📚 УРОК 3: Ключевые слова и ATS-оптимизация.\n📝 ЗАДАНИЕ: Соберите список ключевых слов из 5 вакансий.\n⏱ Время: 15 минут"},
            {"title": "Урок 4: Сопроводительное письмо за 10 минут", "content": "📚 УРОК 4: Сопроводительное письмо за 10 минут.\n📝 ЗАДАНИЕ: Напишите сопроводительное письмо по формуле.\n⏱ Время: 10 минут"},
            {"title": "Урок 5: Финальная проверка и стратегия отправки", "content": "🎉 ПОЗДРАВЛЯЮ! Курс завершён!\n⏱ Время: 20 минут"}
        ]
    },
    "interview": {
        "title": "🎤 Собеседование без стресса",
        "description": "Как пройти любое собеседование уверенно",
        "lessons": [
            {"title": "Урок 1: Подготовка к собеседованию", "content": "📚 УРОК 1: Подготовка.\n📝 ЗАДАНИЕ: Подготовьте ответы на все вопросы.\n⏱ Время: 45 минут"},
            {"title": "Урок 2: Каверзные вопросы", "content": "📚 УРОК 2: Каверзные вопросы.\n📝 ЗАДАНИЕ: Запишите свои ответы на диктофон.\n⏱ Время: 30 минут"},
            {"title": "Урок 3: Вопросы о зарплате", "content": "📚 УРОК 3: Вопросы о зарплате.\n📝 ЗАДАНИЕ: Определите свою рыночную стоимость.\n⏱ Время: 20 минут"},
            {"title": "Урок 4: Как произвести впечатление", "content": "📚 УРОК 4: Как произвести впечатление.\n📝 ЗАДАНИЕ: Подготовьте 5 вопросов.\n⏱ Время: 15 минут"},
            {"title": "Урок 5: После собеседования", "content": "🎉 ПОЗДРАВЛЯЮ! Курс завершён!\n⏱ Время: 20 минут"}
        ]
    },
    "salary": {
        "title": "💰 Переговоры о зарплате",
        "description": "Как получить максимум от оффера",
        "lessons": [
            {"title": "Урок 1: Определите свою рыночную стоимость", "content": "📚 УРОК 1: Определите свою стоимость.\n📝 ЗАДАНИЕ: Рассчитайте свою рыночную стоимость.\n⏱ Время: 30 минут"},
            {"title": "Урок 2: Когда и как говорить о зарплате", "content": "📚 УРОК 2: Когда говорить о зарплате.\n📝 ЗАДАНИЕ: Подготовьте скрипт ответа.\n⏱ Время: 20 минут"},
            {"title": "Урок 3: Техники переговоров", "content": "📚 УРОК 3: Техники переговоров.\n📝 ЗАДАНИЕ: Потренируйтесь отвечать.\n⏱ Время: 25 минут"},
            {"title": "Урок 4: Торг за бонусы и условия", "content": "📚 УРОК 4: Торг за бонусы.\n📝 ЗАДАНИЕ: Составьте список из 5 пунктов.\n⏱ Время: 15 минут"},
            {"title": "Урок 5: Контр-оффер и финальное решение", "content": "🎉 ПОЗДРАВЛЯЮ! Вы прошли курс. Удачи! 💪\n Время: 15 минут"}
        ]
    },
    "crisis": {
        "title": "🆘 Поиск работы в кризис: план выхода из ямы",
        "description": "Для тех, кто ищет 3+ месяца: деньги, голова, пробелы, тишина, безопасность",
        "lessons": [
            {"title": "Урок 1: Деньги — комплект выживания", "content": "📚 УРОК 1: Деньги — комплект выживания\n\n🎯 Цель: остановить финансовое кровотечение за 1-2 дня, чтобы искать работу с ясной головой.\n\n1️⃣ *Аудит минимального бюджета.* Выпиши только обязательные расходы: жильё, еда, кредиты, дети, лекарства. Всё остальное — на паузу. Это твой пол.\n\n2️⃣ *Кредитные каникулы (106-ФЗ).* Право требовать каникулы, если доход упал более чем на 30% к среднемесячному за прошлый год. Один раз по каждому кредиту, до 6 месяцев. Заявление — в свой банк, можно онлайн. Альтернатива — реструктуризация: срок длиннее, платёж меньше.\n\n3️⃣ *Центр занятости.* Официальный статус безработного: пособие, бесплатное обучение, иногда оплачиваемые общественные работы. Оформление через Госуслуги.\n\n4️⃣ *Детские выплаты и субсидии.* Проверь право на пособия и субсидию на ЖКУ (если коммуналка съедает больше региональной доли дохода).\n\n5️⃣ *Письмо в банк.* Если под каникулы не проходишь — проси реструктуризацию человеческим письмом.\n\n📝 ЗАДАНИЕ: Прогони инструмент «Мой минимум оффера» и запиши свой пол на бумаге.\n⏱ Время: 40 минут"},
            {"title": "Урок 2: Голова — система микро-шагов", "content": "📚 УРОК 2: Голова — система микро-шагов\n\n💡 4 месяца поиска — марафон без видимого финиша. Выгорание — не слабость, а физиология.\n\n✅ *Правило 3 микро-шагов:* в день — 2 целевых отклика + 1 контакт с человеком (фоллоу-ап, тёплое письмо, звонок). Не 20 откликов в панике.\n\n✅ *Стрик важнее интенсивности:* 3 шага каждый день 30 дней сильнее, чем 50 откликов за ночь и неделя апатии.\n\n✅ *Вечерний ритуал:* запиши 3 сделанных сегодня дела. Мозгу нужна видимость прогресса.\n\n✅ *Один выходной в неделю:* день без поиска. Без чувства вины — это часть плана.\n\n📝 ЗАДАНИЕ: Запусти инструмент «План на сегодня» и выполни 3 шага до вечера.\n⏱ Время: 15 минут"},
            {"title": "Урок 3: Пробел — как объяснять дыру в резюме", "content": "📚 УРОК 3: Пробел — как объяснять дыру в резюме\n\n💡 Рекрутер боится не пробела, а сбивчивой оправдывающейся истории.\n\n📐 ФОРМУЛА: факт (спокойно, одним предложением) → что делал в паузе (учёба, фрилас, семейные обстоятельства без деталей) → почему сейчас сильнее (навыки, ясность фокуса).\n\n❌ ТАБУ: «меня никто не берёт», «я в депрессии», извинения, длинные оправдания.\n\n✅ ПРИМЕР: «После сокращения взял паузу: закрыл семейные обстоятельства и прошёл курс аналитики. Сейчас возвращаюсь с обновлённым стеком и чётким пониманием, какой продукт хочу развивать».\n\n📝 ЗАДАНИЕ: Сгенерируй свою версию инструментом «Объяснение пробела» и проговори её вслух 3 раза.\n⏱ Время: 20 минут"},
            {"title": "Урок 4: Тишина — система фоллоу-апов", "content": "📚 УРОК 4: Тишина — система фоллоу-апов\n\n💡 60% тишины — это процесс (отпуск согласующего, бюрократия), а не отказ тебе.\n\n📅 СХЕМА: отклик → фоллоу-ап на 5-7 день → второй на 12-14 день → закрыл вакансию и живёшь дальше.\n\n📐 ФОРМУЛА фоллоу-апа: благодарность + один новый факт о себе + вопрос о сроках решения.\n\n✅ ПРИМЕР: «Мария, здравствуйте! Спасибо за разговор 12-го. С тех пор закрыл сертификацию по X. Подскажите, сориентируете по срокам решения?»\n\n📝 ЗАДАНИЕ: Проставь статусы в трекере и отправь фоллоу-апы всем откликам старше 5 дней — инструмент «Фоллоу-ап после тишины» напишет их за тебя.\n⏱ Время: 25 минут"},
            {"title": "Урок 5: Минимум — не продавать себя дёшево", "content": "📚 УРОК 5: Минимум — не продавать себя дёшево\n\n💡 Оффер, принятый из страха на -40% от рынка, превращается в двухлетнюю яму дохода и самооценки.\n\n📐 ПОЛ = обязательные расходы месяца + 20% буфер. Ниже пола — только как временный мост с чётким сроком (например, 3 месяца) и продолжением поиска.\n\n✅ ФРАЗА-ДЕРЖАТЕЛЬ: «Я понимаю рынок и свой уровень: рассматриваю предложения от X. Ниже обсуждать не готов, но открыт к разговору о бонусной структуре».\n\n✅ Если давят «решайте сегодня» — нормальная компания даёт 2-3 дня. Давление — красный флаг.\n\n📝 ЗАДАНИЕ: Посчитай свой пол инструментом «Мой минимум оффера».\n⏱ Время: 15 минут"},
            {"title": "Урок 6: Безопасность, тёплые контакты, мост-доход", "content": "📚 УРОК 6: Безопасность, тёплые контакты, мост-доход\n\n🚩 КРАСНЫЕ ФЛАГИ СКАМА: предоплата за «обучение» или «документы», зарплата в 2-3 раза выше рынка за простую работу, нет юрлица и договора, давление «решите сегодня», просят CVC/коды из СМС.\n\n🤝 ТЁПЛЫЕ КОНТАКТЫ: до 70% руководящих вакансий не доходят до публикации. Формула письма: общее воспоминание → суть поиска одной фразой → конкретная лёгкая просьба (рекомендация/знакомство/15 минут звонка). Инструмент «Письмо тёплому контакту».\n\n🌉 МОСТ-ДОХОД: монетизируй свою экспертизу на время поиска — консультации, interim-проекты, фриланс по профилю, менторство. Не подработка веером, а 2-3 канала твоего уровня. Инструмент «Мост-доход».\n\n🎉 Ты прошёл антикризисный пакет. У тебя есть план, пол, история и инструменты. Дальше — микро-шагами. И помни: поиск работы — не приговор тебе как человеку.\n⏱ Время: 30 минут"}
        ]
    }
}

COVER_LETTER_TEMPLATES = [
    {"name": "🎯 Классический", "content": "Добрый день, [Имя]!\n\nУвидел вакансию [позиция] в [компания] и очень заинтересовался.\n\nМой опыт в [область] составляет [Х] лет. За это время я [главное достижение с цифрами].\nГотов обсудить, как мой опыт поможет решить ваши задачи.\n\nКогда удобно созвониться на 15 минут?\nС уважением,\n[Имя]"},
    {"name": "🔥 Цепляющий (для стартапов)", "content": "[Имя], добрый день!\nУвидел, что [компания] ищет [позиция]. Это именно то, чем я горю.\nВ прошлом году я [достижение]. Готов повторить и улучшить.\nКогда удобно?\n[Имя]"},
    {"name": "💼 Для крупных корпораций", "content": "Добрый день, [Имя]!\nМеня зовут [Имя], я [должность] с опытом [Х] лет.\nОсобенно интересна задача [конкретика] — я решал похожую в [предыдущая компания].\nС уважением,\n[Имя]"},
    {"name": "🚀 Для перехода из другой отрасли", "content": "Добрый день, [Имя]!\nЯ [Х] лет работал в [отрасль]. Теперь хочу применить этот опыт в [новая отрасль].\nКогда удобно созвониться?\n[Имя]"},
    {"name": "📊 Для аналитиков", "content": "Добрый день, [Имя]!\nМой опыт: [Х] лет в анализе данных.\nНедавний проект: [описание с метриками].\nКогда удобно?\n[Имя]"},
    {"name": "👥 Для менеджеров", "content": "Добрый день, [Имя]!\nЯ руководитель с опытом [Х] лет. Управлял командой из [Х] человек и достиг [результат].\nС уважением,\n[Имя]"},
    {"name": "🎓 Для джуниоров", "content": "Добрый день, [Имя]!\nЯ начинающий [должность]. Прошёл [курсы], где научился [навыки].\nГотов выполнить тестовое задание.\n[Имя]"},
    {"name": "💻 Для разработчиков", "content": "Добрый день, [Имя]!\nМой стек: [языки]. Опыт [Х] лет.\nНедавний проект: [описание].\nКогда удобно?\n[Имя]"},
    {"name": "🌟 Для отклика в соцсетях", "content": "[Имя], добрый день!\nУвидел ваш пост о поиске [позиция].\nЯ [кратко о себе].\nКогда удобно?\n[Имя]"},
    {"name": "📧 Короткий (для мессенджеров)", "content": "[Имя], добрый день!\nУвидел вакансию [позиция]. Мой опыт [Х] лет, [достижение].\nКогда удобно созвониться на 10 минут?\n[Имя]"}
]

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 7, unlimited_until TIMESTAMP, daily_count INTEGER DEFAULT 0, last_active_date TEXT, referred_by INTEGER, user_mode TEXT DEFAULT 'seeker', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS resumes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT, text TEXT, active INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS hidden_vacancies (user_id INTEGER, vacancy_id TEXT, PRIMARY KEY (user_id, vacancy_id));
CREATE TABLE IF NOT EXISTS liked_vacancies (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, vacancy_id TEXT, title TEXT, status TEXT DEFAULT 'Откликнулся');
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, message TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS social_shares (user_id INTEGER, network TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (user_id, network));
CREATE TABLE IF NOT EXISTS free_actions (user_id INTEGER, action_type TEXT, used_count INTEGER DEFAULT 0, PRIMARY KEY (user_id, action_type));
""")
conn.commit()

for _alter in (
    "ALTER TABLE users ADD COLUMN user_mode TEXT DEFAULT 'seeker'",
    "ALTER TABLE users ADD COLUMN digest_active INTEGER DEFAULT 0",
):
    try:
        cur.execute(_alter)
        conn.commit()
    except sqlite3.OperationalError:
        pass


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
    cur.execute("INSERT INTO users (user_id, username, balance, referred_by) VALUES (?, ?, 7, ?)",
                (user_id, username, referrer_id))
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


def set_user_mode(user_id: int, mode: str):
    cur.execute("UPDATE users SET user_mode=? WHERE user_id=?", (mode, user_id))
    conn.commit()


def get_user_mode(user_id: int) -> str:
    cur.execute("SELECT user_mode FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] else "seeker"


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
    cur.execute("SELECT used_count FROM free_actions WHERE user_id=? AND action_type=?", (user_id, action_type))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO free_actions (user_id, action_type, used_count) VALUES (?, ?, 1)", (user_id, action_type))
        conn.commit()
        return True
    if row[0] < max_free:
        cur.execute("UPDATE free_actions SET used_count = used_count + 1 WHERE user_id=? AND action_type=?", (user_id, action_type))
        conn.commit()
        return True
    return False


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
    cur.execute("INSERT INTO resumes (user_id, name, text, active) VALUES (?,?,?,1)", (user_id, name, text[:25000]))
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


# ---------------- ИИ-слой (С РЕТРАЯМИ) ----------------
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
    track_ai_request()
    start_time = time.time()
    result = None
    if client:
        for attempt in range(2):
            cands = list(GEMINI_MODEL_CANDIDATES)
            if _working_model["name"] in cands:
                cands.remove(_working_model["name"])
                cands.insert(0, _working_model["name"])
            for m in cands:
                try:
                    resp = client.models.generate_content(
                        model=m, contents=prompt,
                        config=gtypes.GenerateContentConfig(temperature=0.7))
                    if resp is not None and resp.text:
                        _working_model["name"] = m
                        result = resp.text
                        break
                except Exception as e:
                    log.warning("Gemini model %s failed: %s", m, str(e)[:100])
            if result:
                break
            _working_model["name"] = None
            time.sleep(2)
    if result is None and GROQ_KEY:
        try:
            result = _openai_compat(prompt, "https://api.groq.com/openai/v1", GROQ_KEY, GROQ_MODEL)
        except Exception as e:
            log.warning("Groq failed: %s", str(e)[:100])
    if result is None and OPENROUTER_KEY:
        try:
            result = _openai_compat(prompt, "https://openrouter.ai/api/v1", OPENROUTER_KEY, "qwen/qwen-2.5-7b-instruct:free")
        except Exception as e:
            log.warning("OpenRouter failed: %s", str(e)[:100])
    elapsed = time.time() - start_time
    bot_metrics["avg_ai_response_time"] = (bot_metrics["avg_ai_response_time"] * 0.8) + (elapsed * 0.2)
    if result is None:
        track_error()
    return result


async def extract_region_from_resume(resume_text: str) -> int:
    prompt = (
        "Проанализируй резюме и определи город проживания кандидата или желаемый регион работы.\n"
        f"Резюме:\n{resume_text[:2000]}\n\n"
        "Выдай ТОЛЬКО название города одним словом или фразой. Если не удалось определить — напиши 'Москва'."
    )
    city_response = await asyncio.to_thread(ai_generate, prompt)
    if not city_response or not validate_ai_response(city_response, min_length=3):
        return 1
    city = city_response.strip().lower()
    city = re.sub(r'[^а-яa-zё\- ]', '', city).strip()
    for city_name, area_code in CITY_TO_HH_AREA.items():
        if city_name in city:
            log.info(f"Region detected: {city} -> area {area_code}")
            return area_code
    return 113


# ---------------- Извлечение текста ----------------
def rtf_to_text(raw: str) -> str:
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: bytes.fromhex(m.group(1)).decode("cp1251", errors="ignore"), raw)
    text = re.sub(r"\\[a-z]+-?\d* ?", " ", text)
    text = re.sub(r"[{}]", "", text)
    return html.unescape(text).strip()


def extract_text_from_doc_binary(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            content = f.read()
        text = content.decode('utf-8', errors='ignore')
        text = re.sub(r'[^\x20-\x7E\u0400-\u04FF\u00C0-\u00FF\n\r\t.,;:!?()-]', ' ', text)
        text = re.sub(r' {3,}', '\n', text)
        return text.strip()
    except Exception as e:
        log.error(f"DOC extraction failed: {e}")
        return ""


def extract_text_from_odt(path: str) -> str:
    try:
        with zipfile.ZipFile(path, 'r') as z:
            if 'content.xml' in z.namelist():
                content_xml = z.read('content.xml').decode('utf-8')
                text = re.sub(r'<[^>]+>', '\n', content_xml)
                text = re.sub(r'\n{3,}', '\n\n', text)
                return text.strip()
    except Exception as e:
        log.error(f"ODT extraction failed: {e}")
    return ""


async def extract_text_from_image(path: str) -> str:
    if not client:
        return ""
    try:
        with open(path, 'rb') as f:
            image_data = f.read()
        ext = path.lower().split('.')[-1]
        mime_types = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}
        mime_type = mime_types.get(ext, 'image/jpeg')
        try:
            image_part = gtypes.Part.from_bytes(data=image_data, mime_type=mime_type)
            resp = client.models.generate_content(
                model=GEMINI_MODEL_CANDIDATES[0],
                contents=[image_part, "Извлеки ВЕСЬ текст из этого изображения. Выдай только текст без комментариев."]
            )
            if resp and resp.text:
                return resp.text
        except Exception as e1:
            log.warning(f"Part.from_bytes failed: {e1}")
            try:
                image_b64 = base64.b64encode(image_data).decode('utf-8')
                resp = client.models.generate_content(
                    model=GEMINI_MODEL_CANDIDATES[0],
                    contents=[
                        {"inline_data": {"mime_type": mime_type, "data": image_b64}},
                        "Извлеки ВЕСЬ текст из этого изображения. Выдай только текст без комментариев."
                    ]
                )
                if resp and resp.text:
                    return resp.text
            except Exception as e2:
                log.error(f"inline_data also failed: {e2}")
        return ""
    except Exception as e:
        log.error(f"Image text extraction failed: {e}")
        return ""


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
        elif fn.endswith(".doc"):
            text_content = extract_text_from_doc_binary(path)
        elif fn.endswith(".odt"):
            text_content = extract_text_from_odt(path)
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
            track_error()
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


async def show_typing(chat_id):
    try:
        await HTTP.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ============================================================
# 🎨 КЛАВИАТУРЫ
# ============================================================

def get_main_keyboard(is_admin=False):
    kb = [
        [{"text": "💼 Я ищу работу"}, {"text": "🏢 Я нанимаю"}],
        [{"text": "📁 Мои резюме"}, {"text": "💎 Оплата и Баланс"}],
        [{"text": "⏰ Продлить доступ"}, {"text": "🎁 Бонусы (Репост & Друзья)"}],
        [{"text": "💬 Обратная связь"}, {"text": "ℹ️ Помощь"}],
        [{"text": "🚀 Запустить бота"}],
    ]
    if is_admin:
        kb.append([{"text": "👑 Админ-панель"}, {"text": "📩 Сообщения от пользователей"}])
    return {"keyboard": kb, "resize_keyboard": True}


def get_job_seeker_keyboard(is_admin=False):
    kb = [
        [{"text": "🔍 Поиск вакансий"}, {"text": "🔗 Разобрать вакансию"}],
        [{"text": "🕵️ Найти ЛПР"}, {"text": "📝 Короткие Питчи"}],
        [{"text": "📄 Моё резюме"}, {"text": "🎤 Собеседование"}],
        [{"text": "📊 Трекер и статистика"}, {"text": "🎓 Премиум"}],
        [{"text": "🏠 Главное меню"}],
    ]
    return {"keyboard": kb, "resize_keyboard": True}


def get_seeker_resume_keyboard():
    kb = [
        [{"text": "📋 Аудит резюме"}, {"text": "📊 Анализ навыков (Skill Gap)"}],
        [{"text": "🛠 Адаптация резюме"}, {"text": "📁 Мои резюме"}],
        [{"text": "📥 Загрузить резюме"}, {"text": "📤 Экспорт резюме"}],
        [{"text": "⬅️ Назад к меню соискателя"}],
    ]
    return {"keyboard": kb, "resize_keyboard": True}


def get_seeker_interview_keyboard():
    kb = [
        [{"text": "🎤 Тренажер собеседований"}, {"text": "🌐 Вакансии из Сетки"}],
        [{"text": "⬅️ Назад к меню соискателя"}],
    ]
    return {"keyboard": kb, "resize_keyboard": True}


def get_seeker_tracker_keyboard():
    kb = [
        [{"text": "📌 Трекер откликов"}, {"text": "📊 Аналитика"}],
        [{"text": "🎯 План поиска"}],
        [{"text": "⬅️ Назад к меню соискателя"}],
    ]
    return {"keyboard": kb, "resize_keyboard": True}


def get_seeker_premium_keyboard():
    kb = [
        [{"text": "🎓 Курсы"}, {"text": "📝 Шаблоны писем"}],
        [{"text": "🆘 Антикризисный пакет"}],
        [{"text": "📊 Аналитика"}, {"text": "🎯 План поиска"}],
        [{"text": "⬅️ Назад к меню соискателя"}],
    ]
    return {"keyboard": kb, "resize_keyboard": True}


def get_recruiter_keyboard(is_admin=False):
    kb = [
        [{"text": "📊 Соответствие резюме вакансии"}],
        [{"text": "🎯 Скоринг кандидата"}, {"text": "❓ Вопросы для интервью"}],
        [{"text": "📝 Тестовое задание"}, {"text": "📋 Описание вакансии"}],
        [{"text": "📝 Вежливый отказ"}, {"text": "📄 Шаблон оффера"}],
        [{"text": "💰 Оценка зарплаты"}, {"text": "💬 Питч кандидату"}],
        [{"text": "📅 Фоллоу-ап после интервью"}],
        [{"text": "🏠 Главное меню"}],
    ]
    return {"keyboard": kb, "resize_keyboard": True}


def get_keyboard(is_admin=False):
    return get_main_keyboard(is_admin)


# ---------------- hh.ru парсинг ----------------
async def hh_api_search(query: str, region_code: int = 1):
    try:
        params = {"text": query, "area": region_code, "per_page": "50"}
        async with HTTP.get("https://api.hh.ru/vacancies",
                            params=params,
                            headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status == 429:
                await asyncio.sleep(5)
                return None
            if resp.status != 200:
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
                "id": i.get("id"), "name": i.get("name"),
                "company": (i.get("employer") or {}).get("name"),
                "salary": sal_str,
                "url": i.get("alternate_url") or f"https://hh.ru/vacancy/{i.get('id')}"
            })
        return items or None
    except Exception as e:
        log.warning("hh API failed: %s", str(e)[:150])
        track_error()
        return None


async def hh_scrape_search(query: str, region_code: int = 1):
    try:
        async with HTTP.get("https://hh.ru/search/vacancy",
                            params={"text": query, "area": region_code, "items_on_page": "50"},
                            headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
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
                "id": vid, "name": it.get("name"),
                "company": (it.get("company") or {}).get("name"),
                "salary": sal_str, "url": f"https://hh.ru/vacancy/{vid}"
            })
        return out or None
    except Exception as e:
        log.warning("hh scrape failed: %s", str(e)[:150])
        track_error()
        return None


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
        track_error()
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
            search_log.append(f"✅ Найдено {len(raw_results)} результатов")
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
                if contacts_raw and validate_ai_response(contacts_raw, min_length=10) and "НЕ НАЙДЕНО" not in contacts_raw.upper():
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
                    search_log.append(f"✅ Извлечено {len(found_contacts)} контактов")
        except Exception as e:
            log.error(f"Aggressive search error: {e}")
            search_log.append(f"❌ Ошибка поиска: {str(e)[:100]}")
            track_error()
    email_templates = []
    if company and not found_contacts:
        email_prompt = (
            f"Для компании '{company}' сгенерируй 3-5 шаблонов корпоративных почтовых адресов. "
            f"Выдай ТОЛЬКО список, каждый с новой строки."
        )
        templates_raw = await asyncio.to_thread(ai_generate, email_prompt)
        if templates_raw and validate_ai_response(templates_raw, min_length=10):
            email_templates = [t.strip() for t in templates_raw.split("\n") if "@" in t and len(t.strip()) < 50][:5]
    return {
        "found": len(found_contacts) > 0, "contacts": found_contacts,
        "email_templates": email_templates, "search_log": "\n".join(search_log),
        "queries_used": search_queries
    }


# ---------------- Утилиты ----------------
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


def clean_pitch_text(pitch: str, title: str) -> str:
    if not pitch:
        return ""
    garbage_in_pitch = ["Название позиции", "[позиция]", "[должность]", "{позиция}", "Позиция"]
    for garbage in garbage_in_pitch:
        if garbage in pitch:
            pitch = pitch.replace(garbage, title)
    return pitch


async def export_resume_docx(chat_id: int, user_id: int):
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Нет активного резюме для экспорта. Загрузите резюме.")
        return
    await show_typing(chat_id)
    try:
        doc = Document()
        lines = resume.split("\n")
        for i, line in enumerate(lines):
            clean_line = re.sub(r'[*#]', '', line).strip()
            if not clean_line:
                continue
            if i == 0 and len(clean_line) < 100:
                doc.add_heading(clean_line, level=1)
            elif any(keyword in clean_line.lower() for keyword in ["summary", "обо мне", "опыт работы", "образование", "ключевые навыки"]):
                doc.add_heading(clean_line, level=2)
            elif clean_line.startswith("•") or clean_line.startswith("-"):
                doc.add_paragraph(clean_line, style='List Bullet')
            else:
                doc.add_paragraph(clean_line)
        stream = io.BytesIO()
        doc.save(stream)
        file_bytes = stream.getvalue()
        await send_document_bytes(chat_id, file_bytes, "My_Resume.docx", "📤 *Ваше резюме экспортировано!*")
    except Exception as e:
        log.error(f"Resume export error: {e}")
        track_error()
        await send_telegram(chat_id, "⚠️ Ошибка экспорта.")


# ---------------- Разбор вакансии ----------------
async def analyze_vacancy_text(chat_id: int, user_id: int, vacancy_text: str):
    if not spend_balance(user_id, cost=2):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    await send_telegram(chat_id, "📄 *Разбор текста вакансии:* Извлекаю компанию, должность и контакты...")
    extract_prompt = (
        "Ты — эксперт по анализу вакансий. Проанализируй текст вакансии и вытащи:\n"
        "1. Название компании-работодателя.\n2. Точное название должности.\n3. Имя контактного лица (если указано).\n"
        f"Текст вакансии:\n{vacancy_text[:4000]}\n\n"
        "Выдай ТОЛЬКО JSON: {\"company\": \"...\", \"title\": \"...\", \"contact_name\": \"...\"}"
    )
    company = ""
    title = ""
    contact_name = ""
    try:
        ai_response = await asyncio.to_thread(ai_generate, extract_prompt)
        if ai_response and validate_ai_response(ai_response, min_length=10):
            clean = ai_response.replace("```json", "").replace("```", "").strip()
            json_match = re.search(r'\{.*\}', clean, re.S)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                    company = parsed.get("company", "").strip()
                    title = parsed.get("title", "").strip()
                    contact_name = parsed.get("contact_name", "").strip()
                    garbage_values = ["не найдено", "не указано", "неизвестно", "n/a", "null", "none", "-", ""]
                    if company.lower() in garbage_values: company = ""
                    if title.lower() in garbage_values: title = ""
                    if contact_name.lower() in garbage_values: contact_name = ""
                except json.JSONDecodeError:
                    log.warning("Failed to parse JSON from vacancy analysis")
    except Exception as e:
        log.error(f"Vacancy text parsing failed: {e}")
        track_error()
    if not company or not title:
        lines = [l.strip() for l in vacancy_text.split("\n") if l.strip()][:5]
        for line in lines:
            match = re.search(r'([А-ЯA-Z][\w\s\-\.]+?)\s+(?:ищет|приглашает|нанимает)\s+(.+?)(?:[,\.\-]|$)', line, re.I)
            if match and not company:
                company = match.group(1).strip()
                if not title:
                    title = match.group(2).strip()[:60]
    if not company or not title:
        await send_telegram(chat_id,
            f"🤔 *Не удалось определить компанию или должность.*\n"
            f"Напиши следующим сообщением в формате: `Компания, Должность`")
        user_states[user_id] = "waiting_for_company_correction"
        user_search_cache[user_id] = {
            "vacancy_title": title or "", "vacancy_description": vacancy_text[:3000],
            "resume": get_active_resume(user_id) or "Резюме не указано", "contact_name": contact_name
        }
        return
    await send_telegram(chat_id, f"🏢 *Распознано:* `{company}` | `{title}`\n🔍 Запускаю поиск контактов...")
    final_report = f"🏢 *Компания:* {company}\n💼 *Позиция:* {title}\n\n📇 *Контакты:* {contact_name or '❌ не указано'}\n"
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
    resume = get_active_resume(user_id) or "Резюме не указано"
    pitch_prompt = (
        f"Напиши короткий питч (4-5 строк) для рекрутера компании '{company}' на позицию '{title}'.\n"
        f"Имя рекрутера: {contact_name or 'неизвестно'}\n"
        f"Резюме: {resume[:1500]}\nВыдай ТОЛЬКО текст питча."
    )
    pitch = await asyncio.to_thread(ai_generate, pitch_prompt)
    if pitch and validate_ai_response(pitch, min_length=50):
        pitch = clean_pitch_text(pitch, title)
        final_report += f"\n📝 *Питч для отправки:*\n\n{pitch}"
    encoded_company = urllib.parse.quote(company)
    inline_kb = [
        [{"text": "🔍 LinkedIn", "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_company}%22+HR"}],
        [{"text": "🌐 Карьерный сайт", "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded_company}%22+контакты"}]
    ]
    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Разобрана: Контакт')",
                (user_id, f"text_{int(datetime.datetime.now().timestamp())}", f"{title} ({company})"))
    conn.commit()
    final_report += "\n\n📌 _Вакансия добавлена в Трекер._"
    await send_telegram(chat_id, final_report, {"inline_keyboard": inline_kb})


# ============================================================
# 🏢 ФУНКЦИИ ДЛЯ РЕКРУТЕРОВ
# ============================================================

async def hr_analyze_candidate_match(chat_id: int, user_id: int, resume_text: str, vacancy_text: str):
    if not spend_balance(user_id, cost=2):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    await send_telegram(chat_id, "📊 *Анализирую соответствие...*")
    prompt = (
        "Ты — опытный рекрутер. Проанализируй соответствие резюме кандидата требованиям вакансии.\n"
        f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}\n\n"
        f"--- ТЕКСТ ВАКАНСИИ ---\n{vacancy_text[:3000]}\n\n"
        "Выдай структурированный анализ:\n"
        "🎯 ОБЩИЙ ПРОЦЕНТ СООТВЕТСТВИЯ: [0-100]%\n"
        "Разбивка по категориям:\n"
        "   • Опыт работы: [0-100]% (почему)\n"
        "   • Навыки и компетенции: [0-100]% (почему)\n"
        "   • Образование: [0-100]% (почему)\n"
        "   • Дополнительные требования: [0-100]% (почему)\n"
        "✅ СИЛЬНЫЕ СТОРОНЫ: [3-5 пунктов]\n"
        "⚠️ ПРОБЕЛЫ: [3-5 пунктов]\n"
        "🚩 КРАСНЫЕ ФЛАГИ: [или 'Не обнаружены']\n"
        "❓ ВОПРОСЫ ДЛЯ ИНТЕРВЬЮ: [3-5 вопросов]\n"
        "📋 ВЕРДИКТ: [Рекомендуем/Под вопросом/Не рекомендуем]\n[Обоснование]"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis or not validate_ai_response(analysis, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📊 *Результат анализа:*\n\n{analysis}")


async def hr_generate_interview_questions(chat_id: int, user_id: int, resume_text: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    await send_telegram(chat_id, "❓ *Генерирую вопросы...*")
    prompt = (
        "Ты — опытный интервьюер. Составь список вопросов для собеседования.\n"
        f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}\n\n"
        "Составь вопросы по категориям:\n"
        "1. 🎯 Вопросы по опыту (3-4).\n"
        "2. 🔍 Уточняющие по пробелам (2-3).\n"
        "3. 🚩 Проверка красных флагов (2-3).\n"
        "4. 💡 Поведенческие (2-3).\n"
        "5. 🎪 Каверзные (1-2).\n"
        "6. 🤝 О мотивации (2)."
    )
    questions = await asyncio.to_thread(ai_generate, prompt)
    if not questions or not validate_ai_response(questions, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"❓ *Вопросы для интервью:*\n\n{questions}")


async def hr_generate_test_task(chat_id: int, user_id: int, resume_text: str):
    if not spend_balance(user_id, cost=2):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    await send_telegram(chat_id, "📝 *Генерирую тестовое задание...*")
    prompt = (
        "Ты — опытный нанимающий менеджер. Составь тестовое задание на основе резюме.\n"
        f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}\n\n"
        "ПРАВИЛА:\n"
        "1. Основано на том, что кандидат указал в резюме.\n"
        "2. Если управлял P&L — попроси рассчитать юнит-экономику.\n"
        "3. Если запускал продукт — попроси описать метрики, риски, план Б.\n"
        "4. Если оптимизировал процессы — попроси расчёт эффекта.\n"
        "5. Если руководил командой — попроси кейс управления конфликтом.\n"
        "6. Задание выполнимо за 1-2 часа.\n"
        "Выдай:\n"
        "📋 НАЗВАНИЕ\n⏱ ВРЕМЯ\n📝 ОПИСАНИЕ\n🎯 ЧТО ПРОВЕРЯЕМ\n✅ КРИТЕРИИ\n❓ ВОПРОСЫ ПОСЛЕ"
    )
    test_task = await asyncio.to_thread(ai_generate, prompt)
    if not test_task or not validate_ai_response(test_task, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📝 *Тестовое задание:*\n\n{test_task}")


async def hr_generate_vacancy_description(chat_id: int, user_id: int, params: str):
    if not spend_balance(user_id, cost=2):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    await send_telegram(chat_id, "📋 *Генерирую описание вакансии...*")
    prompt = (
        f"Ты — опытный рекрутер. Составь описание вакансии по структуре hh.ru.\n"
        f"Вводные: {params}\n\n"
        "Структура:\n"
        "📌 НАЗВАНИЕ ДОЛЖНОСТИ\n"
        "🏢 О КОМПАНИИ\n"
        "🎯 ОБЯЗАННОСТИ (5-8 пунктов)\n"
        "✅ ТРЕБОВАНИЯ (must have + nice to have)\n"
        "💎 УСЛОВИЯ\n"
        "🚀 ПРЕИМУЩЕСТВА\n"
        "📩 ПРИЗЫВ К ДЕЙСТВИЮ"
    )
    description = await asyncio.to_thread(ai_generate, prompt)
    if not description or not validate_ai_response(description, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📋 *Описание вакансии:*\n\n{description}")


async def hr_generate_rejection_letter(chat_id: int, user_id: int, params: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    prompt = (
        f"Напиши вежливое письмо с отказом кандидату.\n"
        f"Параметры: {params}\n\n"
        "Письмо должно быть уважительным, кратким (до 150 слов)."
    )
    letter = await asyncio.to_thread(ai_generate, prompt)
    if not letter or not validate_ai_response(letter, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📝 *Вежливый отказ:*\n\n{letter}")


async def hr_generate_offer_letter(chat_id: int, user_id: int, params: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    prompt = (
        f"Составь профессиональный шаблон оффера.\n"
        f"Параметры: {params}\n\n"
        "Оффер должен включать: поздравление, должность, условия, дату выхода, испытательный срок, следующие шаги."
    )
    offer = await asyncio.to_thread(ai_generate, prompt)
    if not offer or not validate_ai_response(offer, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📄 *Шаблон оффера:*\n\n{offer}")


async def hr_estimate_salary(chat_id: int, user_id: int, params: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    prompt = (
        f"Ты — эксперт по компенсациям. Оцени рыночную зарплату.\n"
        f"Параметры: {params}\n\n"
        "Выдай:\n"
        "1. 💰 Вилка зарплаты (минимум - медиана - максимум).\n"
        "2. 📊 Факторы влияния.\n"
        "3. 🎁 Типичный пакет бонусов.\n"
        "4. 📈 Тренды.\n"
        "5. 💡 Рекомендации."
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis or not validate_ai_response(analysis, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"💰 *Оценка зарплаты:*\n\n{analysis}")


async def hr_candidate_pitch(chat_id: int, user_id: int, params: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    prompt = (
        f"Ты — рекрутер. Напиши питч для кандидата.\n"
        f"Параметры: {params}\n\n"
        "Цепляет с первого предложения, показывает ценность, подчёркивает рост. Стиль от равного к равному."
    )
    pitch = await asyncio.to_thread(ai_generate, prompt)
    if not pitch or not validate_ai_response(pitch, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"💬 *Питч для кандидата:*\n\n{pitch}")


async def hr_followup_after_interview(chat_id: int, user_id: int, params: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    prompt = (
        f"Напиши фоллоу-ап письмо кандидату после собеседования.\n"
        f"Параметры: {params}\n\n"
        "Поблагодари, подчеркни что впечатлило, опиши следующие шаги."
    )
    followup = await asyncio.to_thread(ai_generate, prompt)
    if not followup or not validate_ai_response(followup, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📅 *Фоллоу-ап:*\n\n{followup}")


async def analyze_setka_post(chat_id: int, user_id: int, post_text: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ У вас закончились запросы!")
        return
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    await show_typing(chat_id)
    prompt = (
        "Ты — карьерный стратег. Пользователь нашел пост о найме.\n"
        "1. Соответствие резюме в %.\n2. Сильные стороны.\n3. Идеальное сообщение для лички.\n"
        f"--- ПОСТ ---\n{post_text[:3000]}\n\n--- РЕЗЮМЕ ---\n{resume[:5000]}"
    )
    result = await asyncio.to_thread(ai_generate, prompt)
    if not result or not validate_ai_response(result, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    title_match = re.search(r'(ищем|вакансия|требуется|позиция)\s+([^\n\.,]+)', post_text, re.IGNORECASE)
    role_title = title_match.group(0)[:40] if title_match else "Вакансия из Сетки"
    cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Сетка: Контакт')",
                (user_id, "setka_" + str(int(datetime.datetime.now().timestamp())), role_title))
    conn.commit()
    await send_telegram(chat_id, f"🌐 *Разбор предложения:*\n\n{result}")


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
        match_badge = f"🎯 Соответствие: {match_score}% ({match_reason})\n"
        sal_line = f"{sal}\n" if sal else ""
        markup = {"inline_keyboard": [
            [{"text": "👍 Откликнулся", "callback_data": f"like_{vid}"},
             {"text": "✍️ Сопроводительное", "callback_data": f"gen_{vid}"}],
            [{"text": "📊 Соответствие", "callback_data": f"match_{vid}"},
             {"text": "🎯 Питч для ЛПР", "callback_data": f"pitch_{vid}"}],
            [{"text": "🗑 Мусор", "callback_data": f"hide_{vid}"}]
        ]}
        await send_telegram(chat_id, f"🏢 *{comp}*\n💼 [{name}]({v.get('url')})\n{sal_line}{match_badge}", markup)
        await asyncio.sleep(0.2)
    if end < len(items):
        more_markup = {"inline_keyboard": [[{"text": "▶ Далее", "callback_data": f"page_{page + 1}"}]]}
        await send_telegram(chat_id, f"💡 Осталось {len(items) - end} вакансий.", more_markup)


async def handle_search(chat_id: int, user_id: int, is_admin: bool):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ У вас закончились запросы!", get_job_seeker_keyboard(is_admin))
        return
    active_resume = get_active_resume(user_id)
    if not active_resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    await show_typing(chat_id)
    await send_telegram(chat_id, "🔍 Подбираю вакансии по резюме (регион, отрасль, уровень, топ-компании)...")
    result = await core_search_vacancies(user_id)
    if result.get("error"):
        await send_telegram(chat_id, f"⚠️ {result['error']}", get_job_seeker_keyboard(is_admin))
        return
    scored_list = result["vacancies"]
    if not scored_list:
        await send_telegram(chat_id, "⚠️ Не удалось найти вакансии.", get_job_seeker_keyboard(is_admin))
        return
    user_search_cache[user_id] = {"items": scored_list}
    await send_telegram(chat_id,
        f"🔥 Нашел {len(scored_list)} вакансий.\n🎯 Запросы: {', '.join(result['queries'][:3])}",
        get_job_seeker_keyboard(is_admin))
    await send_vacancies_page(chat_id, user_id, page=0)


async def run_skill_gap_analysis(chat_id: int, user_id: int):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
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
    if not analysis or not validate_ai_response(analysis, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    user_skillgap_cache[user_id] = analysis
    markup = {"inline_keyboard": [[{"text": "🚀 Исправить резюме", "callback_data": "fix_resume_from_gap"}]]}
    await send_telegram(chat_id, f"📊 *Анализ навыков:*\n\n{analysis}", markup)


async def run_fix_resume_by_gap(chat_id: int, user_id: int):
    if not check_free_action(user_id, "resume_fix", max_free=1):
        await send_telegram(chat_id, "🔒 *Бесплатный лимит исчерпан!*")
        return
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    resume = get_active_resume(user_id)
    gap = user_skillgap_cache.get(user_id, "Усилить бизнес-метрики")
    await show_typing(chat_id)
    await send_telegram(chat_id, "⚙️ *Переписываю резюме...*")
    prompt = (
        "Ты — элитный карьерный консультант. Перепиши резюме кандидата по рекомендациям.\n"
        f"РЕКОМЕНДАЦИИ:\n{gap[:3000]}\n\nИСХОДНОЕ РЕЗЮМЕ:\n{resume[:6000]}\n\n"
        "ПРАВИЛА:\n"
        "1. Сохрани ВСЕ факты.\n"
        "2. Замени слабые глаголы на сильные.\n"
        "3. Добавь цифры.\n"
        "4. Структура: ФИО → Контакты → Summary → Навыки → Опыт → Образование.\n"
        "5. Выдай ТОЛЬКО текст резюме."
    )
    improved = await asyncio.to_thread(ai_generate, prompt)
    if not improved or not validate_ai_response(improved, min_length=200):
        await send_telegram(chat_id, "⚠️ ИИ вернул некорректный результат.")
        return
    try:
        doc = Document()
        lines = improved.split("\n")
        for i, line in enumerate(lines):
            clean_line = re.sub(r'[*#]', '', line).strip()
            if not clean_line:
                continue
            if i == 0 and len(clean_line) < 100:
                doc.add_heading(clean_line, level=1)
            elif any(keyword in clean_line.lower() for keyword in ["summary", "обо мне", "опыт работы", "образование", "ключевые навыки"]):
                doc.add_heading(clean_line, level=2)
            elif clean_line.startswith("•") or clean_line.startswith("-"):
                doc.add_paragraph(clean_line, style='List Bullet')
            else:
                doc.add_paragraph(clean_line)
        stream = io.BytesIO()
        doc.save(stream)
        file_bytes = stream.getvalue()
        add_resume(user_id, "Optimized_Resume.docx", improved)
        await send_document_bytes(chat_id, file_bytes, "Optimized_Resume.docx",
            "💎 *Ваше улучшенное резюме готово!*\n"
            "✅ Переписано по рекомендациям Skill Gap.\n"
            "✅ Сохранено как новое активное резюме.")
    except Exception as e:
        log.error("DOCX error: %s", e)
        track_error()
        await send_telegram(chat_id, "⚠️ Ошибка файла.")


async def run_ai_generation(chat_id: int, user_id: int, vac_info: dict):
    await show_typing(chat_id)
    resume = get_active_resume(user_id) or "Опыт не указан."
    letter = await asyncio.to_thread(ai_generate,
        f"Напиши сопроводительное письмо на позицию '{vac_info.get('title', '')}' "
        f"в '{vac_info.get('employer', '')}'.\nРезюме:\n{resume}")
    if not letter or not validate_ai_response(letter, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📝 *Сопроводительное:*\n\n{letter}")


async def osint_search_manager(chat_id: int, user_id: int, target_info: str):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    resume = get_active_resume(user_id) or "Резюме не указано"
    prompt = (
        f"Ты — эксперт по executive search. Цель: {target_info}\nРезюме: {resume[:2000]}\n"
        "1. Кто принимает решение о найме.\n2. 3 Google Dorks для LinkedIn/TenChat/TG.\n"
        "3. Короткое Cold DM сообщение.\n4. Лайфхаки."
    )
    result = await asyncio.to_thread(ai_generate, prompt)
    if not result or not validate_ai_response(result, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    company_name = target_info.split(",")[0].strip().split()[0] if target_info else "Company"
    encoded_company = urllib.parse.quote(company_name)
    live_results = []
    if DDGS_AVAILABLE:
        live_results = await live_search_recruiter(company_name)
    links_kb = [
        [{"text": "🔍 HR в LinkedIn", "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_company}%22+HR"}],
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
    await show_typing(chat_id)
    resume = get_active_resume(user_id) or "Резюме не указано"
    if "," in target_info:
        parts = target_info.split(",", 1)
        company = parts[0].strip()
        title = parts[1].strip()
    else:
        company = target_info.strip()
        title = "Специалист"
    prompt = (
        f"Напиши короткий питч (4-5 строк) для рекрутера компании '{company}' на позицию '{title}'.\n"
        f"Резюме: {resume[:2000]}\nВыдай ТОЛЬКО текст."
    )
    pitch = await asyncio.to_thread(ai_generate, prompt)
    if pitch and validate_ai_response(pitch, min_length=50):
        pitch = clean_pitch_text(pitch, title)
        await send_telegram(chat_id, f"🎯 *Питч для ЛПР:*\n\n{pitch}")
    else:
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")


async def run_pitch_generation(chat_id: int, user_id: int, vac_info: dict):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    resume = get_active_resume(user_id) or "Опыт не указан."
    pitch = await asyncio.to_thread(ai_generate,
        f"Короткий питч (4-5 строк) для рекрутера '{vac_info.get('employer', '')}' на '{vac_info.get('title', '')}'.\n"
        f"Резюме: {resume[:2000]}")
    if pitch and validate_ai_response(pitch, min_length=50):
        pitch = clean_pitch_text(pitch, vac_info.get('title', 'Специалист'))
        await send_telegram(chat_id, f"🎯 *Питч для ЛПР:*\n\n{pitch}")


async def run_vacancy_match(chat_id: int, user_id: int, vac_info: dict):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    resume = get_active_resume(user_id) or "Резюме не найдено."
    prompt = (
        f"Оцени соответствие резюме вакансии '{vac_info.get('title', '')}' в '{vac_info.get('employer', '')}'.\n"
        f"% соответствия, сильные стороны, пробелы.\nРезюме:\n{resume}"
    )
    analysis = await asyncio.to_thread(ai_generate, prompt)
    if not analysis or not validate_ai_response(analysis, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"📊 *Анализ соответствия:*\n\n{analysis}")


async def run_resume_adaptation(chat_id: int, user_id: int, resume_id: int, vacancy_text: str):
    if not check_free_action(user_id, "resume_adapt", max_free=1):
        await send_telegram(chat_id, "🔒 *Бесплатный лимит исчерпан!*")
        return
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    resume_text = get_resume_by_id(user_id, resume_id) or get_active_resume(user_id)
    if not resume_text:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    current_date = datetime.date.today().strftime("%d.%m.%Y")
    prompt = (
        f"Дата: {current_date}. Перепиши резюме под вакансию. ТОЛЬКО текст, начиная с ФИО.\n"
        f"Вакансия:\n{vacancy_text[:3000]}\n\nРезюме:\n{resume_text[:6000]}"
    )
    adapted = await asyncio.to_thread(ai_generate, prompt)
    if not adapted or not validate_ai_response(adapted, min_length=200):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    if "---" in adapted:
        adapted = adapted.split("---")[-1].strip()
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
        track_error()


async def run_resume_audit(chat_id: int, user_id: int):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    audit = await asyncio.to_thread(ai_generate, f"Глубокий аудит резюме:\n{resume[:8000]}")
    rewrite = await asyncio.to_thread(ai_generate, f"Перепиши для позиций выше:\n{resume[:8000]}")
    if audit and validate_ai_response(audit, min_length=100) and rewrite and validate_ai_response(rewrite, min_length=200):
        await send_telegram(chat_id, f"📋 *Аудит резюме:*\n\n{audit}")
        doc = Document()
        for p in rewrite.split("\n"):
            clean_p = re.sub(r'[*#]', '', p).strip()
            if clean_p:
                doc.add_paragraph(clean_p)
        stream = io.BytesIO()
        doc.save(stream)
        await send_document_bytes(chat_id, stream.getvalue(), "Resume_Pro.docx", "✅ Оптимизированное резюме готово!")


async def show_courses(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(chat_id, "🔒 *Курсы доступны только премиум-пользователям!*")
        return
    courses_msg = "🎓 *Курсы для премиум-пользователей*\n\n"
    for course_id, course in COURSES.items():
        courses_msg += f"*{course['title']}*\n{course['description']}\n\n"
    kb = {
        "inline_keyboard": [
            [{"text": "🎓 Резюме за 1 час", "callback_data": "course_resume"}],
            [{"text": "🎤 Собеседование без стресса", "callback_data": "course_interview"}],
            [{"text": "💰 Переговоры о зарплате", "callback_data": "course_salary"}],
            [{"text": "🆘 Поиск работы в кризис", "callback_data": "course_crisis"}]
        ]
    }
    await send_telegram(chat_id, courses_msg, kb)


async def show_course_lessons(chat_id: int, user_id: int, course_id: str):
    if course_id not in COURSES:
        await send_telegram(chat_id, "⚠️ Курс не найден.")
        return
    course = COURSES[course_id]
    lessons_msg = f"{course['title']}\n\n📚 *Уроки:*\n"
    inline_kb = []
    for i, lesson in enumerate(course["lessons"], 1):
        lessons_msg += f"{i}. {lesson['title']}\n"
        inline_kb.append([{"text": lesson['title'][:35], "callback_data": f"lesson_{course_id}_{i}"}])
    await send_telegram(chat_id, lessons_msg, {"inline_keyboard": inline_kb})


async def show_lesson(chat_id: int, user_id: int, course_id: str, lesson_num: int):
    if course_id not in COURSES:
        return
    course = COURSES[course_id]
    if lesson_num < 1 or lesson_num > len(course["lessons"]):
        return
    lesson = course["lessons"][lesson_num - 1]
    nav_kb = []
    if lesson_num > 1:
        nav_kb.append({"text": f"← Урок {lesson_num - 1}", "callback_data": f"lesson_{course_id}_{lesson_num - 1}"})
    if lesson_num < len(course["lessons"]):
        nav_kb.append({"text": f"Урок {lesson_num + 1} →", "callback_data": f"lesson_{course_id}_{lesson_num + 1}"})
    await send_telegram(chat_id, lesson["content"], {"inline_keyboard": [nav_kb] if nav_kb else []})


async def show_cover_letter_templates(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(chat_id, "🔒 *Шаблоны доступны только премиум-пользователям!*")
        return
    templates_msg = "📝 *Шаблоны сопроводительных писем*\n\n"
    inline_kb = []
    for i, template in enumerate(COVER_LETTER_TEMPLATES, 1):
        templates_msg += f"{i}. {template['name']}\n"
        inline_kb.append([{"text": template['name'][:35], "callback_data": f"template_{i}"}])
    await send_telegram(chat_id, templates_msg, {"inline_keyboard": inline_kb})


async def show_template(chat_id: int, user_id: int, template_num: int):
    if template_num < 1 or template_num > len(COVER_LETTER_TEMPLATES):
        return
    template = COVER_LETTER_TEMPLATES[template_num - 1]
    await send_telegram(chat_id, f"{template['name']}\n\n{template['content']}")


async def show_analytics(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(chat_id, "🔒 *Аналитика доступна только премиум-пользователям!*")
        return
    cur.execute("SELECT COUNT(*) FROM liked_vacancies WHERE user_id=?", (user_id,))
    total_vacancies = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM liked_vacancies WHERE user_id=? AND status LIKE '%Контакт%'", (user_id,))
    contacted = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM resumes WHERE user_id=?", (user_id,))
    total_resumes = cur.fetchone()[0]
    conversion = (contacted / total_vacancies * 100) if total_vacancies > 0 else 0
    analytics_msg = (
        "📊 *Расширенная аналитика*\n"
        f"📋 *Всего разобрано вакансий:* {total_vacancies}\n"
        f"📧 *Установлено контактов:* {contacted}\n"
        f"📈 *Конверсия в контакт:* {conversion:.1f}%\n"
        f"📁 *Загружено резюме:* {total_resumes}\n"
    )
    await send_telegram(chat_id, analytics_msg)


async def generate_job_search_plan(chat_id: int, user_id: int):
    if not is_premium_user(user_id):
        await send_telegram(chat_id, "🔒 *План поиска доступен только премиум-пользователям!*")
        return
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    await show_typing(chat_id)
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    prompt = (
        f"Ты — карьерный стратег. Составь персональный план поиска работы на 7 дней.\n"
        f"Резюме:\n{resume[:3000]}\n"
        "План должен включать:\n"
        "1. Конкретные действия на каждый день.\n"
        "2. Сколько вакансий разбирать в день.\n"
        "3. Кого искать и как выходить на ЛПР.\n"
        "4. Какие документы готовить.\n"
        "5. Когда отправлять отклики.\n"
        "6. Метрики успеха."
    )
    plan = await asyncio.to_thread(ai_generate, prompt)
    if not plan or not validate_ai_response(plan, min_length=100):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    await send_telegram(chat_id, f"🎯 *Персональный план поиска работы на неделю:*\n\n{plan}")


# ============================================================
# 🆘 АНТИКРИЗИСНЫЙ ПАКЕТ (ПРЕМИУМ)
# ============================================================

CRISIS_TOOLS_SET = {"gap", "followup", "scam", "min", "warm", "bridge", "day"}

CRISIS_HINTS = {
    "gap": "🕳 *Объяснение пробела*\nНапиши: сколько длился пробел и причина своими словами.\n_Пример: 8 месяцев, сокращение + уход за болеющим родственником._",
    "followup": "📮 *Фоллоу-ап после тишины*\nНапиши: компания, должность, сколько дней тишины.\n_Пример: Сбер, Руководитель продукта, 9 дней._",
    "scam": "🛡 *Проверка вакансии на скам*\nПришли полный текст вакансии следующим сообщением.",
    "min": "💰 *Мой минимум оффера*\nНапиши обязательные расходы в месяц.\n_Пример: кредиты 25000, аренда 45000, дети 30000, прочее 20000._",
    "warm": "🤝 *Письмо тёплому контакту*\nНапиши: имя и где работали вместе.\n_Пример: Мария, экс-коллега из МТС (мой руководитель)._",
}

CRISIS_TITLES = {
    "gap": "🕳 *Твоя история пробела:*",
    "followup": "📮 *Фоллоу-ап готов:*",
    "scam": "🛡 *Проверка вакансии:*",
    "min": "💰 *Твой минимум и стратегия:*",
    "warm": "🤝 *Письмо готово:*",
    "bridge": "🌉 *Мост-доход по твоим навыкам:*",
    "day": "📅 *План на сегодня:*",
}


def build_crisis_prompt(tool: str, params: str, resume: str) -> str:
    if tool == "gap":
        return (
            "Ты — карьерный консультант и психолог. Ситуация кандидата: пробел в занятости.\n"
            f"Вводные: {params}\n"
            "Составь:\n"
            "1) Уверенный нарратив пробела на 2-3 предложения (формула: факт → чем занимался в паузе → почему сейчас сильнее);\n"
            "2) Короткие ответы на 3 каверзных вопроса рекрутера о пробеле;\n"
            "3) Три фразы-табу, которые нельзя говорить.\n"
            "Тон: достойный, без извинений и драмы.\n"
            f"Резюме кандидата для контекста:\n{resume[:1500]}"
        )
    if tool == "followup":
        return (
            "Ты — мастер деловой коммуникации. Напиши фоллоу-ап письмо HR после тишины на отклик.\n"
            f"Вводные: {params}\n"
            "Структура: благодарность за возможность отклика/интервью → один новый конкретный факт о себе "
            "(достижение, сертификация, проект) → вопрос о сроках решения и следующих шагах.\n"
            "Объём до 120 слов. Тон: тёплый, уверенный, без давления и без отчаяния."
        )
    if tool == "scam":
        return (
            "Ты — эксперт по трудовому мошенничеству. Проанализируй вакансию на признаки скама.\n"
            f"Текст вакансии:\n{params[:4000]}\n\n"
            "Выдай:\n"
            "1) Список найденных красных флагов (или «не найдены»);\n"
            "2) Уровень риска: низкий/средний/высокий;\n"
            "3) Что проверить до согласия (юрлицо в ЕГРЮЛ, отзывы, договор, оформление по ТК);\n"
            "4) Что нельзя отправлять и платить ни при каких условиях."
        )
    if tool == "min":
        return (
            "Ты — финансовый советник и коуч по переговорам. Вводные — обязательные расходы кандидата в месяц: "
            f"{params}\n"
            "Выдай:\n"
            "1) Расчёт минимального приемлемого оффера: сумма расходов + 20% буфер (покажи арифметику);\n"
            "2) Точную фразу для переговоров, удерживающую этот пол;\n"
            "3) Две стратегии, если оффер ниже пола: временный мост с дедлайном и торг немедными условиями;\n"
            "4) Одно поддерживающее предложение: почему согласие ниже пола из страха — это потерянные 2 года."
        )
    if tool == "warm":
        return (
            "Ты — эксперт по нетворкингу. Напиши сообщение тёплому контакту с просьбой о рекомендации или тёплом знакомстве.\n"
            f"Вводные: {params}\n"
            "Структура: тёплое приветствие с общим воспоминанием → суть поиска одной фразой (позиция, ценность) → "
            "конкретная лёгкая просьба (рекомендация / знакомство с нужным человеком / 15 минут звонка) → благодарность без давления.\n"
            "Объём до 100 слов. Тон: на равных, без стыда и просьб «выручить»."
        )
    if tool == "bridge":
        return (
            "Ты — карьерный стратег. Кандидату нужен мост-доход на период поиска работы.\n"
            f"Резюме:\n{resume[:2500]}\n\n"
            "Предложи 5 идей мост-дохода именно по навыкам кандидата. Для каждой:\n"
            "• суть одной фразой;\n"
            "• где продавать (платформы, типы компаний, каналы);\n"
            "• как упаковать предложение в одну фразу;\n"
            "• реалистичный срок до первых денег.\n"
            "Идеи должны соответствовать уровню кандидата, а не быть случайными подработками."
        )
    extra = f"\nДополнительно о ситуации: {params}" if params else ""
    return (
        "Ты — поддерживающий коуч. Составь план на СЕГОДНЯ для кандидата в поиске работы.\n"
        f"Резюме:\n{resume[:1500]}{extra}\n\n"
        "Выдай:\n"
        "1) Три микро-шага на сегодня (2 целевых отклика + 1 контакт с человеком) с конкретикой из резюме;\n"
        "2) Один пункт заботы о себе и теле;\n"
        "3) Вечернюю фразу поддержки: что записать в дневник перед сном.\n"
        "Формат: простой чек-лист. Тон: тёплый, без токсичного позитива."
    )


async def crisis_generate(chat_id: int, user_id: int, tool: str, params: str):
    if not is_premium_user(user_id):
        await send_telegram(chat_id, "🔒 *Антикризисный пакет входит в Премиум.*\nБезлимит на 10 дней — 500 ⭐: кнопка «💎 Оплата и Баланс».")
        return
    resume = get_active_resume(user_id) or ""
    await show_typing(chat_id)
    prompt = build_crisis_prompt(tool, params, resume)
    res = await asyncio.to_thread(ai_generate, prompt)
    if not res or not validate_ai_response(res, min_length=50):
        await send_telegram(chat_id, "⚠️ ИИ недоступен. Попробуй ещё раз через минуту.")
        return
    await send_telegram(chat_id, f"{CRISIS_TITLES.get(tool, '🆘 *Результат:*')}\n\n{res}")


async def handle_document(chat_id: int, user_id: int, document: dict, is_admin: bool):
    file_size = document.get("file_size", 0)
    if file_size > MAX_FILE_SIZE:
        await send_telegram(chat_id,
            f"⚠️ *Файл слишком большой!*\n"
            f"Размер: {file_size / 1024 / 1024:.1f} МБ\n"
            f"Максимальный размер: {MAX_FILE_SIZE / 1024 / 1024:.0f} МБ")
        return
    file_id = document["file_id"]
    file_name = document.get("file_name", "resume.pdf")
    await show_typing(chat_id)
    try:
        async with HTTP.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}) as resp:
            file_info = await resp.json()
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            await send_telegram(chat_id, "⚠️ Не смог скачать файл.")
            return
        async with HTTP.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as f_resp:
            content = await f_resp.read()
    except Exception as e:
        log.error("download failed: %s", e)
        track_error()
        return
    path = f"tmp_{user_id}_{file_name}"
    with open(path, "wb") as f:
        f.write(content)
    fn_lower = file_name.lower()
    text_content = ""
    if fn_lower.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
        await send_telegram(chat_id, "🖼 *Распознаю текст с изображения...*")
        text_content = await extract_text_from_image(path)
    else:
        text_content = await asyncio.to_thread(extract_text, path, file_name)
    if os.path.exists(path):
        os.remove(path)
    if not text_content or not text_content.strip():
        await send_telegram(chat_id, "⚠️ Не удалось извлечь текст из файла.")
        return
    if user_states.get(user_id) == "waiting_for_hr_resume":
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_candidate_resume"] = text_content
        user_states[user_id] = "waiting_for_hr_vacancy_text"
        await send_telegram(chat_id, "✅ *Резюме кандидата получено!*\nТеперь пришлите текст вакансии.")
        return
    if user_states.get(user_id) == "waiting_for_hr_resume_scoring":
        user_states.pop(user_id, None)
        hr_sub_action = user_search_cache.get(user_id, {}).get("hr_sub_action", "scoring")
        if hr_sub_action == "scoring":
            await show_typing(chat_id)
            prompt = (
                "Ты — опытный рекрутер. Проведи скоринг кандидата.\n"
                f"--- РЕЗЮМЕ ---\n{text_content[:4000]}\n"
                "🟢 СИЛЬНЫЕ СТОРОНЫ\n🔴 КРАСНЫЕ ФЛАГИ\n🟡 НА ЧТО ОБРАТИТЬ ВНИМАНИЕ\n⭐ ОБЩАЯ ОЦЕНКА [1-10]\n📋 РЕКОМЕНДАЦИЯ"
            )
            scoring = await asyncio.to_thread(ai_generate, prompt)
            if scoring and validate_ai_response(scoring, min_length=50):
                await send_telegram(chat_id, f"🎯 *Скоринг кандидата:*\n\n{scoring}")
        elif hr_sub_action == "questions":
            bg(hr_generate_interview_questions(chat_id, user_id, text_content))
        elif hr_sub_action == "test_task":
            bg(hr_generate_test_task(chat_id, user_id, text_content))
        return
    add_resume(user_id, file_name, text_content)
    user_mode = get_user_mode(user_id)
    if user_mode == "recruiter":
        keyboard = get_recruiter_keyboard(is_admin)
        success_text = f"✅ *Резюме «{file_name}» загружено!*\nИспользуйте меню ниже."
    else:
        keyboard = get_job_seeker_keyboard(is_admin)
        success_text = (
            f"✅ *Резюме «{file_name}» загружено!*\n"
            "💡 *Что можно сделать:*\n"
            "1️⃣ * Разобрать вакансию* — скопируй текст вакансии.\n"
            "2️⃣ *️ Найти ЛПР* — прямой выход на менеджера.\n"
            "3️⃣ * Анализ навыков* — выявит пробелы.\n"
            "📎 *Форматы:* PDF, DOCX, DOC, ODT, RTF, TXT и фото резюме."
        )
    await send_telegram(chat_id, success_text, keyboard)


async def activate_resume(chat_id: int, user_id: int, rid: str):
    try:
        rid = int(rid)
    except ValueError:
        return
    cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (user_id,))
    cur.execute("UPDATE resumes SET active=1 WHERE id=? AND user_id=?", (rid, user_id))
    conn.commit()
    await send_telegram(chat_id, "✅ Резюме активировано.")


async def start_interview_simulator(chat_id: int, user_id: int):
    if not spend_balance(user_id, cost=1):
        await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
        return
    resume = get_active_resume(user_id)
    if not resume:
        await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        return
    await show_typing(chat_id)
    prompt = f"Задай первый каверзный вопрос на собеседовании по резюме:\n{resume[:5000]}"
    first_q = await asyncio.to_thread(ai_generate, prompt)
    if not first_q or not validate_ai_response(first_q, min_length=20):
        await send_telegram(chat_id, "⚠️ ИИ недоступен.")
        return
    interview_sessions[user_id] = {"question_count": 1, "history": []}
    user_states[user_id] = "interview_active"
    await send_telegram(chat_id, f"🎙 *Вопрос 1 из 3:*\n\n{first_q}")


async def handle_interview_answer(chat_id: int, user_id: int, answer_text: str):
    session = interview_sessions.get(user_id)
    if not session:
        user_states.pop(user_id, None)
        return
    q_count = session["question_count"]
    await show_typing(chat_id)
    prompt = (
        f"Ответ кандидата: {answer_text}\n"
        f"Дай фидбек и задай следующий вопрос (номер {q_count + 1} из 3). "
        f"Если это был 3-й вопрос — подведи итог."
    )
    feedback = await asyncio.to_thread(ai_generate, prompt)
    if not feedback or not validate_ai_response(feedback, min_length=20):
        await send_telegram(chat_id, "⚠️ ИИ вернул некорректный ответ.")
        return
    if q_count >= 3:
        user_states.pop(user_id, None)
        interview_sessions.pop(user_id, None)
        await send_telegram(chat_id, f"🏁 *Итоги тренировки:*\n\n{feedback}")
    else:
        session["question_count"] += 1
        await send_telegram(chat_id, f"💡 *Разбор и следующий вопрос:*\n\n{feedback}")


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

    track_user_activity(user_id)

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

    if not is_admin and not check_rate_limit(user_id):
        await send_telegram(chat_id, "⚠️ *Слишком много запросов.*\nПодождите немного.")
        return

    if is_admin and text.startswith("/reply"):
        parts = text.split(maxsplit=2)
        if len(parts) >= 3:
            try:
                target_uid = int(parts[1])
                reply_text = parts[2]
                await send_telegram(target_uid, f"💬 *От администратора:*\n\n{reply_text}")
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

    if user_states.get(user_id) == "waiting_for_hr_resume":
        user_states.pop(user_id, None)
        if len(text) < 100:
            await send_telegram(chat_id, "⚠️ Текст резюме слишком короткий.")
            user_states[user_id] = "waiting_for_hr_resume"
            return
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_candidate_resume"] = text
        user_states[user_id] = "waiting_for_hr_vacancy_text"
        await send_telegram(chat_id, "✅ *Резюме кандидата получено!*\nТеперь пришлите текст вакансии.")
        return

    if user_states.get(user_id) == "waiting_for_vacancy_text":
        user_states.pop(user_id, None)
        if len(text) < 100:
            await send_telegram(chat_id, "⚠️ Текст слишком короткий.")
            user_states[user_id] = "waiting_for_vacancy_text"
            return
        bg(analyze_vacancy_text(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_hr_vacancy_text":
        user_states.pop(user_id, None)
        if len(text) < 100:
            await send_telegram(chat_id, "⚠️ Текст вакансии слишком короткий.")
            user_states[user_id] = "waiting_for_hr_vacancy_text"
            return
        cached = user_search_cache.get(user_id, {})
        candidate_resume = cached.get("hr_candidate_resume", "")
        if not candidate_resume:
            await send_telegram(chat_id, "⚠️ Не нашёл резюме кандидата.")
            return
        bg(hr_analyze_candidate_match(chat_id, user_id, candidate_resume, text))
        return

    if user_states.get(user_id) == "waiting_for_hr_resume_scoring":
        user_states.pop(user_id, None)
        if len(text) < 100:
            await send_telegram(chat_id, "⚠️ Текст резюме слишком короткий.")
            user_states[user_id] = "waiting_for_hr_resume_scoring"
            return
        hr_sub_action = user_search_cache.get(user_id, {}).get("hr_sub_action", "scoring")
        if hr_sub_action == "scoring":
            await show_typing(chat_id)
            prompt = (
                "Ты — опытный рекрутер. Проведи скоринг кандидата.\n"
                f"--- РЕЗЮМЕ ---\n{text[:4000]}\n"
                "🟢 СИЛЬНЫЕ СТОРОНЫ\n🔴 КРАСНЫЕ ФЛАГИ\n🟡 НА ЧТО ОБРАТИТЬ ВНИМАНИЕ\n⭐ ОБЩАЯ ОЦЕНКА [1-10]\n📋 РЕКОМЕНДАЦИЯ"
            )
            scoring = await asyncio.to_thread(ai_generate, prompt)
            if scoring and validate_ai_response(scoring, min_length=50):
                await send_telegram(chat_id, f"🎯 *Скоринг кандидата:*\n\n{scoring}")
        elif hr_sub_action == "questions":
            bg(hr_generate_interview_questions(chat_id, user_id, text))
        elif hr_sub_action == "test_task":
            bg(hr_generate_test_task(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_hr_params":
        user_states.pop(user_id, None)
        hr_action = user_search_cache.get(user_id, {}).get("hr_action", "")
        if hr_action == "rejection":
            bg(hr_generate_rejection_letter(chat_id, user_id, text))
        elif hr_action == "offer":
            bg(hr_generate_offer_letter(chat_id, user_id, text))
        elif hr_action == "salary":
            bg(hr_estimate_salary(chat_id, user_id, text))
        elif hr_action == "pitch":
            bg(hr_candidate_pitch(chat_id, user_id, text))
        elif hr_action == "followup":
            bg(hr_followup_after_interview(chat_id, user_id, text))
        elif hr_action == "vacancy_description":
            bg(hr_generate_vacancy_description(chat_id, user_id, text))
        return

    if user_states.get(user_id) == "waiting_for_crisis_input":
        user_states.pop(user_id, None)
        tool = user_search_cache.get(user_id, {}).get("crisis_tool", "")
        bg(crisis_generate(chat_id, user_id, tool, text))
        return

    if user_states.get(user_id) == "waiting_for_company_correction":
        user_states.pop(user_id, None)
        cached = user_search_cache.get(user_id, {})
        if "," in text:
            parts = text.split(",", 1)
            new_company = parts[0].strip()
            new_title = parts[1].strip()
        else:
            new_company = text.strip()
            new_title = cached.get("vacancy_title", "") or "Специалист"
        contact_name = cached.get("contact_name", "")
        resume = cached.get("resume") or get_active_resume(user_id) or "Резюме не указано"
        await send_telegram(chat_id, f"🏢 *Компания:* `{new_company}`\n💼 *Позиция:* `{new_title}`")
        aggressive_results = await aggressive_recruiter_search(chat_id, new_company, new_title, contact_name)
        final_report = f"🏢 *Компания:* {new_company}\n💼 *Позиция:* {new_title}\n"
        final_report += f"\n🔎 *Результаты поиска:*\n{aggressive_results['search_log']}\n"
        if aggressive_results["found"]:
            final_report += "\n✅ *Найденные контакты:*\n"
            for i, c in enumerate(aggressive_results["contacts"][:3], 1):
                final_report += f"{i}. *{c.get('name', 'Имя')}*\n"
                if c.get("email"): final_report += f"   📧 {c['email']}\n"
                if c.get("phone"): final_report += f"   📱 {c['phone']}\n"
                if c.get("url"): final_report += f"   🔗 {c['url']}\n"
                final_report += "\n"
        pitch = await asyncio.to_thread(ai_generate,
            f"Напиши короткий питч (4-5 строк) для рекрутера компании '{new_company}' на позицию '{new_title}'.\n"
            f"Резюме: {resume[:1500]}")
        if pitch and validate_ai_response(pitch, min_length=50):
            pitch = clean_pitch_text(pitch, new_title)
            final_report += f"\n📝 *Питч:*\n\n{pitch}"
        encoded = urllib.parse.quote(new_company)
        inline_kb = [
            [{"text": "🔍 LinkedIn", "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded}%22+HR"}],
            [{"text": "🌐 Карьерный сайт", "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded}%22+контакты"}]
        ]
        cur.execute("INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Разобрана')",
                    (user_id, f"manual_{int(datetime.datetime.now().timestamp())}", f"{new_title} ({new_company})"))
        conn.commit()
        await send_telegram(chat_id, final_report, {"inline_keyboard": inline_kb})
        return

    if user_states.get(user_id) == "waiting_for_osint_target":
        user_states.pop(user_id, None)
        if extract_hh_vacancy_id(text):
            await send_telegram(chat_id, "📎 Вижу ссылку. Пришли текст вакансии.")
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
            await send_telegram(chat_id, "📎 Вижу ссылку. Пришли текст вакансии.")
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
        await send_telegram(chat_id, "✅ Сообщение отправлено.")
        await send_telegram(ADMIN_ID, f"📩 *Отзыв!*\nОт: @{username or 'нет'} (ID: `{user_id}`)\n\n{text}")
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
            await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
            return
        bg(run_resume_adaptation(chat_id, user_id, rid, text))
        return

    if extract_hh_vacancy_id(text):
        await send_telegram(chat_id,
            "📎 *Вижу ссылку на вакансию!*\n"
            "⚠️ hh.ru блокирует автоматический доступ.\n"
            "💡 Скопируй полный текст вакансии и пришли сюда.")
        return

    if is_vacancy_text(text):
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(analyze_vacancy_text(chat_id, user_id, text))
        return

    if text.startswith("/start") or text == "🚀 Запустить бота":
        if is_admin:
            welcome_text = "👋 Привет, Антон! Админ-режим активирован."
        else:
            welcome_text = (
                "👋 Привет! Я — твой ИИ-карьерный агент (Версия 4.3).\n"
                "🎯 *Выбери свой режим:*\n"
                "💼 *Я ищу работу* — для соискателей.\n"
                "🏢 *Я нанимаю* — для рекрутеров.\n"
                "🎁 *Баланс:* `7 запросов` бесплатно!\n"
                "📎 *Форматы файлов:* PDF, DOCX, DOC, ODT, RTF, TXT и фото резюме.\n"
                "📏 *Максимальный размер файла:* 5 МБ.\n"
                "🛡️ *Защита:* Не более 15 запросов в минуту."
            )
        await send_telegram(chat_id, welcome_text, get_main_keyboard(is_admin))

    elif text == "💼 Я ищу работу":
        set_user_mode(user_id, "seeker")
        seeker_text = (
            "💼 *Режим соискателя активирован!*\n"
            "🚀 *Как это работает:*\n"
            "1️⃣ Отправь файл резюме (до 5 МБ).\n"
            "2️⃣ Скопируй текст вакансии из приложения hh.\n"
            "3️⃣ Пришли текст сюда — я найду контакты и напишу питч!\n"
            "Используй меню ниже 👇"
        )
        await send_telegram(chat_id, seeker_text, get_job_seeker_keyboard(is_admin))

    elif text == "🏢 Я нанимаю":
        set_user_mode(user_id, "recruiter")
        recruiter_text = (
            "🏢 *Режим рекрутера активирован!*\n"
            "📋 *Доступные инструменты:*\n"
            "• 📊 Соответствие резюме вакансии.\n"
            "• 🎯 Скоринг кандидата.\n"
            "• ❓ Вопросы для интервью.\n"
            "• 📝 Тестовое задание.\n"
            "• 📋 Генератор описания вакансии.\n"
            "• 📝 Вежливый отказ.\n"
            "• 📄 Шаблон оффера.\n"
            "• 💰 Оценка зарплаты.\n"
            "• 💬 Питч кандидату.\n"
            "• 📅 Фоллоу-ап.\n"
            "Используй меню ниже 👇"
        )
        await send_telegram(chat_id, recruiter_text, get_recruiter_keyboard(is_admin))

    elif text == "🏠 Главное меню":
        await send_telegram(chat_id, "🏠 *Главное меню*", get_main_keyboard(is_admin))

    elif text == "📄 Моё резюме":
        await send_telegram(chat_id, "📄 *Работа с резюме*", get_seeker_resume_keyboard())

    elif text == "🎤 Собеседование":
        await send_telegram(chat_id, "🎤 *Подготовка к собеседованию*", get_seeker_interview_keyboard())

    elif text == "📊 Трекер и статистика":
        await send_telegram(chat_id, "📊 *Трекер и статистика*", get_seeker_tracker_keyboard())

    elif text == "🎓 Премиум":
        await send_telegram(chat_id, "🎓 *Премиум функции*", get_seeker_premium_keyboard())

    elif text == "⬅️ Назад к меню соискателя":
        await send_telegram(chat_id, "💼 *Меню соискателя*", get_job_seeker_keyboard(is_admin))

    elif text == "🌐 Вакансии из Сетки":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_setka_post"
        await send_telegram(chat_id, "🌐 *Вакансии из Сетки*\nСкопируйте текст поста и отправьте сюда.")

    elif text == "🔗 Разобрать вакансию":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_vacancy_text"
        await send_telegram(chat_id,
            "🔗 *Разбор вакансии*\n"
            "1️⃣ Открой вакансию в приложении hh.\n"
            "2️⃣ Скопируй полный текст.\n"
            "3️⃣ Вставь сюда следующим сообщением.")

    elif text == "🕵️ Найти ЛПР":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_osint_target"
        await send_telegram(chat_id,
            "🕵️ *Прямой выход на ЛПР*\n"
            "Напиши: `Компания, должность` (например: `Сбер, Product Manager`).")

    elif text == "📝 Короткие Питчи":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        user_states[user_id] = "waiting_for_pitch_target"
        await send_telegram(chat_id,
            "📝 *Генерация питча*\n"
            "Напиши: `Компания, должность` (например: `Яндекс, Data Scientist`).")

    elif text == "📊 Анализ навыков (Skill Gap)":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(run_skill_gap_analysis(chat_id, user_id))

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

    elif text == "🎤 Тренажер собеседований":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
            return
        bg(start_interview_simulator(chat_id, user_id))

    elif text == "🎓 Курсы":
        bg(show_courses(chat_id, user_id))

    elif text == "📝 Шаблоны писем":
        bg(show_cover_letter_templates(chat_id, user_id))

    elif text == "📊 Аналитика":
        bg(show_analytics(chat_id, user_id))

    elif text == "🎯 План поиска":
        bg(generate_job_search_plan(chat_id, user_id))

    elif text == "🆘 Антикризисный пакет":
        if not is_premium_user(user_id):
            await send_telegram(chat_id,
                "🔒 *Антикризисный пакет входит в Премиум.*\n"
                "Внутри: курс «Поиск работы в кризис» (6 уроков) и 7 персональных генераторов:\n"
                "🕳 объяснение пробела • 📮 фоллоу-ап после тишины • 🛡 проверка на скам • "
                "💰 минимум оффера • 🤝 письмо тёплому контакту • 🌉 мост-доход • 📅 план на сегодня.\n"
                "💳 Безлимит на 10 дней — 500 ⭐: кнопка «💎 Оплата и Баланс».")
            return
        kb = {"inline_keyboard": [
            [{"text": "🕳 Объяснить пробел", "callback_data": "crisis_gap"},
             {"text": "📮 Фоллоу-ап после тишины", "callback_data": "crisis_followup"}],
            [{"text": "🛡 Проверить вакансию", "callback_data": "crisis_scam"},
             {"text": "💰 Мой минимум оффера", "callback_data": "crisis_min"}],
            [{"text": "🤝 Письмо тёплому контакту", "callback_data": "crisis_warm"}],
            [{"text": "🌉 Мост-доход по навыкам", "callback_data": "crisis_bridge"},
             {"text": "📅 План на сегодня", "callback_data": "crisis_day"}],
            [{"text": "🎓 Курс «Поиск работы в кризис»", "callback_data": "course_crisis"}]
        ]}
        await send_telegram(chat_id, "🆘 *Антикризисный пакет*\nВыбери инструмент:", kb)

    elif text == "📌 Трекер откликов":
        cur.execute("SELECT vacancy_id, title, status FROM liked_vacancies WHERE user_id=? ORDER BY id DESC LIMIT 15", (user_id,))
        rows = cur.fetchall()
        if not rows:
            await send_telegram(chat_id, "📌 Трекер пуст.")
        else:
            tracker_msg = "📌 *Ваш трекер откликов:*\n\n"
            for r in rows:
                v_url = f"https://hh.ru/vacancy/{r[0]}" if not (str(r[0]).startswith("setka_") or str(r[0]).startswith("text_") or str(r[0]).startswith("manual_") or str(r[0]).startswith("hr_") or str(r[0]).startswith("mini_")) else "#"
                tracker_msg += f"• [{r[1]}]({v_url})\nСтатус: `{r[2]}`\n\n"
            await send_telegram(chat_id, tracker_msg)

    elif text == "🔍 Поиск вакансий":
        if not get_active_resume(user_id):
            await send_telegram(chat_id, "💡 Сначала загрузите резюме!")
        else:
            bg(handle_search(chat_id, user_id, is_admin))

    elif text == "📥 Загрузить резюме":
        await send_telegram(chat_id,
            "📄 *Загрузка резюме*\n"
            "Отправьте файл резюме в следующем сообщении.\n"
            "📎 *Форматы:* PDF, DOCX, DOC, ODT, RTF, TXT.\n"
            "• Изображения: JPG, PNG, WEBP.\n"
            "📏 *Максимальный размер:* 5 МБ.")

    elif text == "📤 Экспорт резюме":
        bg(export_resume_docx(chat_id, user_id))

    elif text == "📊 Соответствие резюме вакансии":
        user_states[user_id] = "waiting_for_hr_resume"
        await send_telegram(chat_id,
            "📊 *Анализ соответствия*\n"
            "1️⃣ Пришлите файл резюме кандидата или вставьте текст.\n"
            "2️⃣ Затем пришлите текст вакансии.\n"
            "3️⃣ Я выдам процент соответствия и вердикт.")

    elif text == "🎯 Скоринг кандидата":
        user_states[user_id] = "waiting_for_hr_resume_scoring"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_sub_action"] = "scoring"
        await send_telegram(chat_id, "🎯 *Скоринг кандидата*\nПришлите текст резюме.")

    elif text == "❓ Вопросы для интервью":
        user_states[user_id] = "waiting_for_hr_resume_scoring"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_sub_action"] = "questions"
        await send_telegram(chat_id, "❓ *Вопросы для интервью*\nПришлите текст резюме.")

    elif text == "📝 Тестовое задание":
        user_states[user_id] = "waiting_for_hr_resume_scoring"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_sub_action"] = "test_task"
        await send_telegram(chat_id, "📝 *Тестовое задание*\nПришлите текст резюме.")

    elif text == "📋 Описание вакансии":
        user_states[user_id] = "waiting_for_hr_params"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_action"] = "vacancy_description"
        await send_telegram(chat_id, "📋 *Описание вакансии*\nОпишите вакансию в свободной форме.")

    elif text == "📝 Вежливый отказ":
        user_states[user_id] = "waiting_for_hr_params"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_action"] = "rejection"
        await send_telegram(chat_id, "📝 *Вежливый отказ*\nОпишите ситуацию.")

    elif text == "📄 Шаблон оффера":
        user_states[user_id] = "waiting_for_hr_params"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_action"] = "offer"
        await send_telegram(chat_id, "📄 *Шаблон оффера*\nОпишите параметры.")

    elif text == "💰 Оценка зарплаты":
        user_states[user_id] = "waiting_for_hr_params"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_action"] = "salary"
        await send_telegram(chat_id, "💰 *Оценка зарплаты*\nОпишите параметры вакансии.")

    elif text == "💬 Питч кандидату":
        user_states[user_id] = "waiting_for_hr_params"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_action"] = "pitch"
        await send_telegram(chat_id, "💬 *Питч кандидату*\nОпишите вакансию.")

    elif text == "📅 Фоллоу-ап после интервью":
        user_states[user_id] = "waiting_for_hr_params"
        user_search_cache[user_id] = user_search_cache.get(user_id, {})
        user_search_cache[user_id]["hr_action"] = "followup"
        await send_telegram(chat_id, "📅 *Фоллоу-ап*\nОпишите ситуацию.")

    elif text in ("👥 Пригласить друга", "🎁 Бонусы (Репост & Друзья)"):
        bot_info = await HTTP.get(f"{TELEGRAM_API}/getMe")
        bot_data = await bot_info.json()
        bot_username = bot_data.get("result", {}).get("username", "bot")
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        bonus_text = (
            "🎁 *Программа лояльности*\n"
            "👥 *Пригласить друга (+7 запросов)*\n"
            f"Ссылка:\n`{ref_link}`\n"
            "📢 *Поделиться в соцсетях (+20 запросов)*"
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
            feedbacks_msg += f"🆔 `{r[1]}` (@{r[2] or 'нет'})\n💬 {r[3]}\n⏱ `{r[4]}`\n\n"
        await send_telegram(chat_id, feedbacks_msg)

    elif text == "💎 Оплата и Баланс":
        if is_admin:
            status_str = "📊 Баланс: `∞ Безлимит`"
        else:
            data = get_user_data(user_id)
            status_str = f"📊 Баланс: `{data['balance']} запросов`"
            if data["unlimited_until"]:
                status_str += f"\n⭐ Безлимит до: `{data['unlimited_until']}`"
        balance_text = (
            f"💎 *Оплата и Баланс*\n{status_str}\n"
            "💳 *Тарифы:*\n"
            "1️⃣ Пакет «50 запросов»: 100 ⭐ ИЛИ 200 руб.\n"
            "2️⃣ Безлимит на 10 дней: 500 ⭐ ИЛИ 500 руб. (включает Премиум: курсы, шаблоны, 🆘 антикризисный пакет).\n"
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
            await send_telegram(chat_id, "👑 У тебя уже безлимит.")
            return
        data = get_user_data(user_id)
        status_str = f"📊 Баланс: `{data['balance']} запросов`"
        if data["unlimited_until"]:
            status_str += f"\n⭐ Безлимит до: `{data['unlimited_until']}`"
        extend_text = (
            f"⏰ *Продление доступа*\n{status_str}\n"
            "💳 *Выберите вариант:*\n"
            "⭐ 50 запросов — 100 ⭐\n"
            "⭐ Безлимит 10 дней — 500 ⭐ (включает Премиум и 🆘 пакет)"
        )
        kb = {"inline_keyboard": [
            [{"text": "⭐ 50 запросов (100 Звезд)", "callback_data": "buy_pack_stars"}],
            [{"text": "⭐ Безлимит 10 дней (500 Звезд)", "callback_data": "buy_unl_stars"}],
            [{"text": "📄 Отправить чек", "callback_data": "send_receipt"}]
        ]}
        await send_telegram(chat_id, extend_text, kb)

    elif text == "📁 Мои резюме":
        rows = list_resumes(user_id)
        if not rows:
            await send_telegram(chat_id, "💡 Нет резюме.")
        else:
            kb = {"inline_keyboard": [[{"text": f"{'✅ ' if r['active'] else '📄 '}{r['name']}",
                                        "callback_data": f"act_{r['id']}"}] for r in rows]}
            await send_telegram(chat_id, "📁 *Ваши резюме:*", kb)

    elif text == "ℹ️ Помощь":
        help_text = (
            "ℹ️ *Справка (Версия 4.3):*\n"
            "🎯 *Два режима:*\n"
            "💼 *Я ищу работу* — для соискателей.\n"
            "🏢 *Я нанимаю* — для рекрутеров.\n"
            "📎 *Форматы:* PDF, DOCX, DOC, ODT, RTF, TXT и фото.\n"
            "📏 *Максимальный размер:* 5 МБ.\n"
            "🛡️ *Защита:* 15 запросов в минуту.\n"
            "📧 *Поддержка:* a.lemus@ya.ru"
        )
        await send_telegram(chat_id, help_text, get_main_keyboard(is_admin))

    elif text in ("👑 Админ-панель", "/admin"):
        if not is_admin:
            return
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM resumes")
        total_resumes = cur.fetchone()[0]
        await send_telegram(chat_id, f"👑 *Админ-панель*\n👥 Пользователей: `{total_users}`\n📁 Резюме: `{total_resumes}`")

    else:
        await send_telegram(chat_id, "ℹ️ Воспользуйтесь меню ниже.", get_main_keyboard(is_admin))


# ============================================================
# 🔍 ЯДРО ПОИСКА ВАКАНСИЙ v3
# ============================================================

SENIORITY_WORDS = ["руководитель", "директор", "head", "chief", "lead", "начальник",
                   "управляющий", "commercial", "коммерческий", "cco", "c-level", "vp"]
IC_SENIOR_WORDS = ["ведущий", "главный", "senior", "эксперт"]
JUNIOR_TITLE_MARKERS = ["стажер", "стажёр", "trainee", "intern", "junior", "джуниор",
                        "студент", "начинающий", "без опыта", "практикант", "стажировка"]
INTERN_TITLE_MARKERS = ["стажер", "стажёр", "trainee", "intern", "студент",
                        "практикант", "стажировка", "без опыта"]
MANAGEMENT_TITLE_MARKERS = SENIORITY_WORDS

TOP_COMPANIES_RU = [
    "сбер", "яндекс", "мтс", "мегафон", "ростелеком", "билайн", "втб", "т-банк", "тинькофф",
    "альфа-банк", "газпромбанк", "совкомбанк", "райффайзен", "росбанк", "дом.рф", "псб",
    "ozon", "озон", "wildberries", "вайлдберриз", "avito", "авито", "vk", "x5", "магнит",
    "лента", "мвидео", "dns", "касперский", "kaspersky", "1с", "сбертех", "мтс digital",
    "мегафон технологии", "ozon tech", "yandex", "газпром", "роснефть", "лукойл", "сибур",
    "северсталь", "нлмк", "росатом", "ростех", "ржд", "аэрофлот", "почта россии",
    "шереметьево", "транснефть", "россети", "интер рао", "норникель", "фосагро",
    "леруа мерлен", "спортмастер", "эльдорадо", "хоум банк",
]

INDUSTRY_COMPANY_HINTS = {
    "телеком": ["мегафон", "мтс", "билайн", "ростелеком", "tele2", "т2", "вымпелком", "дом.ru", "эртелеком"],
    "связь": ["мегафон", "мтс", "билайн", "ростелеком", "tele2", "т2", "вымпелком"],
    "банк": ["сбер", "втб", "альфа", "газпромбанк", "т-банк", "тинькофф", "райффайзен", "совкомбанк",
             "мкб", "псб", "отп", "росбанк", "дом.рф", "банк"],
    "финтех": ["тинькофф", "т-банк", "юмани", "cloudpayments", "fintech"],
    "it": ["яндекс", "vk", "озон", "avito", "kaspersky", "1с", "сбертех", "wildberries"],
    "итей": ["яндекс", "vk", "озон", "avito", "kaspersky", "1с"],
    "ретейл": ["магнит", "x5", "пятёрочка", "перекрёсток", "лента", "wildberries", "озон"],
    "фарма": ["фарм", "биотех", "медицин", "клиник"],
    "медиа": ["медиа", "тв", "радио", "пресс", "издатель"],
    "строитель": ["строй", "девелоп", "инжинир", "проектн"],
    "нефтегаз": ["нефт", "газ", "лукойл", "роснефт", "газпром", "труб"],
    "транспорт": ["логистик", "транспорт", "жд", "ржд", "аэро", "авто"],
    "производ": ["завод", "производ", "промышл", "металл", "машин"],
    "образован": ["школ", "универ", "институт", "образован"],
}


def detect_profile(resume_text: str) -> dict:
    text = (resume_text or "").lower()
    years = 0
    m = re.search(r'опыт работы[^\d]{0,30}(\d{1,2})\s*(лет|года)', text)
    if m:
        years = int(m.group(1))
    else:
        m2 = re.search(r'(\d{1,2})\s+лет\s+опыта', text)
        if m2:
            years = int(m2.group(1))
        else:
            dates = [int(d) for d in re.findall(r'\b(19[89]\d|20[0-4]\d)\b', text)]
            if dates:
                years = max(0, min(40, max(dates) - min(dates)))
    has_management = any(w in text for w in SENIORITY_WORDS) or \
        "руководил" in text or "командой" in text or "подчиненными" in text or "в подчинении" in text
    if years >= 7 or (has_management and years >= 5):
        seniority = "senior"
    elif years >= 3:
        seniority = "middle"
    else:
        seniority = "junior" if years else "middle"
    return {
        "years": years,
        "has_management": has_management,
        "seniority": seniority,
    }


def company_boost(company: str, industries: list, employers: list) -> int:
    cl = (company or "").lower()
    if not cl:
        return 0
    boost = 0
    for e in (employers or []):
        e = (e or "").lower().strip()
        if not e or len(e) < 3:
            continue
        tokens = [w for w in re.split(r'[\s\.,«»"\']+', e) if len(w) > 3]
        if e in cl or any(tok in cl for tok in tokens):
            boost = max(boost, 15)
            break
    if boost == 0:
        targeted = set()
        for ind in (industries or []):
            key = ind.strip().lower()
            if not key:
                continue
            for dict_key, tokens in INDUSTRY_COMPANY_HINTS.items():
                if dict_key in key or key in dict_key:
                    targeted.update(tokens)
        if targeted:
            for t in targeted:
                if t in cl:
                    boost = max(boost, 10)
                    break
    if boost == 0:
        for tokens in INDUSTRY_COMPANY_HINTS.values():
            hit = False
            for t in tokens:
                if t in cl:
                    boost = max(boost, 5)
                    hit = True
                    break
            if hit:
                break
    for tc in TOP_COMPANIES_RU:
        if tc in cl:
            boost += 12
            break
    return min(boost, 27)


def title_level_ok(title: str, profile: dict) -> bool:
    t = (title or "").lower()
    sen = profile["seniority"]
    if sen == "senior":
        if any(m in t for m in JUNIOR_TITLE_MARKERS):
            return False
    elif sen == "middle":
        if any(m in t for m in INTERN_TITLE_MARKERS):
            return False
    else:
        if any(m in t for m in MANAGEMENT_TITLE_MARKERS):
            return False
    return True


def keyword_fallback_score(title: str, company: str, keywords: list,
                           industries: list, employers: list, profile: dict) -> tuple:
    t = (title or "").lower()
    s = 45
    hits = [k for k in (keywords or []) if k and len(k) > 2 and k.lower() in t]
    s += min(30, 6 * len(hits))
    s += company_boost(company, industries, employers)
    if profile["has_management"]:
        if any(w in t for w in MANAGEMENT_TITLE_MARKERS):
            s += 12
    elif profile["seniority"] == "senior":
        if any(w in t for w in IC_SENIOR_WORDS + MANAGEMENT_TITLE_MARKERS):
            s += 8
    if profile["seniority"] == "junior":
        if any(w in t for w in ["junior", "стажер", "стажёр", "начинающий", "ассистент", "помощник", "trainee"]):
            s += 10
    reason = ("ключевые слова: " + ", ".join(hits[:3])) if hits else "совпадение профиля"
    return min(98, max(10, s)), reason


async def core_search_vacancies(user_id: int) -> dict:
    resume = get_active_resume(user_id)
    if not resume:
        return {"error": "Сначала загрузите резюме (в приложении или в боте)",
                "vacancies": [], "queries": [], "industries": []}
    profile = detect_profile(resume)
    region_code = await extract_region_from_resume(resume)
    log.info(f"🎯 Search profile: seniority={profile['seniority']}, mgmt={profile['has_management']}, "
             f"years={profile['years']}, region={region_code}")

    plan_prompt = (
        "Ты — карьерный аналитик. По резюме составь план поиска вакансий на hh.ru.\n"
        f"Резюме:\n{resume[:4000]}\n\n"
        f"Уровень кандидата: {profile['seniority']}. "
        f"Трек: {'управленческий' if profile['has_management'] else 'специалист/эксперт'}.\n"
        "Верни ТОЛЬКО JSON вида:\n"
        "{\"queries\": [\"должность 1\", \"... 5 должностей\"], "
        "\"industries\": [\"отрасль 1\", \"отрасль 2\"], "
        "\"keywords\": [\"навык 1\", \"... до 10\"], "
        "\"employers\": [\"компания из опыта 1\", \"...\"], "
        "\"target_companies\": [\"желаемый работодатель 1\", \"... до 8\"]}\n"
        "Правила: должности строго уровня и трека кандидата (не ниже и не выше); "
        "отрасли — где кандидат работал и куда целится; keywords — ключевые навыки и домены; "
        "employers — компании из блоков опыта; target_companies — компании из целевого вектора кандидата "
        "(если указан) плюс крупнейшие игроки его отраслей."
    )
    plan_raw = await asyncio.to_thread(ai_generate, plan_prompt)
    queries, industries, keywords, employers, target_companies = [], [], [], [], []
    if plan_raw:
        clean = plan_raw.replace("```json", "").replace("```", "").strip()
        m = re.search(r'\{.*\}', clean, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
                queries = [str(q).strip() for q in (parsed.get("queries") or []) if str(q).strip()][:5]
                industries = [str(i).strip() for i in (parsed.get("industries") or []) if str(i).strip()][:4]
                keywords = [str(k).strip() for k in (parsed.get("keywords") or []) if str(k).strip()][:10]
                employers = [str(e).strip() for e in (parsed.get("employers") or []) if str(e).strip()][:8]
                target_companies = [str(t).strip() for t in (parsed.get("target_companies") or []) if str(t).strip()][:8]
            except Exception as e:
                log.warning(f"Search plan parse error: {e}")
    if not queries:
        if profile["has_management"]:
            queries = ["Руководитель направления", "Директор по развитию", "Коммерческий директор",
                       "Руководитель проектов", "Head of business development"]
        elif profile["seniority"] == "senior":
            queries = ["Ведущий специалист", "Главный эксперт", "Senior manager", "Ведущий менеджер", "Эксперт"]
        else:
            queries = ["Специалист", "Менеджер", "Ассистент", "Начинающий специалист", "Стажер"]

    adjusted = []
    for q in queries:
        ql = q.lower()
        if profile["has_management"]:
            if not any(w in ql for w in MANAGEMENT_TITLE_MARKERS):
                q = f"Руководитель {q}"
        elif profile["seniority"] == "senior":
            if not any(w in ql for w in IC_SENIOR_WORDS + MANAGEMENT_TITLE_MARKERS):
                q = f"Ведущий {q}"
        adjusted.append(q)
    queries = adjusted[:5]

    all_items = []
    for idx, q in enumerate(queries):
        if idx > 0:
            await asyncio.sleep(1)
        res = await hh_api_search(q, region_code) or await hh_scrape_search(q, region_code)
        if res:
            all_items.extend(res)
    senior_word = "руководитель" if profile["has_management"] else (queries[0] if queries else "специалист")
    for comp in target_companies[:6]:
        await asyncio.sleep(1)
        tq = f"{comp} {senior_word}"
        res = await hh_api_search(tq, region_code) or await hh_scrape_search(tq, region_code)
        if res:
            all_items.extend(res)
    log.info(f"📊 Raw collected: {len(all_items)}")
    if not all_items:
        return {"error": "", "vacancies": [], "queries": queries, "industries": industries}

    unique = {}
    for v in all_items:
        vid = str(v.get("id"))
        if not vid or vid in unique:
            continue
        if not title_level_ok(v.get("name"), profile):
            continue
        if is_vacancy_hidden(user_id, vid):
            continue
        unique[vid] = v
    filtered = list(unique.values())[:60]
    log.info(f"📊 Filtered: {len(filtered)} / {len(all_items)}")

    scored = []
    for i in range(0, len(filtered), 15):
        batch = filtered[i:i + 15]
        vacancies_text = "\n".join([f"ID {v['id']}: {v.get('name')} в {v.get('company')}" for v in batch])
        quick_prompt = (
            f"Оцени соответствие резюме кандидата каждой вакансии (0-100).\n"
            f"Уровень кандидата: {profile['seniority']}, трек: "
            f"{'управленческий' if profile['has_management'] else 'специалист'}.\n"
            "ВАЖНО: отрасли телеком, IT, SaaS, банки, финтех и экосистемы — взаимозаменяемые домены для "
            "коммерческих, продуктовых и управленческих ролей. НЕ снижай оценку сильно за другую отрасль, "
            "если совпадают уровень, трек и компетенции (P&L, B2B, команды, продукты, юнит-экономика).\n"
            f"Резюме:\n{resume[:2500]}\n\nВакансии:\n{vacancies_text}\n\n"
            "Верни ТОЛЬКО JSON: {\"ID\": {\"score\": 85, \"reason\": \"причина\"}}"
        )
        eval_res = await asyncio.to_thread(ai_generate, quick_prompt)
        parsed_batch = {}
        if eval_res:
            try:
                clean = eval_res.replace("```json", "").replace("```", "").strip()
                m = re.search(r'\{.*\}', clean, re.S)
                if m:
                    parsed_batch = json.loads(m.group(0))
            except Exception as e:
                log.warning(f"Batch JSON error: {e}")
        for v in batch:
            vid = str(v["id"])
            v_data = parsed_batch.get(vid) or {}
            boost = company_boost(v.get("company"), industries, employers)
            if v_data.get("score") is not None:
                try:
                    base = int(v_data["score"])
                except Exception:
                    base = 60
                reason = str(v_data.get("reason", "релевантный профиль"))
            else:
                base, reason = keyword_fallback_score(
                    v.get("name"), v.get("company"), keywords, industries, employers, profile)
            final_score = min(98, base + boost)
            if boost >= 12:
                reason += " • приоритет: топ-компания/целевой работодатель"
            scored.append({
                "id": vid,
                "title": v.get("name"),
                "company": v.get("company"),
                "salary": v.get("salary"),
                "url": v.get("url"),
                "match_score": final_score,
                "match_reason": reason,
            })
        await asyncio.sleep(1)
    scored.sort(key=lambda x: -x["match_score"])
    return {"error": "", "vacancies": scored[:30], "queries": queries, "industries": industries}


# ============================================================
# 🌙 ДАЙДЖЕСТ ВАКАНСИЙ
# ============================================================

async def digest_loop():
    await asyncio.sleep(300)
    log.info("🌙 Digest loop started")
    while True:
        try:
            cur.execute("SELECT user_id FROM users WHERE digest_active=1")
            rows = cur.fetchall()
            for (uid,) in rows[:30]:
                try:
                    if not get_active_resume(uid):
                        continue
                    res = await core_search_vacancies(uid)
                    vacs = res.get("vacancies") or []
                    if not vacs:
                        continue
                    msg = "🌅 *Дайджест вакансий для вас:*\n\n"
                    for v in vacs[:5]:
                        msg += f"• *{v['match_score']}%* — [{v['title']}]({v['url']})\n  {v['company']}\n\n"
                    msg += "_Отключить дайджест: экран Баланс в приложении._"
                    kb = None
                    if MINI_APP_URL:
                        kb = {"inline_keyboard": [[{"text": "📱 Открыть приложение", "url": MINI_APP_URL}]]}
                    await send_telegram(uid, msg, kb)
                except Exception as e:
                    log.error(f"Digest user {uid} error: {e}")
                await asyncio.sleep(5)
        except Exception as e:
            log.error(f"Digest loop error: {e}")
        await asyncio.sleep(12 * 3600)


# ============================================================
# 🌐 МИНИ-АП ЭНДПОИНТЫ
# ============================================================

def extract_user_from_init_data(init_data: str):
    try:
        pairs = {}
        for item in init_data.split("&"):
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            try:
                pairs[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
            except Exception:
                pairs[k] = v
        user_json_str = pairs.get("user")
        if not user_json_str:
            return None
        user_obj = json.loads(user_json_str)
        uid = user_obj.get("id")
        return int(uid) if uid else None
    except Exception as e:
        log.error(f"Extract user error: {e}")
        return None


async def miniapp_verify(request):
    try:
        data = await parse_json_body(request)
        init_data = data.get("initData", "")
        if not init_data:
            return web.json_response({"error": "No initData"}, status=400)
        user_id = extract_user_from_init_data(init_data)
        if not user_id:
            return web.json_response({"error": "Invalid initData format"}, status=400)
        register_user(int(user_id), "", None)
        log.info(f"Miniapp verify OK: user_id={user_id}")
        return web.json_response({"ok": True, "user_id": int(user_id)})
    except Exception as e:
        log.error(f"Miniapp verify error: {type(e).__name__}: {e}", exc_info=True)
        return web.json_response({"error": f"Server error: {str(e)[:200]}"}, status=500)


async def miniapp_data(request):
    try:
        user_id = int(request.query.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)
        data = get_user_data(user_id)
        resumes = list_resumes(user_id)
        cur.execute(
            "SELECT vacancy_id, title, status FROM liked_vacancies WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user_id,)
        )
        tracker_rows = cur.fetchall()
        tracker = [{"vacancy_id": r[0], "title": r[1], "status": r[2]} for r in tracker_rows]
        active_resume_text = get_active_resume(user_id)
        cur.execute("SELECT digest_active FROM users WHERE user_id=?", (user_id,))
        drow = cur.fetchone()
        digest_active = int(drow[0]) if drow and drow[0] else 0
        return web.json_response({
            "balance": data["balance"],
            "unlimited_until": data["unlimited_until"],
            "is_premium": is_premium_user(user_id),
            "resumes_count": len(resumes),
            "has_active_resume": len(active_resume_text) > 0,
            "digest_active": digest_active,
            "tracker": tracker,
            "active_resume_preview": active_resume_text[:500] if active_resume_text else ""
        })
    except Exception as e:
        log.error(f"Miniapp data error: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def miniapp_upload_resume(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        filename = (body.get("filename") or "resume.pdf").strip()
        b64 = body.get("content_base64") or ""
        if not user_id or not b64:
            return web.json_response({"error": "Нет данных файла"}, status=400)
        # Принимаем и чистый base64, и dataURL с префиксом
        if "," in b64[:80]:
            b64 = b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return web.json_response({"error": "Файл повреждён при передаче. Попробуйте ещё раз."}, status=400)
        log.info(f"Upload: user={user_id} file={filename} bytes={len(raw)}")
        if len(raw) > MAX_FILE_SIZE:
            return web.json_response({"error": "Файл больше 5 МБ"}, status=400)
        if len(raw) < 200:
            return web.json_response({"error": "Файл пустой или слишком маленький"}, status=400)
        base, ext = os.path.splitext(filename)
        ext = ext.lower()
        safe_base = re.sub(r'[^\w\.\-]', '_', base)[:60]
        safe_name = (safe_base + ext) if ext else safe_base
        path = f"tmp_up_{user_id}_{safe_name}"
        with open(path, "wb") as f:
            f.write(raw)
        fnl = safe_name.lower()
        if fnl.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            text = await extract_text_from_image(path)
        else:
            text = await asyncio.to_thread(extract_text, path, safe_name)
        if os.path.exists(path):
            os.remove(path)
        if not text or not text.strip():
            return web.json_response({
                "error": f"Не удалось извлечь текст из файла '{filename}'. Если это скан-PDF или фото — загрузите через бота (там есть OCR)."
            }, status=422)
        add_resume(user_id, safe_name, text)
        log.info(f"Miniapp upload resume OK: user_id={user_id}, file={safe_name}, chars={len(text)}")
        return web.json_response({"ok": True, "resumes_count": len(list_resumes(user_id))})
    except Exception as e:
        log.error(f"Upload resume error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_resumes_list(request):
    try:
        user_id = int(request.query.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)
        rows = list_resumes(user_id)
        return web.json_response({"resumes": rows})
    except Exception as e:
        log.error(f"Resumes list error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_activate_resume(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        resume_id = int(body.get("resume_id", 0))
        if not user_id or not resume_id:
            return web.json_response({"error": "Нет данных"}, status=400)
        cur.execute("UPDATE resumes SET active=0 WHERE user_id=?", (user_id,))
        cur.execute("UPDATE resumes SET active=1 WHERE id=? AND user_id=?", (resume_id, user_id))
        conn.commit()
        return web.json_response({"ok": True})
    except Exception as e:
        log.error(f"Activate resume error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_like(request):
    """POST /miniapp/like — добавить вакансию в трекер из приложения."""
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        vacancy_id = str(body.get("vacancy_id", ""))
        title = str(body.get("title", "Вакансия"))
        if not user_id or not vacancy_id:
            return web.json_response({"error": "Нет данных"}, status=400)
        like_vacancy(user_id, vacancy_id, title)
        return web.json_response({"ok": True})
    except Exception as e:
        log.error(f"Like error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_cover_letter(request):
    """POST /miniapp/cover-letter — сопроводительное под вакансию (1 запрос)."""
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        company = (body.get("company") or "").strip()
        title = (body.get("title") or "").strip()
        if not user_id or not company or not title:
            return web.json_response({"error": "Нет компании или должности"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        resume = get_active_resume(user_id) or "Опыт не указан."
        letter = await asyncio.to_thread(ai_generate,
            f"Напиши сопроводительное письмо на позицию '{title}' в '{company}'.\n"
            f"Резюме:\n{resume[:3000]}\n\n"
            "Объём до 150 слов. Тон: уверенный, конкретный, без воды. Выдай ТОЛЬКО текст письма.")
        if not letter or not validate_ai_response(letter, min_length=50):
            return web.json_response({"error": "ИИ недоступен, попробуйте ещё раз"}, status=500)
        return web.json_response({"letter": letter})
    except Exception as e:
        log.error(f"Cover letter error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_invoice(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        tariff = body.get("tariff", "pack50")
        if not user_id:
            return web.json_response({"error": "Нет user_id"}, status=400)
        if tariff == "unl10":
            amount, title = 500, "Безлимит 10 дней + Премиум"
        else:
            amount, title = 100, "Пакет 50 запросов"
        payload = f"mini_{tariff}_{user_id}"
        async with HTTP.post(f"{TELEGRAM_API}/createInvoiceLink", json={
            "title": title,
            "description": "Пополнение баланса Lemus Career Agent",
            "payload": payload,
            "currency": "XTR",
            "prices": [{"label": "Stars", "amount": amount}],
        }) as resp:
            r = await resp.json()
        link = r.get("result")
        if not link:
            return web.json_response({"error": "Не удалось создать счет"}, status=500)
        return web.json_response({"invoice_link": link, "tariff": tariff})
    except Exception as e:
        log.error(f"Invoice error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_payments(request):
    try:
        user_id = int(request.query.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)
        cur.execute("SELECT amount, status, created_at FROM payments WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,))
        rows = cur.fetchall()
        return web.json_response({"payments": [{"amount": r[0], "status": r[1], "created_at": r[2]} for r in rows]})
    except Exception as e:
        log.error(f"Payments error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_digest(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        active = 1 if body.get("active") else 0
        if not user_id:
            return web.json_response({"error": "Нет user_id"}, status=400)
        register_user(user_id, "", None)
        cur.execute("UPDATE users SET digest_active=? WHERE user_id=?", (active, user_id))
        conn.commit()
        return web.json_response({"ok": True, "digest_active": active})
    except Exception as e:
        log.error(f"Digest toggle error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_referral(request):
    try:
        user_id = int(request.query.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)
        cur.execute("SELECT COUNT(*) FROM users WHERE referred_by=?", (user_id,))
        referred = cur.fetchone()[0]
        return web.json_response({
            "link": f"https://t.me/{BOT_USERNAME}?start={user_id}",
            "referred_count": referred,
            "bonus_per_friend": 7,
        })
    except Exception as e:
        log.error(f"Referral error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_courses(request):
    try:
        user_id = int(request.query.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)
        if not is_premium_user(user_id):
            return web.json_response({"error": "Доступно только премиум-пользователям"}, status=403)
        out = []
        for cid, c in COURSES.items():
            out.append({"id": cid, "title": c["title"], "description": c["description"],
                        "lessons": [l["title"] for l in c["lessons"]]})
        return web.json_response({"courses": out})
    except Exception as e:
        log.error(f"Courses error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_lesson(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        course_id = body.get("course", "")
        num = int(body.get("num", 0))
        if not is_premium_user(user_id):
            return web.json_response({"error": "Доступно только премиум-пользователям"}, status=403)
        course = COURSES.get(course_id)
        if not course or num < 1 or num > len(course["lessons"]):
            return web.json_response({"error": "Урок не найден"}, status=404)
        lesson = course["lessons"][num - 1]
        return web.json_response({"title": lesson["title"], "content": lesson["content"],
                                  "total": len(course["lessons"])})
    except Exception as e:
        log.error(f"Lesson error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_templates(request):
    try:
        user_id = int(request.query.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "No user_id"}, status=400)
        if not is_premium_user(user_id):
            return web.json_response({"error": "Доступно только премиум-пользователям"}, status=403)
        return web.json_response({"templates": [{"num": i, "name": t["name"]} for i, t in enumerate(COVER_LETTER_TEMPLATES, 1)]})
    except Exception as e:
        log.error(f"Templates error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_template(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        num = int(body.get("num", 0))
        if not is_premium_user(user_id):
            return web.json_response({"error": "Доступно только премиум-пользователям"}, status=403)
        if num < 1 or num > len(COVER_LETTER_TEMPLATES):
            return web.json_response({"error": "Шаблон не найден"}, status=404)
        t = COVER_LETTER_TEMPLATES[num - 1]
        return web.json_response({"name": t["name"], "content": t["content"]})
    except Exception as e:
        log.error(f"Template error: {e}")
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_crisis_tool(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        tool = body.get("tool", "")
        params = (body.get("params") or "").strip()
        if not user_id or tool not in CRISIS_TOOLS_SET:
            return web.json_response({"error": "Некорректный запрос"}, status=400)
        if not is_premium_user(user_id):
            return web.json_response({"error": "Доступно по подписке Премиум (Безлимит 10 дней)"}, status=403)
        if tool not in ("day", "bridge") and len(params) < 5:
            return web.json_response({"error": "Заполни параметры инструмента"}, status=400)
        resume = get_active_resume(user_id) or ""
        prompt = build_crisis_prompt(tool, params, resume)
        result = await asyncio.to_thread(ai_generate, prompt)
        if not result or not validate_ai_response(result, min_length=50):
            return web.json_response({"error": "ИИ недоступен, попробуйте ещё раз"}, status=500)
        return web.json_response({"result": result, "tool": tool})
    except Exception as e:
        log.error(f"Crisis tool error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_analyze_vacancy(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        vacancy_text = (body.get("vacancy_text") or "").strip()
        if not user_id or not vacancy_text or len(vacancy_text) < 100:
            return web.json_response({"error": "Недостаточно данных"}, status=400)
        if not spend_balance(user_id, cost=2):
            return web.json_response({"error": "Недостаточно запросов! Нужно 2."}, status=402)
        resume = get_active_resume(user_id)
        extract_prompt = (
            "Ты — эксперт по анализу вакансий. Проанализируй текст и вытащи:\n"
            "1. Название компании.\n2. Название должности.\n3. Имя контактного лица (если есть).\n"
            f"Текст вакансии:\n{vacancy_text[:4000]}\n\n"
            "Выдай ТОЛЬКО JSON: {\"company\": \"...\", \"title\": \"...\", \"contact_name\": \"...\"}"
        )
        ai_response = await asyncio.to_thread(ai_generate, extract_prompt)
        company = ""
        title = ""
        contact_name = ""
        if ai_response and validate_ai_response(ai_response, min_length=10):
            clean = ai_response.replace("```json", "").replace("```", "").strip()
            json_match = re.search(r'\{.*\}', clean, re.S)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                    company = parsed.get("company", "").strip()
                    title = parsed.get("title", "").strip()
                    contact_name = parsed.get("contact_name", "").strip()
                except json.JSONDecodeError:
                    pass
        if not company or not title:
            lines = [l.strip() for l in vacancy_text.split("\n") if l.strip()][:5]
            for line in lines:
                match = re.search(r'([А-ЯA-Z][\w\s\-\.]+?)\s+(?:ищет|приглашает|нанимает)\s+(.+?)(?:[,\.\-]|$)', line, re.I)
                if match and not company:
                    company = match.group(1).strip()
                    if not title:
                        title = match.group(2).strip()[:60]
        if not company or not title:
            return web.json_response({"error": "Не удалось определить компанию или должность."}, status=422)
        aggressive_results = await aggressive_recruiter_search(0, company, title, contact_name)
        pitch_prompt = (
            f"Напиши короткий питч (4-5 строк) для рекрутера компании '{company}' на позицию '{title}'.\n"
            f"Имя рекрутера: {contact_name or 'неизвестно'}\n"
            f"Резюме: {resume[:1500] if resume else 'Резюме не загружено'}\n"
            "Выдай ТОЛЬКО текст питча."
        )
        pitch = await asyncio.to_thread(ai_generate, pitch_prompt)
        if pitch and validate_ai_response(pitch, min_length=50):
            pitch = clean_pitch_text(pitch, title)
        else:
            pitch = "⚠️ Не удалось сгенерировать питч."
        cur.execute(
            "INSERT INTO liked_vacancies (user_id, vacancy_id, title, status) VALUES (?, ?, ?, 'Разобрана: Мини-ап')",
            (user_id, f"mini_{int(datetime.datetime.now().timestamp())}", f"{title} ({company})")
        )
        conn.commit()
        contacts_list = []
        if aggressive_results["found"]:
            for c in aggressive_results["contacts"][:3]:
                contacts_list.append({
                    "name": c.get("name", ""),
                    "email": c.get("email", ""),
                    "phone": c.get("phone", ""),
                    "url": c.get("url", "")
                })
        return web.json_response({
            "company": company,
            "title": title,
            "contact_name": contact_name,
            "pitch": pitch,
            "contacts": contacts_list,
            "email_templates": aggressive_results.get("email_templates", []),
            "search_log": aggressive_results.get("search_log", "")
        })
    except Exception as e:
        log.error(f"Miniapp analyze error: {e}")
        track_error()
        return web.json_response({"error": f"Ошибка сервера: {str(e)[:200]}"}, status=500)


async def miniapp_search_vacancies(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "Нет user_id"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        result = await core_search_vacancies(user_id)
        if result.get("error"):
            return web.json_response({"error": result["error"]}, status=400)
        return web.json_response({
            "vacancies": result["vacancies"],
            "queries": result["queries"],
            "industries": result["industries"],
        })
    except Exception as e:
        log.error(f"Miniapp search error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_find_lpr(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        company = (body.get("company") or "").strip()
        position = (body.get("position") or "").strip()
        if not user_id or not company:
            return web.json_response({"error": "Укажите компанию"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        aggressive_results = await aggressive_recruiter_search(0, company, position, "")
        contacts_list = []
        if aggressive_results["found"]:
            for c in aggressive_results["contacts"][:5]:
                contacts_list.append({
                    "name": c.get("name", ""),
                    "email": c.get("email", ""),
                    "phone": c.get("phone", ""),
                    "url": c.get("url", "")
                })
        encoded_company = urllib.parse.quote(company)
        links = [
            {"text": "🔍 HR в LinkedIn", "url": f"https://www.google.com/search?q=site:linkedin.com+%22{encoded_company}%22+HR"},
            {"text": "🔍 TenChat", "url": f"https://www.google.com/search?q=site:tenchat.ru+%22{encoded_company}%22+HR"},
            {"text": "🌐 Карьерный сайт", "url": f"https://www.google.com/search?q=%22карьера%22+%22{encoded_company}%22+контакты"}
        ]
        return web.json_response({
            "company": company,
            "position": position,
            "contacts": contacts_list,
            "email_templates": aggressive_results.get("email_templates", []),
            "search_log": aggressive_results.get("search_log", ""),
            "links": links
        })
    except Exception as e:
        log.error(f"Miniapp LPR error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_generate_pitch(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        company = (body.get("company") or "").strip()
        title = (body.get("title") or "").strip()
        if not user_id or not company:
            return web.json_response({"error": "Укажите компанию"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        resume = get_active_resume(user_id)
        prompt = (
            f"Напиши короткий питч (4-5 строк) для рекрутера компании '{company}' на позицию '{title}'.\n"
            f"Резюме: {resume[:2000] if resume else 'Резюме не загружено'}\n"
            "Стиль от равного к равному. Выдай ТОЛЬКО текст питча."
        )
        pitch = await asyncio.to_thread(ai_generate, prompt)
        if not pitch or not validate_ai_response(pitch, min_length=50):
            return web.json_response({"error": "Не удалось сгенерировать питч"}, status=500)
        pitch = clean_pitch_text(pitch, title or "Специалист")
        return web.json_response({"pitch": pitch, "company": company, "title": title})
    except Exception as e:
        log.error(f"Miniapp pitch error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_skill_gap(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        if not user_id:
            return web.json_response({"error": "Нет user_id"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        resume = get_active_resume(user_id)
        if not resume:
            return web.json_response({"error": "Сначала загрузите резюме"}, status=400)
        current_date = datetime.date.today().strftime("%d.%m.%Y")
        prompt = (
            f"Дата: {current_date}. Проведи анализ навыков (Skill Gap) кандидата.\n"
            "Выдай структурированный ответ:\n"
            "✅ СИЛЬНЫЕ КОМПЕТЕНЦИИ: [3-5 пунктов]\n"
            "⚠️ ЗОНЫ РОСТА: [3-5 пунктов]\n"
            "💡 РЕКОМЕНДАЦИИ: [что подтянуть]\n\n" + resume[:8000]
        )
        analysis = await asyncio.to_thread(ai_generate, prompt)
        if not analysis or not validate_ai_response(analysis, min_length=100):
            return web.json_response({"error": "Не удалось провести анализ"}, status=500)
        user_skillgap_cache[user_id] = analysis
        return web.json_response({"analysis": analysis})
    except Exception as e:
        log.error(f"Miniapp skill gap error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_match(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        resume_text = (body.get("resume_text") or "").strip()
        vacancy_text = (body.get("vacancy_text") or "").strip()
        if not user_id or not resume_text or not vacancy_text:
            return web.json_response({"error": "Заполните все поля"}, status=400)
        if len(resume_text) < 100 or len(vacancy_text) < 100:
            return web.json_response({"error": "Тексты слишком короткие"}, status=400)
        if not spend_balance(user_id, cost=2):
            return web.json_response({"error": "Недостаточно запросов! Нужно 2."}, status=402)
        prompt = (
            "Ты — опытный рекрутер. Проанализируй соответствие резюме кандидата требованиям вакансии.\n"
            f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}\n\n"
            f"--- ТЕКСТ ВАКАНСИИ ---\n{vacancy_text[:3000]}\n\n"
            "Выдай структурированный анализ:\n"
            "🎯 ОБЩИЙ ПРОЦЕНТ СООТВЕТСТВИЯ: [число]%\n"
            "Разбивка:\n"
            "   • Опыт работы: [число]% \n"
            "   • Навыки: [число]% \n"
            "   • Образование: [число]% \n"
            "✅ СИЛЬНЫЕ СТОРОНЫ: [3-5 пунктов]\n"
            "⚠️ ПРОБЕЛЫ: [3-5 пунктов]\n"
            "🚩 КРАСНЫЕ ФЛАГИ: [или 'Не обнаружены']\n"
            "❓ ВОПРОСЫ ДЛЯ ИНТЕРВЬЮ: [3-5 вопросов]\n"
            "📋 ВЕРДИКТ: [Рекомендуем/Под вопросом/Не рекомендуем]"
        )
        analysis = await asyncio.to_thread(ai_generate, prompt)
        if not analysis or not validate_ai_response(analysis, min_length=100):
            return web.json_response({"error": "Не удалось проанализировать"}, status=500)
        percent_match = re.search(r'(\d+)\s*%', analysis)
        overall_percent = int(percent_match.group(1)) if percent_match else 0
        return web.json_response({"analysis": analysis, "overall_percent": overall_percent})
    except Exception as e:
        log.error(f"Miniapp HR match error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_scoring(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        resume_text = (body.get("resume_text") or "").strip()
        if not user_id or not resume_text or len(resume_text) < 100:
            return web.json_response({"error": "Пришлите полный текст резюме"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            "Ты — опытный рекрутер. Проведи скоринг кандидата по резюме.\n"
            f"--- РЕЗЮМЕ ---\n{resume_text[:4000]}\n\n"
            "Выдай структурированный анализ:\n"
            "🟢 СИЛЬНЫЕ СТОРОНЫ: [3-5 пунктов]\n"
            "🔴 КРАСНЫЕ ФЛАГИ: [если есть]\n"
            "🟡 НА ЧТО ОБРАТИТЬ ВНИМАНИЕ: [2-3 пункта]\n"
            "⭐ ОБЩАЯ ОЦЕНКА: [число от 1 до 10]\n"
            "📋 РЕКОМЕНДАЦИЯ: [краткая рекомендация]"
        )
        scoring = await asyncio.to_thread(ai_generate, prompt)
        if not scoring or not validate_ai_response(scoring, min_length=50):
            return web.json_response({"error": "Не удалось оценить"}, status=500)
        rating_match = re.search(r'[⭐★]\s*(?:ОБЩАЯ ОЦЕНКА|ОЦЕНКА)[^\d]*(\d)', scoring)
        rating = int(rating_match.group(1)) if rating_match else 0
        return web.json_response({"scoring": scoring, "rating": rating})
    except Exception as e:
        log.error(f"Miniapp HR scoring error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_questions(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        resume_text = (body.get("resume_text") or "").strip()
        if not user_id or len(resume_text) < 100:
            return web.json_response({"error": "Пришлите полный текст резюме"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            "Ты — опытный интервьюер. Составь список вопросов для собеседования.\n"
            f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}\n\n"
            "Составь вопросы по категориям:\n"
            "1. 🎯 Вопросы по опыту (3-4).\n"
            "2. 🔍 Уточняющие по пробелам (2-3).\n"
            "3. 🚩 Проверка красных флагов (2-3).\n"
            "4. 💡 Поведенческие (2-3).\n"
            "5. 🎪 Каверзные (1-2).\n"
            "6. 🤝 О мотивации (2)."
        )
        questions = await asyncio.to_thread(ai_generate, prompt)
        if not questions or not validate_ai_response(questions, min_length=100):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"questions": questions})
    except Exception as e:
        log.error(f"HR questions error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_test(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        resume_text = (body.get("resume_text") or "").strip()
        if not user_id or len(resume_text) < 100:
            return web.json_response({"error": "Пришлите полный текст резюме"}, status=400)
        if not spend_balance(user_id, cost=2):
            return web.json_response({"error": "Недостаточно запросов! Нужно 2."}, status=402)
        prompt = (
            "Ты — опытный нанимающий менеджер. Составь тестовое задание на основе резюме.\n"
            f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text[:4000]}\n\n"
            "ПРАВИЛА:\n"
            "1. Основано на том, что кандидат указал в резюме.\n"
            "2. Если управлял P&L — попроси рассчитать юнит-экономику.\n"
            "3. Если запускал продукт — попроси описать метрики, риски, план Б.\n"
            "4. Если оптимизировал процессы — попроси расчёт эффекта.\n"
            "5. Если руководил командой — попроси кейс управления конфликтом.\n"
            "6. Задание выполнимо за 1-2 часа.\n"
            "Выдай:\n"
            "📋 НАЗВАНИЕ\n⏱ ВРЕМЯ\n📝 ОПИСАНИЕ\n🎯 ЧТО ПРОВЕРЯЕМ\n✅ КРИТЕРИИ\n❓ ВОПРОСЫ ПОСЛЕ"
        )
        test_task = await asyncio.to_thread(ai_generate, prompt)
        if not test_task or not validate_ai_response(test_task, min_length=100):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"test_task": test_task})
    except Exception as e:
        log.error(f"HR test error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_description(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        params = (body.get("params") or "").strip()
        if not user_id or len(params) < 50:
            return web.json_response({"error": "Опишите вакансию подробнее"}, status=400)
        if not spend_balance(user_id, cost=2):
            return web.json_response({"error": "Недостаточно запросов! Нужно 2."}, status=402)
        prompt = (
            f"Ты — опытный рекрутер. Составь описание вакансии по структуре hh.ru.\n"
            f"Вводные: {params}\n\n"
            "Структура:\n"
            "📌 НАЗВАНИЕ ДОЛЖНОСТИ\n"
            "🏢 О КОМПАНИИ\n"
            "🎯 ОБЯЗАННОСТИ (5-8 пунктов)\n"
            "✅ ТРЕБОВАНИЯ (must have + nice to have)\n"
            "💎 УСЛОВИЯ\n"
            "🚀 ПРЕИМУЩЕСТВА\n"
            "📩 ПРИЗЫВ К ДЕЙСТВИЮ"
        )
        description = await asyncio.to_thread(ai_generate, prompt)
        if not description or not validate_ai_response(description, min_length=100):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"description": description})
    except Exception as e:
        log.error(f"HR description error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_rejection(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        params = (body.get("params") or "").strip()
        if not user_id or len(params) < 20:
            return web.json_response({"error": "Опишите ситуацию"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            f"Напиши вежливое письмо с отказом кандидату.\n"
            f"Параметры: {params}\n\n"
            "Письмо должно быть уважительным, кратким (до 150 слов)."
        )
        letter = await asyncio.to_thread(ai_generate, prompt)
        if not letter or not validate_ai_response(letter, min_length=50):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"letter": letter})
    except Exception as e:
        log.error(f"HR rejection error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_offer(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        params = (body.get("params") or "").strip()
        if not user_id or len(params) < 20:
            return web.json_response({"error": "Опишите параметры"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            f"Составь профессиональный шаблон оффера.\n"
            f"Параметры: {params}\n\n"
            "Оффер должен включать: поздравление, должность, условия, дату выхода, испытательный срок, следующие шаги."
        )
        offer = await asyncio.to_thread(ai_generate, prompt)
        if not offer or not validate_ai_response(offer, min_length=50):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"offer": offer})
    except Exception as e:
        log.error(f"HR offer error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_salary(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        params = (body.get("params") or "").strip()
        if not user_id or len(params) < 20:
            return web.json_response({"error": "Опишите параметры"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            f"Ты — эксперт по компенсациям. Оцени рыночную зарплату.\n"
            f"Параметры: {params}\n\n"
            "Выдай:\n"
            "1. 💰 Вилка зарплаты (минимум - медиана - максимум).\n"
            "2. 📊 Факторы влияния.\n"
            "3. 🎁 Типичный пакет бонусов.\n"
            "4. 📈 Тренды.\n"
            "5. 💡 Рекомендации."
        )
        analysis = await asyncio.to_thread(ai_generate, prompt)
        if not analysis or not validate_ai_response(analysis, min_length=50):
            return web.json_response({"error": "Не удалось оценить"}, status=500)
        return web.json_response({"analysis": analysis})
    except Exception as e:
        log.error(f"HR salary error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_candidate_pitch(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        params = (body.get("params") or "").strip()
        if not user_id or len(params) < 20:
            return web.json_response({"error": "Опишите вакансию"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            f"Ты — рекрутер. Напиши питч для кандидата.\n"
            f"Параметры: {params}\n\n"
            "Цепляет с первого предложения, показывает ценность, подчёркивает рост. Стиль от равного к равному."
        )
        pitch = await asyncio.to_thread(ai_generate, prompt)
        if not pitch or not validate_ai_response(pitch, min_length=50):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"pitch": pitch})
    except Exception as e:
        log.error(f"HR candidate pitch error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


async def miniapp_hr_followup(request):
    try:
        body = await parse_json_body(request)
        user_id = int(body.get("user_id", 0))
        params = (body.get("params") or "").strip()
        if not user_id or len(params) < 20:
            return web.json_response({"error": "Опишите ситуацию"}, status=400)
        if not spend_balance(user_id, cost=1):
            return web.json_response({"error": "Недостаточно запросов!"}, status=402)
        prompt = (
            f"Напиши фоллоу-ап письмо кандидату после собеседования.\n"
            f"Параметры: {params}\n\n"
            "Поблагодари, подчеркни что впечатлило, опиши следующие шаги."
        )
        followup = await asyncio.to_thread(ai_generate, prompt)
        if not followup or not validate_ai_response(followup, min_length=50):
            return web.json_response({"error": "Не удалось сгенерировать"}, status=500)
        return web.json_response({"followup": followup})
    except Exception as e:
        log.error(f"HR followup error: {e}")
        track_error()
        return web.json_response({"error": str(e)[:200]}, status=500)


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
            sp = msg["successful_payment"]
            uid = msg.get("from", {}).get("id") or msg["chat"]["id"]
            payload = sp.get("invoice_payload", "")
            amount = sp.get("total_amount", 0)
            try:
                cur.execute("INSERT INTO payments (user_id, amount, status) VALUES (?, ?, 'paid')", (uid, amount))
                conn.commit()
            except Exception as pe:
                log.error(f"Payment log error: {pe}")
            if payload.startswith("mini_"):
                parts = payload.split("_")
                tariff = parts[1] if len(parts) > 1 else "pack50"
                puid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else uid
                if tariff == "unl10":
                    admin_set_unlimited(puid, 10)
                    await send_telegram(puid, "🎉 *Безлимит на 10 дней активирован!*\nОткрыты: курсы, шаблоны, аналитика, план и 🆘 антикризисный пакет.")
                else:
                    admin_add_balance(puid, 50)
                    await send_telegram(puid, "🎉 *Начислено 50 запросов!*")
            elif "unl" in payload:
                admin_set_unlimited(uid, 10)
                await send_telegram(uid, "🎉 Безлимит активирован!")
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
            if data_str.startswith("crisis_"):
                tool = data_str[len("crisis_"):]
                if tool not in CRISIS_TOOLS_SET:
                    return web.Response(text="OK")
                if not is_premium_user(user_id):
                    await send_telegram(chat_id, "🔒 *Антикризисный пакет входит в Премиум.*\nБезлимит 10 дней — 500 ⭐: кнопка «💎 Оплата и Баланс».")
                elif tool in ("day", "bridge"):
                    bg(crisis_generate(chat_id, user_id, tool, ""))
                else:
                    user_search_cache[user_id] = user_search_cache.get(user_id, {})
                    user_search_cache[user_id]["crisis_tool"] = tool
                    user_states[user_id] = "waiting_for_crisis_input"
                    await send_telegram(chat_id, CRISIS_HINTS.get(tool, "Опиши ситуацию своими словами."))
            elif data_str == "fix_resume_from_gap":
                bg(run_fix_resume_by_gap(chat_id, user_id))
            elif data_str.startswith("page_"):
                bg(send_vacancies_page(chat_id, user_id, page=int(data_str.split("_")[1])))
            elif data_str == "buy_pack_stars":
                bg(send_stars_invoice(chat_id, 100, "Пакет 50 запросов", "credits_50"))
            elif data_str == "buy_unl_stars":
                bg(send_stars_invoice(chat_id, 500, "Безлимит 10 дней + Премиум", "unl_10d"))
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
            elif data_str == "course_crisis":
                bg(show_course_lessons(chat_id, user_id, "crisis"))
            elif data_str.startswith("lesson_"):
                parts = data_str.split("_")
                if len(parts) == 3:
                    bg(show_lesson(chat_id, user_id, parts[1], int(parts[2])))
            elif data_str.startswith("template_"):
                bg(show_template(chat_id, user_id, int(data_str.split("_")[1])))
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
                await send_telegram(chat_id, "📌 Вакансия добавлена в Трекер!")
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
            elif data_str.startswith("act_"):
                bg(activate_resume(chat_id, user_id, data_str[4:]))
            elif data_str.startswith("adaptsel_"):
                user_adapt_target[user_id] = int(data_str[9:])
                user_states[user_id] = "waiting_for_adaptation_vacancy"
                bg(http_edit_message_text(chat_id, message_id, "✅ Резюме выбрано! Отправьте текст вакансии."))
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
            user_rate_limits.clear()
            log.info("🧹 Memory cleanup completed")
        except Exception as e:
            log.error(f"Cleanup error: {e}")
        await asyncio.sleep(6 * 3600)


# ---------------- Запуск ----------------
async def main():
    global HTTP, BOT_USERNAME
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
    try:
        async with HTTP.get(f"{TELEGRAM_API}/getMe") as resp:
            me = await resp.json()
            BOT_USERNAME = me.get("result", {}).get("username", BOT_USERNAME)
    except Exception as e:
        log.warning(f"getMe failed: {e}")
    app = web.Application(middlewares=[cors_middleware], client_max_size=32 * 1024 * 1024)
    app.router.add_get("/", lambda r: web.Response(text="Bot is running"))
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)

    routes = [
        ("POST", "/miniapp/verify", miniapp_verify),
        ("GET", "/miniapp/data", miniapp_data),
        ("POST", "/miniapp/upload-resume", miniapp_upload_resume),
        ("GET", "/miniapp/resumes", miniapp_resumes_list),
        ("POST", "/miniapp/activate-resume", miniapp_activate_resume),
        ("POST", "/miniapp/like", miniapp_like),
        ("POST", "/miniapp/cover-letter", miniapp_cover_letter),
        ("POST", "/miniapp/invoice", miniapp_invoice),
        ("GET", "/miniapp/payments", miniapp_payments),
        ("POST", "/miniapp/digest", miniapp_digest),
        ("GET", "/miniapp/referral", miniapp_referral),
        ("GET", "/miniapp/courses", miniapp_courses),
        ("POST", "/miniapp/lesson", miniapp_lesson),
        ("GET", "/miniapp/templates", miniapp_templates),
        ("POST", "/miniapp/template", miniapp_template),
        ("POST", "/miniapp/crisis-tool", miniapp_crisis_tool),
        ("POST", "/miniapp/analyze", miniapp_analyze_vacancy),
        ("POST", "/miniapp/search", miniapp_search_vacancies),
        ("POST", "/miniapp/find-lpr", miniapp_find_lpr),
        ("POST", "/miniapp/pitch", miniapp_generate_pitch),
        ("POST", "/miniapp/skill-gap", miniapp_skill_gap),
        ("POST", "/miniapp/hr-match", miniapp_hr_match),
        ("POST", "/miniapp/hr-scoring", miniapp_hr_scoring),
        ("POST", "/miniapp/hr-questions", miniapp_hr_questions),
        ("POST", "/miniapp/hr-test", miniapp_hr_test),
        ("POST", "/miniapp/hr-description", miniapp_hr_description),
        ("POST", "/miniapp/hr-rejection", miniapp_hr_rejection),
        ("POST", "/miniapp/hr-offer", miniapp_hr_offer),
        ("POST", "/miniapp/hr-salary", miniapp_hr_salary),
        ("POST", "/miniapp/hr-candidate-pitch", miniapp_hr_candidate_pitch),
        ("POST", "/miniapp/hr-followup", miniapp_hr_followup),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)

    log.info("✅ MiniApp endpoints registered with CORS middleware (32 endpoints)")

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    render_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if render_url:
        webhook_url = f"{render_url.rstrip('/')}/{BOT_TOKEN}"
        async with HTTP.get(f"{TELEGRAM_API}/setWebhook?url={webhook_url}") as resp:
            log.info("setWebhook: %s", (await resp.text())[:200])
    log.info("🚀 Bot v4.3 started successfully.")
    bg(cleanup_old_data())
    bg(monitor_load())
    bg(digest_loop())
    try:
        await asyncio.Event().wait()
    finally:
        await HTTP.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass