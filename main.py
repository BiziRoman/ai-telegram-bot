import os
import logging
import re
import secrets
import sqlite3
import threading
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
from dotenv import load_dotenv
from flask import Flask

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
logging.basicConfig(level=logging.INFO)

# Инициализация
bot = Bot(token=os.getenv("TG_BOT_TOKEN"))
dp = Dispatcher()
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENAI_API_KEY")
)

# 🔐 БЕЗОПАСНОСТЬ: ADMIN_IDS читается из переменных окружения
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {int(id.strip()) for id in admin_ids_str.split(",") if id.strip()}

SYSTEM_PROMPT = """Ты — профессиональный копирайтер для маркетплейсов (WB, Ozon, Wildberries, Яндекс Маркет). 
Твоя задача — написать продающее текстовое описание товара на основе сырого ввода пользователя.

Пользователь напишет название или короткое описание товара. Ты должен вернуть строго по этой структуре:
1. Заголовок (до 70 символов, с ключевыми словами и выгодой)
2. Короткое УТП (одно предложение, чем товар лучше аналогов)
3. Список характеристик (5-7 пунктов с цифрами, материалами, размерами)
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

# --- БАЗА ДАННЫХ ---
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    requests_count INTEGER DEFAULT 0,
    last_reset DATE
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    plan TEXT,
    expires_at DATE,
    created_at DATE
)""")

cursor.execute("""CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    plan TEXT,
    days INTEGER,
    used INTEGER DEFAULT 0
)""")
conn.commit()

# --- ФУНКЦИИ БД ---
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
    today = date.today().isoformat()
    cursor.execute("SELECT plan, expires_at FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row[1] >= today:
        return {"plan": row[0], "expires_at": row[1]}
    return None

def activate_subscription(user_id, plan, days):
    today = date.today()
    expires = today + timedelta(days=days)
    cursor.execute("""
        INSERT OR REPLACE INTO subscriptions (user_id, plan, expires_at, created_at)
        VALUES (?, ?, ?, ?)
    """, (user_id, plan, expires.isoformat(), today.isoformat()))
    conn.commit()

def generate_promo_code(plan, days):
    code = f"{plan.upper()}-{secrets.token_hex(4).upper()}"
    cursor.execute("INSERT INTO promo_codes (code, plan, days, used) VALUES (?, ?, ?, 0)", (code, plan, days))
    conn.commit()
    return code

def use_promo_code(user_id, code):
    cursor.execute("SELECT plan, days, used FROM promo_codes WHERE code = ?", (code.upper(),))
    row = cursor.fetchone()
    if not row:
        return False, "Промокод не найден"
    if row[2] == 1:
        return False, "Промокод уже использован"
    plan, days, _ = row
    activate_subscription(user_id, plan, days)
    cursor.execute("UPDATE promo_codes SET used = 1 WHERE code = ?", (code.upper(),))
    conn.commit()
    return True, f"Подписка {plan} активирована на {days} дней!"

# --- ОЧИСТКА ОТ МУСОРА И АВТОДОПОЛНЕНИЕ ---
GARBAGE_PATTERNS = [
    r"\*\*[^*]*продолжение[^*]*\*\*", r"\*[^*]*продолжение[^*]*\*",
    r"^[\s]*Продолжение[:：].*?\n", r"^[\s]*Далее[:：].*?\n",
    r"^[\s]*Вот остаток[:：].*?\n", r"^[\s]*Остальная часть[:：].*?\n",
    r"^[\s]*Продолжу[:：].*?\n", r"^[\s]*Продолжаем[:：].*?\n",
    r"^[\s]*---+[\s]*$", r"^\s*[\*\-]{3,}\s*$"
]

def clean_text(text: str) -> str:
    if not text: return text
    for pattern in GARBAGE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

async def generate_with_continuation(system_prompt, user_text, max_attempts=3):
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
                max_tokens=2000
            )
            choice = response.choices[0]
            chunk = choice.message.content or ""
            full_text += chunk
            if choice.finish_reason == "stop": break
            if choice.finish_reason == "length":
                messages.append({"role": "assistant", "content": chunk})
                messages.append({"role": "user", "content": "Продолжи текст С ТОГО МЕСТА, где остановился. НЕ добавляй заголовков, НЕ пиши 'продолжение', НЕ повторяй написанное. Просто продолжи последнюю фразу."})
                continue
            break
        except Exception as e:
            logging.error(f"API error attempt {attempt+1}: {e}")
            if attempt == 0: raise
            break
    return clean_text(full_text)

# --- ОБРАБОТЧИКИ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "️ *Генератор текстовых карточек товара PRO*\n\n"
        "_Нейросеть, которая пишет продающие описания за 1 минуту_\n\n"
        "Привет, селлер! Устали ломать голову над заголовками для WB/Ozon? Просто опишите товар словами.\n\n"
        "🤖 *Я превращаю ваш текст в готовую карточку:*\n"
        "✅ Кликбейтный заголовок (до 70 символов)\n"
        "✅ SEO-описание и УТП\n"
        "✅ Список характеристик + выгоды\n"
        "✅ Ключевые слова для поиска\n\n"
        " *Как это работает:*\n"
        "`Ваш текст` ➡️ `AI` ➡️ `Готовая карточка`\n\n"
        "*Напишите название или пару слов о товаре, и я начну!* 🍵",
        parse_mode="Markdown"
    )

@dp.message(Command("buy"))
async def cmd_buy(message: types.Message):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя — 99₽", callback_data="plan_week")],
        [InlineKeyboardButton(text="📆 Месяц — 299₽", callback_data="plan_month")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])
    await message.answer("💳 *Выберите тариф:*", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("plan_"))
async def process_plan(callback: types.CallbackQuery):
    plan = callback.data.replace("plan_", "")
    amount = "99₽" if plan == "week" else "299₽"
    days = 7 if plan == "week" else 30
    await callback.message.edit_text(
        f"💳 *Тариф «{plan}» ({days} дней) — {amount}*\n\n"
        "📲 *Оплата:*\n"
        "• Карта: `2200 1234 5678 9012`\n"
        "• СБП/ЮMoney: `+7 (999) 123-45-67`\n\n"
        "После оплаты напишите @BiziRoman → получите промокод → введите `/activate КОД`",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "cancel")
async def cancel_buy(callback: types.CallbackQuery):
    await callback.message.edit_text("❌ Покупка отменена")
    await callback.answer()

@dp.message(Command("activate"))
async def cmd_activate(message: types.Message):
    await message.answer("🔑 Введите промокод (например: `WEEK-A1B2C3D4`)", parse_mode="Markdown")

@dp.message(F.text.regexp(r"^[A-Z]+-[A-Z0-9]+$"))
async def process_promo(message: types.Message):
    success, msg = use_promo_code(message.from_user.id, message.text.strip())
    await message.answer(f"{'✅' if success else '❌'} {msg}", parse_mode="Markdown")

@dp.message(Command("genpromo"))
async def cmd_genpromo(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Использование: `/genpromo week 7` или `/genpromo month 30`")
        return
    code = generate_promo_code(args[1], int(args[2]))
    await message.answer(f"✅ Промокод создан:\n`{code}`\n\nПлан: {args[1]}, Срок: {args[2]} дней", parse_mode="Markdown")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    sub = get_subscription(message.from_user.id)
    if message.from_user.id in ADMIN_IDS:
        txt = "👑 *Статус: Администратор (безлимит)*"
    elif sub:
        txt = f"✅ *Активная подписка*\nТариф: {sub['plan']}\nДействует до: {sub['expires_at']}"
    else:
        left = 3 - get_user_stats(message.from_user.id)
        txt = f" *Статус*\nБесплатных запросов сегодня: {max(0, left)} из 3\nПодписка: не активна\n\nДля безлимита: `/buy`"
    await message.answer(txt, parse_mode="Markdown")

@dp.message(F.text)
async def generate_description(message: types.Message):
    text = message.text.strip()
    if len(text) < 15:
        await message.answer("⚠️ Опишите товар подробнее. Минимум 15 символов.")
        return

    user_id = message.from_user.id
    if user_id not in ADMIN_IDS and not get_subscription(user_id) and get_user_stats(user_id) >= 3:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Купить безлимит", callback_data="buy_unlimited")]])
        await message.answer("🔒 *Лимит исчерпан*\n\nОформите подписку для безлимита:", reply_markup=kb, parse_mode="Markdown")
        return

    increment_request(user_id)
    await message.answer("⏳ Генерирую карточку товара...")

    try:
        result = await generate_with_continuation(SYSTEM_PROMPT, text)
        if not result:
            await message.answer("❌ Нейросеть вернула пустой ответ. Попробуйте ещё раз.")
            return
        if len(result) <= 4000:
            await message.answer(f"✅ Готово:\n\n{result}")
        else:
            for i in range(0, len(result), 4000):
                prefix = "✅ Готово (часть 1):\n\n" if i == 0 else ""
                await message.answer(f"{prefix}{result[i:i+4000]}")
    except Exception as e:
        logging.error(f"Ошибка генерации: {e}")
        await message.answer("❌ Ошибка генерации. Попробуйте позже или напишите /help")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer("📖 Отправьте название товара → получите готовую карточку для WB/Ozon за 10 секунд.\n💡 Чем точнее данные, тем лучше результат.", parse_mode="Markdown")

if __name__ == "__main__":
    logging.info("Запуск бота...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())