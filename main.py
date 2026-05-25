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

SYSTEM_PROMPT = """
Ты — профессиональный копирайтер для маркетплейсов (WB, Ozon, Wildberries, Яндекс Маркет). Твоя задача — написать продающее текстовое описание товара на основе сырого ввода пользователя.

Пользователь напишет название или короткое описание товара. Ты должен вернуть строго по этой структуре:

1. Заголовок (до 70 символов, с ключевыми словами и выгодой)
2. Короткое УТП (одно предложение, чем товар лучше аналогов)
3. Список характеристик (5-7 пунктов с цифрами, материалами, размерами, если применимо)
4. Продающее описание (3-4 коротких абзаца, акцент на решение боли клиента)
5. Ключевые слова (через запятую, для SEO внутри маркетплейса)

Правила:
— Не писать «наш товар», «компания предлагает»
— Писать «вы» и «ваш»
— Не врать, не преувеличивать
— Использовать эмодзи умеренно (максимум 3 на весь текст)
— Заголовок — без эмодзи
"""

ADMIN_IDS = {2104462484}  # ← ВСТАВЬ СВОЙ TELEGRAM ID

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


async def generate_with_continuation(system_prompt, user_text, max_attempts=3):
    """
    Генерирует описание с автоматическим продолжением, если ответ обрезался.
    Возвращает полный текст.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Товар: {user_text}"}
    ]

    full_text = ""

    for attempt in range(max_attempts):
        try:
            response = client.chat.completions.create(
                model="z-ai/glm-4.5-air:free",
                messages=messages,
                temperature=0.6,
                max_tokens=1000  # ← увеличено с 400 до 1000
            )

            choice = response.choices[0]
            chunk = choice.message.content or ""
            full_text += chunk

            finish_reason = choice.finish_reason
            logging.info(f"Attempt {attempt + 1}: finish_reason={finish_reason}, length={len(chunk)}")

            # Если модель завершила ответ нормально — выходим
            if finish_reason == "stop":
                break

            # Если обрезалась по длине — просим продолжить
            if finish_reason == "length":
                logging.warning(f"Response truncated, continuing (attempt {attempt + 1})")
                messages.append({"role": "assistant", "content": chunk})
                messages.append({
                    "role": "user",
                    "content": "Продолжи с того места, где остановился. Не повторяй начало."
                })
                continue

            # Другие причины (content_filter и т.п.) — выходим
            break

        except Exception as e:
            logging.error(f"API error on attempt {attempt + 1}: {e}")
            if attempt == 0:
                raise
            break

    return full_text.strip()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        """
✍️ *Генератор текстовых карточек товара PRO*

_Нейросеть, которая пишет продающие описания за 1 минуту_

Привет, селлер! Устали ломать голову над заголовками и характеристиками для Ozon / Wildberries? Просто опишите товар словами — я сделаю всё остальное.

🤖 *Я превращаю ваш текст в готовую карточку для маркетплейса.*

*Что я сгенерирую на основе вашего текста:*
✅ Кликбейтный заголовок (до 60 символов с ключами)
✅ SEO-описание (для поиска внутри WB/Ozon)
✅ Продающее УТП (блок "Почему выберут вас")
✅ Список характеристик (техничка + выгода)
✅ Готовый HTML или просто текст — копируйте и вставляйте

🚀 *Как это работает:*
`Ваше сырое описание` ➡️ `AI` ➡️ `Готовая карточка`

*Просто напишите мне название или пару слов о товаре*
*(например: "стеклянный чайник 1.5 л с подсветкой"), и я начну!* 🍵

—
_Экономит 2 часа в день у селлеров на WB, Ozon, Yandex.Market_
"""
    )


@dp.message(F.text)
async def generate_description(message: types.Message):
    text = message.text.strip()

    if len(text) < 15:
        await message.answer("⚠️ Опиши товар подробнее. Минимум 15 символов.")
        return

    user_id = message.from_user.id
    if user_id not in ADMIN_IDS and get_user_stats(user_id) >= 3:
        await message.answer(
            "🔒 Лимит бесплатных запросов на сегодня исчерпан.\n"
            "💳 Безлимитный доступ: 99₽/неделя. Для оплаты напиши: @BiziRoman"
        )
        return

    increment_request(user_id)
    await message.answer("⏳ Генерирую описание...")

    try:
        result = await generate_with_continuation(SYSTEM_PROMPT, text)

        if not result:
            await message.answer("❌ Нейросеть вернула пустой ответ. Попробуй ещё раз.")
            return

        # Разбиваем на части, если ответ длиннее 4000 символов (лимит Telegram = 4096)
        if len(result) <= 4000:
            await message.answer(f"✅ Готово:\n\n{result}")
        else:
            # Отправляем по частям
            for i in range(0, len(result), 4000):
                chunk = result[i:i + 4000]
                if i == 0:
                    await message.answer(f"✅ Готово (часть 1):\n\n{chunk}")
                else:
                    await message.answer(chunk)

    except Exception as e:
        logging.error(f"Ошибка генерации: {e}")
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