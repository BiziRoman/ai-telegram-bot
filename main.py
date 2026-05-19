# main.py
import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from openai import OpenAI
from dotenv import load_dotenv

from flask import Flask
import threading

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

# Системный промпт (можно менять по ходу)
SYSTEM_PROMPT = """Ты — эксперт по копирайтингу для маркетплейсов.
Создавай короткие, продающие описания товаров на русском языке.
Структура: 1) Заголовок, 2) 3–5 ключевых преимуществ, 3) Призыв к действию.
Не используй эмодзи. Тон: профессиональный, но дружелюбный. Длина: до 150 слов.

❗ ВАЖНО:
- Не выдумывай характеристики, которых нет в запросе
- Избегай канцеляризмов ("обеспечивает", "способствует") — пиши живым языком
- Для преимуществ используй формулу: "Выгода для покупателя + почему это важно"
"""


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

    await message.answer("⏳ Генерирую описание...")

    try:
        response = client.chat.completions.create(
            model="openrouter/auto",  # <-- автовыбор рабочей бесплатной модели
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Товар: {message.text}"}
            ],
            temperature=0.7,
            max_tokens=400
        )
        result = response.choices[0].message.content
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