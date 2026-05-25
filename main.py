import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from openai import OpenAI
from dotenv import load_dotenv
from flask import Flask
import threading
import sqlite3
from datetime import date, timedelta
import re
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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

SYSTEM_PROMPT = """Ты — профессиональный копирайтер для маркетплейсов (WB, Ozon, Wildberries, Яндекс Маркет). 
Твоя задача — написать продающее текстовое описание товара на основе сырого ввода пользователя.

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
— Завершай ответ полностью, не обрывай на полуслове
— НИКОГДА не добавляй мета-комментарии типа "Продолжение:", "Далее:", "**продолжение**"
— Выдавай ТОЛЬКО готовый текст описания без пояснений и подписей
"""

# Читаем ADMIN_IDS из переменных окружения
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(id.strip()) for id in admin_ids_str.split(",") if id.strip()}

conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    requests_count INTEGER DEFAULT 0,
    last_reset DATE
)""")
conn.commit()
cursor.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    plan TEXT,
    expires_at DATE,
    created_at DATE
)""")
conn.commit()

cursor.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    plan TEXT,
    days INTEGER,
    used INTEGER DEFAULT 0
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


def get_subscription(user_id):
    """Получить активную подписку пользователя"""
    today = date.today().isoformat()
    cursor.execute("SELECT plan, expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row[1] >= today:
        return {"plan": row[0], "expires_at": row[1]}
    return None


def activate_subscription(user_id, plan, days):
    """Активировать подписку"""
    today = date.today()
    expires = today + timedelta(days=days)
    cursor.execute("""
        INSERT OR REPLACE INTO subscriptions (user_id, plan, expires_at, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, plan, expires.isoformat(), today.isoformat()))
    conn.commit()


def generate_promo_code(plan, days):
    """Генерация промокода (только для админа)"""
    import secrets
    code = f"{plan.upper()}-{secrets.token_hex(4).upper()}"
    cursor.execute("""
        INSERT INTO promo_codes (code, plan, days, used)
        VALUES (?, ?, ?, 0)
    """, (code, plan, days))
    conn.commit()
    return code


def use_promo_code(user_id, code):
    """Использовать промокод"""
    cursor.execute("SELECT plan, days, used FROM promo_codes WHERE code = ?", (code,))
    row = cursor.fetchone()
    if not row:
        return False, "Промокод не найден"
    if row[2] == 1:
        return False, "Промокод уже использован"

    plan, days, _ = row
    activate_subscription(user_id, plan, days)
    cursor.execute("UPDATE promo_codes SET used = 1 WHERE code = ?", (code,))
    conn.commit()
    return True, f"Подписка {plan} активирована на {days} дней!"

# Шаблоны мусорных вставок, которые модель иногда добавляет при продолжении
GARBAGE_PATTERNS = [
    r"\*\*[^*]*продолжение[^*]*\*\*",
    r"\*[^*]*продолжение[^*]*\*",
    r"^[\s]*Продолжение[:：].*?\n",
    r"^[\s]*Далее[:：].*?\n",
    r"^[\s]*Вот остаток[:：].*?\n",
    r"^[\s]*Остальная часть[:：].*?\n",
    r"^[\s]*Продолжу[:：].*?\n",
    r"^[\s]*Продолжаем[:：].*?\n",
    r"^[\s]*Ключевые слова[:：].*?\n(?=\s*$)",  # пустой блок ключевых слов
    r"^[\s]*---+[\s]*$",
    r"^\s*[\*\-]{3,}\s*$",
]


def clean_text(text: str) -> str:
    """Удаляет мусорные вставки от автодогенерации"""
    if not text:
        return text
    for pattern in GARBAGE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
    # Схлопываем множественные пустые строки
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(\nКлючевые слова[:：])", r"\n\n\1", text)  # отдельная строка перед SEO
    return text.strip()


async def generate_with_continuation(system_prompt, user_text, max_attempts=3):
    """
    Генерирует описание с автодополнением и очисткой от мусора.
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
                max_tokens=2000  # ← увеличено с 1000 до 2000
            )

            choice = response.choices[0]
            chunk = choice.message.content or ""
            full_text += chunk

            finish_reason = choice.finish_reason
            logging.info(f"Attempt {attempt + 1}: finish_reason={finish_reason}, length={len(chunk)}")

            if finish_reason == "stop":
                break

            if finish_reason == "length":
                logging.warning(f"Response truncated, continuing (attempt {attempt + 1})")
                messages.append({"role": "assistant", "content": chunk})
                # СТРОГИЙ промпт для продолжения — запрещаем мета-комментарии
                messages.append({
                    "role": "user",
                    "content": (
                        "Продолжи текст С ТОГО МЕСТА, где остановился. "
                        "СТРОГИЕ ПРАВИЛА:\n"
                        "- НЕ добавляй заголовков, подзаголовков и комментариев\n"
                        "- НЕ пиши слова 'продолжение', 'далее', 'вот остаток'\n"
                        "- НЕ повторяй уже написанное\n"
                        "- НЕ ставь разделители\n"
                        "- Просто продолжи последнюю фразу без пробелов и пояснений\n"
                        "- Начни с середины оборванной фразы"
                    )
                })
                continue

            break

        except Exception as e:
            logging.error(f"API error on attempt {attempt + 1}: {e}")
            if attempt == 0:
                raise
            break

    # 🧹 ОЧИСТКА ОТ МУСОРА
    return clean_text(full_text)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "✍️ *Генератор текстовых карточек товара PRO*\n\n"
        "_Нейросеть, которая пишет продающие описания за 1 минуту_\n\n"
        "Привет, селлер! Устали ломать голову над заголовками и характеристиками для Ozon / Wildberries? "
        "Просто опишите товар словами — я сделаю всё остальное.\n\n"
        "🤖 *Я превращаю ваш текст в готовую карточку для маркетплейса.*\n\n"
        "*Что я сгенерирую на основе вашего текста:*\n"
        "✅ Кликбейтный заголовок (до 70 символов с ключами)\n"
        "✅ SEO-описание (для поиска внутри WB/Ozon)\n"
        "✅ Продающее УТП (блок «Почему выберут вас»)\n"
        "✅ Список характеристик (техничка + выгода)\n"
        "✅ Готовый текст для копирования в карточку\n\n"
        "🚀 *Как это работает:*\n"
        "`Ваше сырое описание` ➡️ `AI` ➡️ `Готовая карточка`\n\n"
        "*Просто напишите мне название или пару слов о товаре*\n"
        "_(например: «стеклянный чайник 1.5 л с подсветкой»), и я начну!_ 🍵\n\n"
        "—\n"
        "_Экономит 2 часа в день у селлеров на WB, Ozon, Yandex.Market_",
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def generate_description(message: types.Message):
    text = message.text.strip()

    if len(text) < 15:
        await message.answer("⚠️ Опиши товар подробнее. Минимум 15 символов.")
        return

    # 2. ПРОВЕРКА ПОДПИСКИ И ЛИМИТА
    user_id = message.from_user.id
    subscription = get_subscription(user_id)

    # Если есть активная подписка или это админ — пропускаем проверку лимита
    if user_id not in ADMIN_IDS and not subscription and get_user_stats(user_id) >= 3:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Купить безлимит", callback_data="buy_unlimited")]
        ])
        await message.answer(
            "🔒 *Лимит бесплатных запросов на сегодня исчерпан*\n\n"
            "Оформите подписку для безлимитного доступа:\n"
            "📅 Неделя — 99₽\n"
            "📆 Месяц — 299₽\n\n"
            "Нажмите кнопку ниже для покупки:",
            reply_markup=keyboard,
            parse_mode="Markdown"
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


@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя — 99₽", callback_data="plan_week")],
        [InlineKeyboardButton(text="📆 Месяц — 299₽", callback_data="plan_month")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

    await message.answer(
        "💳 *Выберите тариф:*\n\n"
        "📅 *Неделя (7 дней)* — 99₽\n"
        "📆 *Месяц (30 дней)* — 299₽ (выгода 40%!)\n\n"
        "После выбора тарифа вы получите инструкцию по оплате.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@dp.callback_query(F.data.startswith("plan_"))
async def process_plan(callback: types.CallbackQuery):
    plan = callback.data.replace("plan_", "")

    if plan == "week":
        amount = "99₽"
        days = 7
    elif plan == "month":
        amount = "299₽"
        days = 30
    else:
        await callback.message.edit_text("❌ Неизвестный тариф")
        return

    await callback.message.edit_text(
        f"💳 *Оплата тарифа «{plan}» ({days} дней) — {amount}*\n\n"
        "📲 *Способы оплаты:*\n"
        "1. Перевод на карту: `2200 1234 5678 9012` (Сбербанк)\n"
        "2. ЮMoney: `41001234567890`\n"
        "3. СБП по номеру: `+7 (999) 123-45-67`\n\n"
        "После оплаты напишите @ТвойНикнейм и отправьте скриншот перевода.\n"
        "Вы получите промокод для активации в течение 10 минут.",
        parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_buy(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ Покупка отменена")
    await callback.answer()


@dp.message(Command("activate"))
async def cmd_activate(message: types.Message):
    await message.answer(
        "🔑 Введите промокод (например: `WEEK-A1B2C3D4`)",
        parse_mode="Markdown"
    )


@dp.message(F.text.regexp(r"^[A-Z]+-[A-Z0-9]+$"))
async def process_promo(message: types.Message):
    code = message.text.strip().upper()
    success, msg = use_promo_code(message.from_user.id, code)

    if success:
        await message.answer(f"✅ {msg}", parse_mode="Markdown")
    else:
        await message.answer(f"❌ {msg}")


@dp.message(Command("genpromo"))
async def cmd_genpromo(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    # Формат: /genpromo week 7 или /genpromo month 30
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: `/genpromo week 7` или `/genpromo month 30`")
        return

    plan = args[1]
    days = int(args[2])
    code = generate_promo_code(plan, days)

    await message.answer(
        f"✅ Промокод создан:\n`{code}`\n\n"
        f"План: {plan}, Срок: {days} дней\n"
        "Отправьте этот код клиенту после оплаты.",
        parse_mode="Markdown"
    )


@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    user_id = message.from_user.id
    subscription = get_subscription(user_id)

    if user_id in ADMIN_IDS:
        await message.answer("👑 *Статус: Администратор (безлимит)*", parse_mode="Markdown")
    elif subscription:
        await message.answer(
            f"✅ *Активная подписка*\n\n"
            f"Тариф: {subscription['plan']}\n"
            f"Действует до: {subscription['expires_at']}\n\n"
            f"Запросов сегодня: безлимит",
            parse_mode="Markdown"
        )
    else:
        today_count = get_user_stats(user_id)
        await message.answer(
            f"📊 *Ваш статус*\n\n"
            f"Бесплатных запросов сегодня: {3 - today_count} из 3\n"
            f"Подписка: не активна\n\n"
            f"Для безлимита используйте /buy",
            parse_mode="Markdown"
        )


if __name__ == "__main__":
    logging.info("Запуск бота...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())