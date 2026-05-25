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

# Мини-веб-сервер для предотвращения "засыпания" на бесплатном тарифе
app = Flask(__name__)


@app.route('/')
def home():
    return "🤖 Бот работает!"


def run_flask():
    app.run(host='0.0.0.0', port=8080)


# Запускаем веб-сервер в отдельном потоке
threading.Thread(target=run_flask, daemon=True).start()

# Загрузка переменных из .env
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация
bot = Bot(token=os.getenv("TG_BOT_TOKEN"))
dp = Dispatcher()
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENAI_API_KEY")
)

# Улучшенный системный промпт
SYSTEM_PROMPT = """Ты — профессиональный копирайтер для маркетплейсов Wildberries и Ozon.

ТВОЯ ЗАДАЧА:
Создавать продающие описания товаров, которые повышают конверсию в покупку.

СТРУКТУРА ОТВЕТА (строго соблюдай):
1. ЗАГОЛОВОК (1 строка, цепляющий, с ключевыми словами)
2. ПРЕИМУЩЕСТВА (3-5 пунктов, маркированный список)
3. ПРИЗЫВ К ДЕЙСТВИЮ (1 строка, мотивирующий)

ПРАВИЛА НАПИСАНИЯ:
✓ Пиши на русском языке, просто и понятно
✓ Используй конкретику: цифры, факты, выгоды
✓ Избегай общих фраз ("высокое качество", "надёжный")
✓ Не используй эмодзи и восклицательные знаки
✓ Тон: профессиональный, но дружелюбный
✓ Длина: 80-120 слов

ФОРМУЛА ПРЕИМУЩЕСТВ:
"Характеристика → Выгода для покупателя → Почему это важно"

ВАЖНО:
- Не выдумывай характеристики, которых нет в запросе
- Если данных мало — пиши максимально близко к запросу
- Адаптируй стиль под категорию товара"""

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
    """Красивое приветственное сообщение"""
    await message.answer(
        "👋 *Привет! Я ваш AI-помощник для маркетплейсов*\n\n"
        "✨ *Что я умею:*\n"
        "• Создаю продающие описания за 10 секунд\n"
        "• Учитываю SEO-требования WB и Ozon\n"
        "• Пишу живым языком, без воды\n\n"
        "📝 *Как использовать:*\n"
        "Просто отправьте название и характеристики товара\n"
        "*Пример:* «Кроссовки мужские, размер 42, сетка, EVA, чёрные»\n\n"
        "🎁 *3 описания бесплатно* каждый день!\n\n"
        " *Нужна помощь?* Напишите /help",
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def generate_description(message: types.Message):
    text = message.text.strip()

    # 1. Проверка длины
    if len(text) < 15:
        await message.answer(
            "⚠️ *Слишком короткое описание*\n\n"
            "Пожалуйста, укажите больше деталей:\n"
            "• Название товара\n"
            "• Основные характеристики\n"
            "• Материал, цвет, размер\n\n"
            "*Пример:* «Платье женское, хлопок, размер M, синее, летнее»",
            parse_mode="Markdown"
        )
        return

    # 2. Проверка лимита
    user_id = message.from_user.id
    if get_user_stats(user_id) >= 3:
        await message.answer(
            "🔒 *Лимит на сегодня исчерпан*\n\n"
            "💳 *Тарифы:*\n"
            "• Неделя — 99₽ (безлимит)\n"
            "• Месяц — 299₽ (безлимит + приоритет)\n\n"
            " Для подключения напишите: @ТвойНикнейм",
            parse_mode="Markdown"
        )
        return

    # 3. Фиксируем запрос
    increment_request(user_id)

    # 4. Генерация
    await message.answer("⏳ *Генерирую описание...*")

    try:
        # Исправленная модель и корректный формат запроса
        response = client.chat.completions.create(
            model="openrouter/auto",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Товар: {text}"}
            ],
            temperature=0.7,
            max_tokens=500
        )
        result = response.choices[0].message.content
        await message.answer(f"✅ *Готово!*\n\n{result}", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка API: {e}")
        await message.answer(
            " *Произошла ошибка*\n\n"
            "Попробуйте:\n"
            "• Проверить подключение к интернету\n"
            "• Повторить через минуту\n"
            "• Написать /help для поддержки"
        )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Справка"""
    await message.answer(
        "📖 *Как получить идеальное описание:*\n\n"
        "1️⃣ *Будьте конкретны:*\n"
        "❌ «Кроссовки хорошие»\n"
        "✅ «Кроссовки Nike Air, размер 43, кожа, белые, для бега»\n\n"
        "2️⃣ *Укажите ключевые характеристики:*\n"
        "• Материал\n"
        "• Размер/цвет\n"
        "• Назначение\n"
        "• Особенности\n\n"
        "3️⃣ *Примеры запросов:*\n"
        "• «Платье летнее, шифон, размер S, красное, с цветочным принтом»\n"
        "• «Наушники беспроводные, шумоподавление, 30ч работы, чёрные»\n\n"
        " *Совет:* Чем больше деталей, тем точнее описание!"
    )


if __name__ == "__main__":
    logging.info("Запуск бота...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())