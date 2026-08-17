import asyncio
import html
import io
import json
import logging
import os
import re
import sqlite3

import aiohttp
import requests
from aiohttp import web
from docx import Document
from google import genai
from google.genai import types as gtypes
from pypdf import PdfReader

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("career_bot")

# ---------------- Конфиг ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
  raise SystemExit("🔴 BOT_TOKEN не задан!")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_KEY = os.getenv("GROQ_KEY", "")
PORT = int(os.getenv("PORT", "10000"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODEL_CANDIDATES = [
    os.getenv("GEMINI_MODEL", "gemini-flash-latest"),
    "gemini-3.5-flash",
]
GROQ_MODEL = "llama-3.1-8b-instant"

HTTP = None  # Инициализируем внутри main()
temp_vacancies = {}
user_states = {}
user_adapt_target = {}

# ---------------- БД ----------------
conn = sqlite3.connect("tracker.db", check_same_thread=False)
cur = conn.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 30, referred_by INTEGER);
CREATE TABLE IF NOT EXISTS resumes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT, text TEXT, active INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS hidden_vacancies (user_id INTEGER, vacancy_id TEXT, PRIMARY KEY (user_id, vacancy_id));
""")
conn.commit()


# ---------------- Утилиты ----------------
def get_user_balance(uid):
  cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
  row = cur.fetchone()
  return row[0] if row else 30


def spend_balance(uid, cost=1):
  if get_user_balance(uid) < cost:
    return False
  cur.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (cost, uid))
  conn.commit()
  return True


def admin_add_balance(user_id: int, amount: int) -> int:
  cur.execute(
      "UPDATE users SET balance = balance + ? WHERE user_id=?",
      (amount, user_id),
  )
  conn.commit()
  return get_user_balance(user_id)


def admin_set_balance(user_id: int, amount: int):
  cur.execute("UPDATE users SET balance = ? WHERE user_id=?", (amount, user_id))
  conn.commit()


def ai_generate(prompt):
  if client:
    for m in GEMINI_MODEL_CANDIDATES:
      try:
        resp = client.models.generate_content(model=m, contents=prompt)
        if resp and resp.text:
          return resp.text
      except:
        continue
  if GROQ_KEY:
    try:
      r = requests.post(
          "https://api.groq.com/openai/v1/chat/completions",
          headers={"Authorization": f"Bearer {GROQ_KEY}"},
          json={
              "model": GROQ_MODEL,
              "messages": [{"role": "user", "content": prompt}],
          },
          timeout=90,
      )
      return r.json()["choices"][0]["message"]["content"]
    except:
      pass
  return None


# ---------------- Логика Аудита ----------------
async def run_resume_audit(chat_id: int):
  if not spend_balance(chat_id, cost=1):
    await send_telegram(chat_id, "⚠️ Недостаточно запросов!")
    return
  await send_telegram(
      chat_id, "📋 *Провожу аудит и формирую исправленный файл...*"
  )
  resume = get_active_resume(chat_id)

  audit_prompt = (
      "Ты жесткий IT-рекрутер. Сделай подробный аудит резюме (ошибки, советы):\n\n"
      f"{resume[:8000]}"
  )
  audit_text = await asyncio.to_thread(ai_generate, audit_prompt)

  rewrite_prompt = (
      "Перепиши резюме для Senior позиции. Обязательно добавь навыки: Agile,"
      " Jira, Confluence, LTV, CAC, ARPU, SaaS, IoT, M2M, B2B Sales. Сохрани"
      " структуру (Контакты, Summary, Навыки, Опыт, Образование). Выдай ТОЛЬКО"
      f" текст без лишних знаков:\n\n{resume[:8000]}"
  )
  improved_text = await asyncio.to_thread(ai_generate, rewrite_prompt)

  if audit_text and improved_text:
    await send_telegram(chat_id, f"📋 *Аудит резюме:*\n\n{audit_text}")
    doc = Document()
    for p in improved_text.split("\n"):
      clean_p = p.replace("*", "").replace("#", "")
      if clean_p.strip():
        doc.add_paragraph(clean_p)
    stream = io.BytesIO()
    doc.save(stream)
    await send_document_bytes(
        chat_id,
        stream.getvalue(),
        "Improved_Resume.docx",
        "✅ Ваше профессиональное резюме.",
    )
  else:
    await send_telegram(chat_id, "⚠️ Ошибка генерации.")


# ---------------- Helpers ----------------
async def send_document_bytes(chat_id, file_bytes, filename, caption=""):
  form = aiohttp.FormData()
  form.add_field("chat_id", str(chat_id))
  form.add_field("caption", caption)
  form.add_field(
      "document",
      file_bytes,
      filename=filename,
      content_type=(
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
      ),
  )
  await HTTP.post(f"{TELEGRAM_API}/sendDocument", data=form)


async def send_telegram(chat_id, text, reply_markup=None):
  await HTTP.post(
      f"{TELEGRAM_API}/sendMessage",
      json={
          "chat_id": chat_id,
          "text": text[:4096],
          "parse_mode": "Markdown",
          "reply_markup": reply_markup,
      },
  )


def get_active_resume(user_id):
  cur.execute(
      "SELECT text FROM resumes WHERE user_id=? AND active=1 LIMIT 1",
      (user_id,),
  )
  row = cur.fetchone()
  return row[0] if row else ""


def list_resumes(user_id):
  cur.execute(
      "SELECT id, name, active FROM resumes WHERE user_id=? ORDER BY id",
      (user_id,),
  )
  return [{"id": r[0], "name": r[1], "active": r[2]} for r in cur.fetchall()]


# ---------------- Обработка сообщений ----------------
async def process_message(msg):
  chat_id = msg["chat"]["id"]
  text = msg.get("text", "")
  is_admin = ADMIN_ID != 0 and chat_id == ADMIN_ID

  if text.startswith("/start"):
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, balance) VALUES (?, ?,"
        " 30)",
        (chat_id, msg["chat"].get("username", "")),
    )
    conn.commit()
    await send_telegram(
        chat_id,
        "👋 Привет! Твой карьерный агент готов к работе.",
        get_keyboard(is_admin),
    )

  elif text == "📋 Аудит резюме":
    bg(run_resume_audit(chat_id))

  elif text == "🛠 Адаптация резюме":
    rows = list_resumes(chat_id)
    if not rows:
      await send_telegram(chat_id, "⚠️ Сначала загрузите резюме!")
      return
    kb = {
        "inline_keyboard": [[{
            "text": f"📄 {r['name']}",
            "callback_data": f"adaptsel_{r['id']}",
        }] for r in rows]
    }
    await send_telegram(chat_id, "🛠 Выберите резюме для адаптации:", kb)

  elif is_admin and text.startswith("/add_credits"):
    parts = text.split()
    if len(parts) == 3:
      admin_add_balance(int(parts[1]), int(parts[2]))
      await send_telegram(chat_id, "✅ Баланс пополнен.")


def bg(coro):
  asyncio.create_task(coro)


def get_keyboard(is_admin=False):
  kb = [
      [{"text": "📁 Мои резюме"}, {"text": "📥 Загрузить резюме"}],
      [{"text": "🔍 Поиск вакансий"}, {"text": "🛠 Адаптация резюме"}],
      [{"text": "📋 Аудит резюме"}, {"text": "💎 Оплата и Баланс"}],
      [{"text": "👥 Пригласить друга"}, {"text": "ℹ️ Помощь"}],
  ]
  if is_admin:
    kb.append([{"text": "👑 Админ-панель"}])
  return {"keyboard": kb, "resize_keyboard": True}


async def telegram_webhook(request):
  data = await request.json()
  if "message" in data:
    bg(process_message(data["message"]))
  return web.Response(text="OK")


# ---------------- Запуск ----------------
async def main():
  global HTTP
  # Инициализируем ClientSession СТРОГО внутри async функции, где есть event loop
  HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

  app = web.Application()
  app.router.add_post(f"/{BOT_TOKEN}", telegram_webhook)
  runner = web.AppRunner(app)
  await runner.setup()
  await web.TCPSite(runner, "0.0.0.0", PORT).start()
  log.info("🚀 Bot started successfully.")
  await asyncio.Event().wait()


if __name__ == "__main__":
  asyncio.run(main())
