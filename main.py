import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import aiohttp
from aiohttp import web
from docx import Document
from google import genai
from google.genai import types as gtypes
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib import colors

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "") # Для карт (опционально)
PORT = int(os.getenv("PORT", "10000"))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------------- База данных ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 30);
CREATE TABLE IF NOT EXISTS resumes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, text TEXT, active INTEGER);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, status TEXT);
""")
conn.commit()

# ---------------- Функционал PDF и ИИ ----------------
def generate_hh_pdf(text_content):
    stream = io.BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    styles = getSampleStyleSheet()
    normal = ParagraphStyle('HHNormal', parent=styles['Normal'], fontSize=10, leading=14)
    title = ParagraphStyle('HHTitle', parent=styles['Heading1'], fontSize=14, leading=18, spaceAfter=10)
    story = [Paragraph(line.replace('#', ''), title) if line.startswith('#') else Paragraph(line, normal) for line in text_content.split('\n')]
    doc.build(story)
    return stream.getvalue()

# ---------------- Обработчик платежей ----------------
async def send_invoice(chat_id):
    # Пример на 50 запросов за 100 звезд
    await HTTP.post(f"{TELEGRAM_API}/sendInvoice", json={
        "chat_id": chat_id,
        "title": "Пакет 50 запросов",
        "description": "Пополнение баланса бота",
        "payload": "50_credits",
        "currency": "XTR",
        "prices": [{"label": "Stars", "amount": 100}]
    })

# ---------------- Основной функционал ----------------
# (Здесь должны быть функции ai_generate, run_resume_audit, run_resume_adaptation, process_message)
# Примечание: Чтобы не превышать лимит, используй предыдущую логику process_message с добавлением нижеуказанных кейсов:

async def process_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")
    
    # 1. Прием чеков
    if msg.get("photo") and user_states.get(chat_id) == "waiting_for_receipt":
        await send_telegram(ADMIN_ID, f"📸 Чек от @{msg['from'].get('username', chat_id)} (ID: {chat_id})", 
                           reply_markup={"inline_keyboard": [[{"text": "✅ Начислить 50", "callback_data": f"pay_{chat_id}_50"}]]})
        await send_telegram(chat_id, "✅ Чек отправлен администратору на проверку!")
        user_states.pop(chat_id)

    # 2. Обработка админских кнопок
    # ... логика ...

# ---------------- Вебхук обновленный ----------------
async def telegram_webhook(request):
    data = await request.json()
    if "pre_checkout_query" in data:
        # Авто-подтверждение платежа звездами
        await HTTP.post(f"{TELEGRAM_API}/answerPreCheckoutQuery", json={"pre_checkout_query_id": data["pre_checkout_query"]["id"], "ok": True})
    
    if "message" in data:
        msg = data["message"]
        if msg.get("successful_payment"):
            uid = msg["chat"]["id"]
            admin_add_balance(uid, 50)
            await send_telegram(uid, "🎉 Оплата прошла! Начислено 50 запросов.")
        else:
            await process_message(msg)
            
    if "callback_query" in data:
        cb = data["callback_query"]
        if cb["data"].startswith("pay_"):
            parts = cb["data"].split("_")
            admin_add_balance(int(parts[1]), int(parts[2]))
            await send_telegram(int(parts[1]), "✅ Администратор подтвердил оплату! Баланс пополнен.")
            await answer_callback(cb["id"])
            
    return web.Response(text="OK")

# ---------------- Запуск ----------------
async def main():
    global HTTP
    HTTP = aiohttp.ClientSession()
    app = web.Application()
    app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
