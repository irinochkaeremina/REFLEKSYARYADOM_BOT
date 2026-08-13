import os
import json
import asyncio
import threading
import logging
import requests

import psycopg2
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

# ===== Логирование =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===== Переменные окружения =====
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TOKEN:
    raise ValueError("BOT_TOKEN не задан")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY не задан")

# ===== Gemini =====
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-3.1-flash-lite"

with open("system_prompt.txt", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

# ===== База данных =====
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            history JSONB DEFAULT '[]'::jsonb,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Таблица users проверена/создана.")

def get_history(chat_id: int) -> list:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT history FROM users WHERE chat_id = %s", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else []

def save_history(chat_id: int, history: list):
    conn = get_db_connection()
    cur = conn.cursor()
    history_json = json.dumps(history, ensure_ascii=False)
    cur.execute("""
        INSERT INTO users (chat_id, history, updated_at)
        VALUES (%s, %s::jsonb, CURRENT_TIMESTAMP)
        ON CONFLICT (chat_id) DO UPDATE
        SET history = EXCLUDED.history, updated_at = CURRENT_TIMESTAMP
    """, (chat_id, history_json))
    conn.commit()
    cur.close()
    conn.close()

def trim_history(history: list, max_messages: int = 20) -> list:
    if len(history) > max_messages:
        return history[-max_messages:]
    return history

# ===== Обработчики =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Здравствуйте, {user.first_name}! 👋\n\n"
        "Я — ваш рефлексивный помощник. Я помогу вам глубже разобраться в ситуации, "
        "задам уточняющие вопросы, обращу внимание на телесные ощущения, эмоции и мысли. "
        "Просто расскажите, что вас беспокоит, или задайте вопрос.\n\n"
        "Начнём? 😊"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_text = update.message.text

    history = get_history(chat_id)
    history.append({"role": "user", "parts": [user_text]})
    trimmed_history = trim_history(history, max_messages=20)

    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=SYSTEM_PROMPT
    )

    gemini_messages = []
    for msg in trimmed_history:
        role = msg["role"]
        if role == "user":
            gemini_messages.append({"role": "user", "parts": msg["parts"]})
        elif role == "assistant":
            gemini_messages.append({"role": "model", "parts": msg["parts"]})

    chat = model.start_chat(history=gemini_messages)
    try:
        response = chat.send_message(user_text)
        assistant_reply = response.text
    except Exception as e:
        logger.error(f"Ошибка Gemini: {e}")
        await update.message.reply_text("Извините, произошла ошибка. Попробуйте позже.")
        return

    history.append({"role": "assistant", "parts": [assistant_reply]})
    save_history(chat_id, history)
    await update.message.reply_text(assistant_reply)

# ===== Flask health-check =====
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

import requests  # Убедись, что импорт есть в начале файла

async def ping_self():
    url = os.getenv("RENDER_URL")
    if not url:
        return
    while True:
        try:
            # Делаем синхронный запрос в отдельном потоке, чтобы не блокировать бота
            await asyncio.to_thread(requests.get, url, timeout=10)
            logger.info("Автопинг успешен")
        except Exception as e:
            logger.error(f"Ошибка автопинга: {e}")
        await asyncio.sleep(300)  # 5 минут

# ===== Запуск бота =====
async def run_bot():
    # Создаём таблицу в базе, если её нет
    init_db()
    
    # Собираем приложение Telegram
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Запускаем автопинг (если задан RENDER_URL)
    asyncio.create_task(ping_self())
    
    logger.info("Бот запущен")
    # Запускаем polling (ожидание сообщений)
    await app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке для health-check
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Запускаем основную асинхронную функцию бота
    asyncio.run(run_bot())
