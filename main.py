import os
import logging
import re
import secrets
import sqlite3
import threading
import asyncio
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from openai import OpenAI
from dotenv import load_dotenv
from flask import Flask

# --- Веб-сервер для пробуждения Render ---
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

# 🔐 ADMIN_IDS из переменных окружения
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
cursor.execute(
    """CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, requests_count INTEGER DEFAULT 0, last_reset DATE)""")
cursor.execute(
    """CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, plan TEXT, expires_at DATE, created_at DATE)""")
cursor.execute(
    """CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, plan TEXT, days INTEGER, used INTEGER DEFAULT 0)""")
conn.commit()


def get_user_stats(user_id):
    today = date.today().isoformat()
    cursor.execute("SELECT requests_count, last_reset FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO users (user_id, requests_count, last_reset) VALUES (?, 0, ?)", (user_id, today))
        conn.commit()
        return 0
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
    return {"plan": row[0], "expires_at": row[1]} if row and row[1] >= today else None


def activate_subscription(user_id, plan, days):
    today = date.today()
    expires = today + timedelta(days=days)
    cursor.execute("INSERT OR REPLACE INTO subscriptions (user_id, plan, expires_at, created_at) VALUES (?, ?, ?, ?)",
                   (user_id, plan, expires.isoformat(), today.isoformat()))
    conn.commit()


def generate_promo_code(plan, days):
    code = f"{plan.upper()}-{secrets.token_hex(4).upper()}"
    cursor.execute("INSERT INTO promo_codes (code, plan, days, used) VALUES (?, ?, ?, 0)", (code, plan, days))
    conn.commit()
    return code


def use_promo_code(user_id, code):
    cursor.execute("SELECT plan, days, used FROM promo_codes WHERE code = ?", (code.upper(),))
    row = cursor.fetchone()
    if not row: return False, "Промокод не найден"
    if row[2] == 1: return False, "Промокод уже использован"
    plan, days, _ = row
    activate_subscription(user_id, plan, days)
    cursor.execute("UPDATE promo_codes SET used = 1 WHERE code = ?", (code.upper(),))
    conn.commit()
    return True, f"Подписка {plan} активирована на {days} дней!"


# --- ОЧИСТКА И ГЕНЕРАЦИЯ ---
GARBAGE_PATTERNS = [r"\*\*[^*]*продолжение[^*]*\*\*", r"\*[^*]*продолжение[^*]*\*",
                    r"^[\s]*Продолжение[:：].*?\n", r"^[\s]*Далее[:：].*?\n",
                    r"^[\s]*Вот остаток[:：].*?\n", r"^[\s]*Остальная часть[:：].*?\n",
                    r"^[\s]*---+[\s]*$", r"^\s*[\*\-]{3,}\s*$"]


def clean_text(text: str) -> str:
    if not text: return text
    for p in GARBAGE_PATTERNS: text = re.sub(p, "", text, flags=re.IGNORECASE | re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def generate_with_continuation(system_prompt, user_text, max_attempts=3):
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": f"Товар: {user_text}"}]
    full_text = ""
    for attempt in range(max_attempts):
        try:
            response = client.chat.completions.create(model="z-ai/glm-4.5-air:free", messages=messages, temperature=0.6,
                                                      max_tokens=2000)
            choice = response.choices[0]
            chunk = choice.message.content or ""
            full_text += chunk
            if choice.finish_reason == "stop": break
            if choice.finish_reason == "length":
                messages.append({"role": "assistant", "content": chunk})
                messages.append({"role": "user",
                                 "content": "Продолжи текст С ТОГО МЕСТА, где остановился. НЕ добавляй заголовков, НЕ пиши 'продолжение', НЕ повторяй написанное. Просто продолжи последнюю фразу."})
                continue
            break
        except Exception as e:
            logging.error(f"API error attempt {attempt + 1}: {e}")
            if attempt == 0: raise
            break
    return clean_text(full_text)


# --- UI: СОСТОЯНИЯ И КЛАВИАТУРЫ ---
user_states = {}  # user_id: "waiting_promo"


def main_menu_kb(user_id):
    kb = [[
        InlineKeyboardButton(text=" Купить подписку", callback_data="menu_buy"),
        InlineKeyboardButton(text=" Ввести промокод", callback_data="menu_promo")
    ], [
        InlineKeyboardButton(text="📊 Мой статус", callback_data="menu_status"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="menu_help")
    ]]
    if user_id in ADMIN_IDS:
        kb.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


back_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="↩️ В главное меню", callback_data="menu_main")]])


# --- ОБРАБОТЧИКИ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_states.pop(message.from_user.id, None)
    await message.answer(
        "✍️ *Генератор карточек товара PRO*\n\n"
        "_Нейросеть, которая пишет продающие описания за 1 минуту_\n\n"
        "Просто отправьте название товара, и я создам готовую карточку для WB/Ozon.\n"
        "Внизу — кнопки для управления подпиской и статусом.",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="Markdown"
    )


@dp.callback_query(F.data == "menu_main")
async def go_main(callback: types.CallbackQuery):
    user_states.pop(callback.from_user.id, None)
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_kb(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "menu_buy")
async def buy_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя — 99₽", callback_data="plan_week")],
        [InlineKeyboardButton(text="📆 Месяц — 299₽", callback_data="plan_month")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="menu_main")]
    ])
    await callback.message.edit_text("💳 *Выберите тариф:*", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("plan_"))
async def plan_selected(callback: types.CallbackQuery):
    plan = callback.data.replace("plan_", "")
    amount, days = ("99₽", 7) if plan == "week" else ("299₽", 30)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="menu_main")]])
    await callback.message.edit_text(
        f"💳 *Тариф «{plan}» ({days} дней) — {amount}*\n\n"
        "📲 *Оплата:*\n"
        "• Карта: `2200 1234 5678 9012`\n"
        "• СБП/ЮMoney: `+7 (999) 123-45-67`\n\n"
        "После оплаты напишите @ТвойНикнейм → получите промокод → введите его через кнопку «🔑 Ввести промокод»",
        reply_markup=kb, parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "menu_promo")
async def promo_input(callback: types.CallbackQuery):
    user_states[callback.from_user.id] = "waiting_promo"
    await callback.message.edit_text("🔑 *Отправьте промокод следующим сообщением*\n\n(например: `WEEK-A1B2C3D4`)",
                                     reply_markup=back_kb, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "menu_status")
async def status_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if uid in ADMIN_IDS:
        txt = "👑 *Статус: Администратор (безлимит)*"
    elif (sub := get_subscription(uid)):
        txt = f"✅ *Активная подписка*\nТариф: {sub['plan']}\nДействует до: {sub['expires_at']}"
    else:
        left = 3 - get_user_stats(uid)
        txt = f"📊 *Ваш статус*\nБесплатных запросов сегодня: {max(0, left)} из 3\nПодписка: не активна"
    await callback.message.edit_text(txt, reply_markup=back_kb, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "menu_help")
async def help_menu(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📖 *Как пользоваться:*\n"
        "1. Отправьте название + характеристики товара\n"
        "2. Получите готовое описание за 5–10 сек\n"
        "3. Используйте кнопки ниже для управления подпиской",
        reply_markup=back_kb, parse_mode="Markdown"
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Создать неделю", callback_data="gen_week")],
        [InlineKeyboardButton(text="📆 Создать месяц", callback_data="gen_month")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="menu_main")]
    ])
    await callback.message.edit_text("⚙️ *Админ-панель*\nВыберите тип промокода:", reply_markup=kb,
                                     parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("gen_"))
async def generate_promo(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    plan_type = callback.data.replace("gen_", "")
    days = 7 if plan_type == "week" else 30
    code = generate_promo_code(plan_type, days)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Скопировать", switch_inline_query_current_chat=code)],
        [InlineKeyboardButton(text="↩️ В админку", callback_data="admin_panel")]
    ])
    await callback.message.edit_text(
        f"✅ *Промокод создан:*\n`{code}`\n\nПлан: {plan_type} | Срок: {days} дней\nОтправьте код клиенту после оплаты.",
        reply_markup=kb, parse_mode="Markdown"
    )
    await callback.answer()


@dp.message(F.text)
async def handle_text(message: types.Message):
    uid = message.from_user.id

    # 1. Проверка состояния: ввод промокода
    if user_states.get(uid) == "waiting_promo":
        user_states.pop(uid)
        success, msg = use_promo_code(uid, message.text.strip())
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ В меню", callback_data="menu_main")]])
        await message.answer(f"{'✅' if success else '❌'} {msg}", reply_markup=kb, parse_mode="Markdown")
        return

    # 2. Обычное сообщение -> генерация описания
    text = message.text.strip()
    if len(text) < 15:
        await message.answer("⚠️ Опишите товар подробнее. Минимум 15 символов.", reply_markup=main_menu_kb(uid))
        return

    if uid not in ADMIN_IDS and not get_subscription(uid) and get_user_stats(uid) >= 3:
        await message.answer(
            " *Лимит исчерпан*\n\nОформите подписку для безлимита:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить подписку", callback_data="menu_buy")]]),
            parse_mode="Markdown"
        )
        return

    increment_request(uid)
    await message.answer("⏳ Генерирую карточку товара...", reply_markup=main_menu_kb(uid))

    try:
        result = await generate_with_continuation(SYSTEM_PROMPT, text)
        if not result:
            await message.answer(" Нейросеть вернула пустой ответ. Попробуйте ещё раз.", reply_markup=main_menu_kb(uid))
            return
        parts = [result[i:i + 4000] for i in range(0, len(result), 4000)]
        for i, part in enumerate(parts):
            prefix = "✅ Готово:\n\n" if i == 0 else ""
            await message.answer(f"{prefix}{part}", reply_markup=main_menu_kb(uid) if i == len(parts) - 1 else None)
    except Exception as e:
        logging.error(f"Ошибка генерации: {e}")
        await message.answer(" Ошибка генерации. Попробуйте позже.", reply_markup=main_menu_kb(uid))


if __name__ == "__main__":
    logging.info("Запуск бота...")
    dp.run_polling(bot, allowed_updates=dp.resolve_used_update_types())