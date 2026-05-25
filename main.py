import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from openai import OpenAI
from dotenv import load_dotenv
from flask import Flask
import threading
import sqlite3
from datetime import date

# Мини-веб-сервер для предотвращения "засыпания"
app = Flask(__name__)


@app.route('/')
def home():
    return "🤖 Бот работает!"


def run_flask():
    app.run(host='0.0.0.0', port=8080)


threading.Thread(target=run_flask, daemon=True).start()

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("TG_BOT_TOKEN"))
dp = Dispatcher()
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENAI_API_KEY")
)

SYSTEM_PROMPT = """Ты — эксперт по копирайтингу для маркетплейсов.
Создавай короткие, продающие описания товаров на русском языке.
Структура: 1) Заголовок, 2) 3–5 ключевых преимуществ, 3) Призыв к действию.
Не используй эмодзи. Тон: профессиональный, но дружелюбный. Длина: до 150 слов.

❗ ВАЖНО:
- Не выдумывай характеристики, которых нет в запросе
- Избегай канцеляризмов ("обеспечивает", "способствует") — пиши живым языком
- Для преимуществ используй формулу: "Выгода для покупателя + почему это важно"
"""

# 🛡️ СПИСОК АДМИНОВ (ВСТАВЬ СЮДА СВОЙ TELEGRAM ID)
ADMIN_IDS = {123456789}

# Инициализация БД
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    requests_count INTEGER DEFAULT 0,
    last_reset DATE
)""")
conn.commit()


def get_user_stats(user_id):
    today = date.today().isoformat()
    cursor.execute("SELECT requests_count, last_reset FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, requests_count, last_reset) VALUES (?, 0, ?)", (user_id, today))
        conn.commit()
        return 0
    else:
        count, last_date = row
        if last_date != today:
            cursor.execute("UPDATE users SET requests_count = 0, last_reset = ? WHERE user_id = ?", (today, user_id))
            conn.commit()
            return 0
        return count


def increment_request(user_id):
    cursor.execute("UPDATE users SET requests_count = requests_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я создаю продающие описания для товаров.\n\n"
        "Просто отправь название и ключевые характеристики товара.\n"
        "Пример: 'Кроссовки мужские, размер 42, дышащая сетка, подошва EVA, цвет чёрный'"
    )


@dp.message(F.text)
async def generate_description(message: types.Message):
    text = message.text.strip()

    if len(text) < 15:
        await message.answer("⚠️ Опиши товар подробнее. Минимум 15 символов.")
        return

    user_id = message.from_user.id

    # 🛡️ ПРОВЕРКА ЛИМИТА (Админы из ADMIN_IDS пропускаются без проверки!)
    if user_id not in ADMIN_IDS and get_user_stats(user_id) >= 3:
        await message.answer(
            "🔒 Лимит бесплатных запросов на сегодня исчерпан.\n"
            "💳 Безлимитный доступ: 99₽/неделя. Для оплаты напиши: @BiziRoman"
        )
        return

    increment_request(user_id)
    await message.answer("⏳ Генерирую описание...")

    try:
        # 🤖 НОВАЯ МОДЕЛЬ GLM 4.5 Air (free)
        response = client.chat.completions.create(
            model="z-ai/glm-4.5-air:free",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Товар: {text}"}
            ],
            temperature=0.6,
            max_tokens=600
        )
        result = response.choices[0].message.content

        if not result:
            await message.answer("❌ Нейросеть вернула пустой ответ. Попробуй еще раз.")
            return

        await message.answer(f"✅ Готово:\n\n{result}")
    except Exception as e:
        logging.error(f"Ошибка API: {e}")
        await message.answer("❌ Ошибка генерации. Попробуй позже или напиши /help")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 Как пользоваться:\n"
        "1. Отправь название + характеристики товара\n"
        "2. Получи готовое описание за 5–10 секунд\n"
        "3. При необходимости уточни детали в следующем сообщении\n\n"
        "💡 Совет: Чем точнее входные данные, тем лучше результат."
    )


if __name__ == "__main__":
    logging.info("Запуск бота...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())