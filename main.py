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
app = Flask(__name__)  # <-- Исправлено: добавлены подчеркивания


@app.route('/')
def home():
    return "🤖 Бот работает!"


def run_flask():
    app.run(host='0.0.0.0', port=8080)


# Запускаем веб-сервер в отдельном потоке
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
- Избегай канцеляризмов — пиши живым языком
"""

# --- БАЗА ДАННЫХ ---
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


# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        " *Привет! Я AI-помощник для маркетплейсов*\n\n"
        "✨ Создаю продающие описания за 10 секунд.\n"
        "📝 Просто отправь название и характеристики товара.\n\n"
        "🎁 *3 описания бесплатно* каждый день!"
    )


@dp.message(F.text)
async def generate_description(message: types.Message):
    text = message.text.strip()

    if len(text) < 15:
        await message.answer("⚠️ Опиши товар подробнее. Минимум 15 символов.")
        return

    user_id = message.from_user.id
    if get_user_stats(user_id) >= 3:
        await message.answer(
            "🔒 Лимит на сегодня исчерпан.\n"
            "💳 Безлимит: 99₽/неделя. Для оплаты напиши: @BiziRoman"
        )
        return

    increment_request(user_id)
    await message.answer("⏳ Генерирую описание...")

    try:
        # ИСПРАВЛЕННЫЙ ЗАПРОС (без лишних пробелов!)
        response = client.chat.completions.create(
            model="deepseek/deepseek-v4-flash:free",  # ← НОВАЯ МОДЕЛЬ
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Товар: {text}"}
            ],
            temperature=0.6,  # Для GLM лучше 0.6
            max_tokens=500  # Увеличь лимит для лучшего качества
        )

        # Защита от None
        result = response.choices[0].message.content
        if not result:
            await message.answer("❌ Нейросеть вернула пустой ответ. Попробуй еще раз.")
            return

        await message.answer(f"✅ *Готово!*\n\n{result}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Ошибка API: {e}")
        await message.answer("❌ Ошибка генерации. Попробуй позже или напиши /help")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("📖 Отправь товар → получи описание.\n💡 Чем точнее данные, тем лучше результат.")


# ИСПРАВЛЕННЫЙ ЗАПУСК
if __name__ == "__main__":
    logging.info("Запуск бота...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())