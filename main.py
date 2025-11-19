import logging
import asyncio
import base64
import json
import random
import sqlite3
import os
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from collections import defaultdict
from contextlib import closing

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher.filters import BoundFilter
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.utils import executor
from aiogram.utils.exceptions import MessageNotModified, MessageToEditNotFound
import aiohttp

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')
ADMIN_USER_IDS = list(map(int, os.getenv('ADMIN_USER_IDS', '').split(','))) if os.getenv('ADMIN_USER_IDS') else []

ALLOWED_USER_IDS = set(ADMIN_USER_IDS)

AI_MODEL = 'sonar-pro'
AI_TEMPERATURE = 0.2
AI_TOP_P = 0.9
AI_MAX_TOKENS = 4000
AI_SEARCH_RECENCY = 'month'
MAX_MESSAGE_LENGTH = 4000
TYPING_INTERVAL = 4

FREE_DAILY_REQUESTS = 3

PRICING = {
    10: 79,
    20: 149,
    30: 279,
    50: 499,
    100: 579,
}

STICKERS = {
    'happy': [
        'CAACAgIAAxkBAAEAT4SkGkeEDiU1I0UqN6fHECID82330xTOAAIWGgACKX8QSDittnGNLdVafAQAHbQADNgQ',
        'CAACAgIAAxkBAAEAT4SmmkeEI7fZ_G7yg2Cfd6wd6gwjv2VAALcGgAC4HQQSAKRksW5iljQAQAHbQADNgQ'
    ],
    'thinking': [
        'CAACAgIAAxkBAAEAT4SnGkeEKb0-LhS--QzGmsLb6wEeo6YAAIuaQACMQv4S17GT8LXFcJzAQAHbQADNgQ'
    ],
    'confused': [
        'CAACAgIAAxkBAAEAT4SnmkeEMWPCsp7dsF0uGgSQVDS-SbOAALzhAACkjZBSj-riZqErj_tAQAHbQADNgQ'
    ],
    'success': [
        'CAACAgIAAxkBAAEAT4SoGkeENw1DpktVCIfTpO71N8-bFRgAALzHAACugkRSHmgabM74njtAQAHbQADNgQ'
    ]
}

logging.basicConfig(
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())


def init_db():
    with closing(sqlite3.connect('bot_data.db')) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    requests_remaining INTEGER DEFAULT 0,
                    daily_free_requests INTEGER DEFAULT 3,
                    last_reset TEXT,
                    total_requests_made INTEGER DEFAULT 0
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    requests_count INTEGER,
                    stars_amount INTEGER,
                    telegram_charge_id TEXT,
                    payment_date TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            conn.commit()
    logger.info("Database initialized successfully")


def get_user_data(user_id: int) -> dict:
    with closing(sqlite3.connect('bot_data.db')) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()

            if result:
                return {
                    'requests_remaining': result[1],
                    'daily_free_requests': result[2],
                    'last_reset': datetime.strptime(result[3], '%Y-%m-%d').date(),
                    'total_requests_made': result[4]
                }
            else:
                cursor.execute('''
                    INSERT INTO users (user_id, requests_remaining, daily_free_requests, last_reset, total_requests_made)
                    VALUES (?, 0, ?, ?, 0)
                ''', (user_id, FREE_DAILY_REQUESTS, datetime.now().date().strftime('%Y-%m-%d')))
                conn.commit()
                return {
                    'requests_remaining': 0,
                    'daily_free_requests': FREE_DAILY_REQUESTS,
                    'last_reset': datetime.now().date(),
                    'total_requests_made': 0
                }


def update_user_data(user_id: int, data: dict):
    with closing(sqlite3.connect('bot_data.db')) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute('''
                UPDATE users 
                SET requests_remaining = ?, 
                    daily_free_requests = ?, 
                    last_reset = ?,
                    total_requests_made = ?
                WHERE user_id = ?
            ''', (
                data['requests_remaining'],
                data['daily_free_requests'],
                data['last_reset'].strftime('%Y-%m-%d'),
                data['total_requests_made'],
                user_id
            ))
            conn.commit()


def save_payment(user_id: int, requests_count: int, stars_amount: int, telegram_charge_id: str):
    with closing(sqlite3.connect('bot_data.db')) as conn:
        with closing(conn.cursor()) as cursor:
            cursor.execute('''
                INSERT INTO payments (user_id, requests_count, stars_amount, telegram_charge_id, payment_date)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, requests_count, stars_amount, telegram_charge_id,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()


def reset_daily_requests(user_id: int):
    if user_id in ADMIN_USER_IDS:
        return

    data = get_user_data(user_id)
    today = datetime.now().date()

    if data['last_reset'] < today:
        data['daily_free_requests'] = FREE_DAILY_REQUESTS
        data['last_reset'] = today
        update_user_data(user_id, data)


def can_make_request(user_id: int) -> bool:
    if user_id in ADMIN_USER_IDS:
        return True

    reset_daily_requests(user_id)
    data = get_user_data(user_id)

    if data['daily_free_requests'] > 0:
        return True

    if data['requests_remaining'] > 0:
        return True

    return False


def consume_request(user_id: int):
    if user_id in ADMIN_USER_IDS:
        return

    reset_daily_requests(user_id)
    data = get_user_data(user_id)

    if data['daily_free_requests'] > 0:
        data['daily_free_requests'] -= 1
    elif data['requests_remaining'] > 0:
        data['requests_remaining'] -= 1

    data['total_requests_made'] += 1
    update_user_data(user_id, data)


def get_time_until_reset() -> str:
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    delta = tomorrow - now

    hours = delta.seconds // 3600
    minutes = (delta.seconds % 3600) // 60

    return f"{hours}ч {minutes}м"


async def send_random_sticker(chat_id: int, mood: str = 'happy'):
    if chat_id not in ADMIN_USER_IDS:
        return

    stickers = STICKERS.get(mood, STICKERS['happy'])
    if stickers and random.random() < 0.3:
        try:
            sticker_id = random.choice(stickers)
            await bot.send_sticker(chat_id, sticker_id)
            logger.info(f"Sent {mood} sticker to admin {chat_id}")
        except Exception as e:
            logger.debug(f"Sticker send error: {e}")


class IsAllowedUser(BoundFilter):
    key = 'is_allowed_user'

    def __init__(self, is_allowed_user: bool = True):
        self.is_allowed_user = is_allowed_user

    async def check(self, message: types.Message) -> bool:
        user_allowed = message.from_user.id in ALLOWED_USER_IDS or message.from_user.id in ADMIN_USER_IDS
        return user_allowed if self.is_allowed_user else not user_allowed


dp.filters_factory.bind(IsAllowedUser)


class TypingMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        if message.from_user.id not in ALLOWED_USER_IDS and message.from_user.id not in ADMIN_USER_IDS:
            return

        if message.text and message.text.startswith('/'):
            return

        task = asyncio.create_task(self._send_typing_periodically(message.chat.id))
        data['typing_task'] = task

    async def on_post_process_message(self, message: types.Message, results, data: dict):
        task = data.get('typing_task')
        if task and not task.done():
            task.cancel()

    async def _send_typing_periodically(self, chat_id: int):
        try:
            while True:
                await bot.send_chat_action(chat_id, types.ChatActions.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Typing action error: {e}")


dp.middleware.setup(TypingMiddleware())


async def download_image_as_base64(file_url: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    image_data = await resp.read()
                    return base64.b64encode(image_data).decode('utf-8')
    except Exception as e:
        logger.error(f"Image download error: {e}")
    return None


async def query_perplexity(question: str, image_base64: Optional[str] = None, is_school_task: bool = False) -> str:
    try:
        headers = {
            'Authorization': f'Bearer {PERPLEXITY_API_KEY}',
            'Content-Type': 'application/json'
        }

        messages = []

        base_system_prompt = (
            "Отвечай кратко, четко и по существу. "
            "Не показывай процесс мышления, рассуждения или промежуточные шаги. "
            "Выдавай только финальный готовый ответ. "
            "Не используй фразы типа 'давайте подумаем', 'сначала разберём', 'итак' и подобные. "
            "Сразу предоставляй конечный результат. "
            "На короткие не имеющие смысла вопросы отвечай также коротко, чтобы не тратить токены. "
            "При употреблении в твою сторону нецензурной лексики можешь отвечать в той же манере, пока перед тобой не извинятся. "
            "Не используй цифровые ссылки на источники в конце предложений (например, 1, 2, 3). "
            "Если нужно указать источник, упомяни его в тексте естественным образом. "
            "Тебя зовут СережкаИИ. "
            "Если человек запрашивает у тебя объяснение какой-либо темы, то ты её объясняешь максимально понятным языком. "
            "Можешь использовать эмодзи из Telegram в умеренном количестве для выразительности, но не перебарщивай."
        )

        if is_school_task:
            system_prompt = (
                    base_system_prompt +
                    " Для школьных заданий: "
                    "структурируй ответ, используй списки и нумерацию, "
                    "выделяй ключевые моменты, объясняй пошагово если нужно, "
                    "но не показывай сам процесс решения - только готовое решение."
            )
        else:
            system_prompt = base_system_prompt

        messages.append({"role": "system", "content": system_prompt})

        if image_base64:
            user_content = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_base64}"
                    }
                },
                {
                    "type": "text",
                    "text": question if question else "Проанализируй это изображение детально. Если это задание или упражнение - реши его полностью."
                }
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": question})

        payload = {
            "model": AI_MODEL,
            "messages": messages,
            "temperature": AI_TEMPERATURE,
            "top_p": AI_TOP_P,
            "max_tokens": AI_MAX_TOKENS,
            "search_recency_filter": AI_SEARCH_RECENCY,
            "return_images": False,
            "return_related_questions": False,
            "stream": False,
            "presence_penalty": 0,
            "frequency_penalty": 1
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                    'https://api.perplexity.ai/chat/completions',
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    answer = result['choices'][0]['message']['content']

                    if not answer:
                        return "Извините, не удалось получить ответ. Попробуйте переформулировать вопрос."

                    import re
                    answer = re.sub(r'\[\d+\]', '', answer)
                    answer = re.sub(r'\s+', ' ', answer)

                    return answer.strip()

                elif resp.status == 429:
                    return "Превышен лимит запросов. Пожалуйста, подождите немного и попробуйте снова."

                elif resp.status == 401:
                    logger.error("Perplexity API authentication error")
                    return "Ошибка доступа к AI. Проверьте настройки API ключа."

                else:
                    error_text = await resp.text()
                    logger.error(f"Perplexity API error {resp.status}: {error_text}")
                    return f"Ошибка API (код {resp.status}). Попробуйте позже."

    except asyncio.TimeoutError:
        return "Превышено время ожидания. Запрос занял слишком много времени. Попробуйте упростить вопрос."

    except Exception as e:
        logger.error(f"Perplexity query error: {e}", exc_info=True)
        return "Произошла ошибка. Попробуйте позже или переформулируйте вопрос."


async def send_long_message(chat_id: int, text: str, parse_mode: str = None) -> None:
    if len(text) <= MAX_MESSAGE_LENGTH:
        try:
            await bot.send_message(chat_id, text, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"Message send error: {e}")
            await bot.send_message(chat_id, text, parse_mode=None)
    else:
        parts = []
        current_part = ""

        for line in text.split('\n'):
            if len(current_part) + len(line) + 1 <= MAX_MESSAGE_LENGTH:
                current_part += line + '\n'
            else:
                if current_part:
                    parts.append(current_part.strip())
                current_part = line + '\n'

        if current_part:
            parts.append(current_part.strip())

        for i, part in enumerate(parts):
            try:
                footer = f"\n\nЧасть {i + 1} из {len(parts)}" if len(parts) > 1 else ""
                await bot.send_message(chat_id, part + footer, parse_mode=parse_mode)
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Message part {i + 1} sending error: {e}")
                await bot.send_message(chat_id, part, parse_mode=None)


@dp.message_handler(commands=['start'], is_allowed_user=False)
async def cmd_start_denied(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        types.InlineKeyboardButton(text="Купить доступ", callback_data="buy_access")
    )

    await message.answer(
        "Привет. Для использования бота нужно приобрести доступ.\n\n"
        "Каждый день вы получаете 3 бесплатных запроса, которые сбрасываются каждые 24 часа.\n"
        "Для дополнительных запросов можно приобрести пакеты по выгодным ценам.",
        reply_markup=keyboard
    )


@dp.message_handler(commands=['start'], is_allowed_user=True)
async def cmd_start(message: types.Message):
    first_name = message.from_user.first_name
    user_id = message.from_user.id

    if user_id in ADMIN_USER_IDS:
        status = "Администратор (безлимит)"
    else:
        reset_daily_requests(user_id)
        data = get_user_data(user_id)
        daily = data['daily_free_requests']
        paid = data['requests_remaining']
        status = f"Бесплатных: {daily}/3 (сброс через {get_time_until_reset()})\nКупленных: {paid}"

    welcome_text = (
        f"Привет, {first_name}!\n\n"
        f"Я — СережкаИИ на базе Sonar Pro.\n\n"
        f"Твой статус: {status}\n\n"
        f"Что я умею:\n"
        f"• Отвечать на любые вопросы\n"
        f"• Решать школьные задания по фото\n"
        f"• Анализировать изображения\n"
        f"• Искать актуальную информацию\n\n"
        f"Команды:\n"
        f"/buy - Купить запросы\n"
        f"/balance - Проверить баланс\n"
        f"/help - Справка\n\n"
        f"Просто отправь мне вопрос или фото с заданием."
    )
    await message.answer(welcome_text)


@dp.message_handler(commands=['help'], is_allowed_user=True)
async def cmd_help(message: types.Message):
    help_text = (
        "Как пользоваться ботом\n\n"
        "Текстовые вопросы:\n"
        "Просто напиши свой вопрос, и я отвечу.\n\n"
        "Школьные задания:\n"
        "Отправь фото задания (можно с подписью).\n"
        "Я решу его и оформлю ответ.\n\n"
        "Анализ изображений:\n"
        "Отправь любое изображение для анализа.\n\n"
        "Бесплатные запросы:\n"
        "Каждый день ты получаешь 3 бесплатных запроса.\n"
        "Они сбрасываются каждые 24 часа.\n\n"
        "Покупка запросов:\n"
        "Используй /buy для покупки дополнительных запросов.\n\n"
        "Все ответы от Perplexity Sonar Pro"
    )
    await message.answer(help_text)


@dp.message_handler(commands=['balance'])
async def cmd_balance(message: types.Message):
    user_id = message.from_user.id

    if user_id in ADMIN_USER_IDS:
        await message.answer("Ты администратор. У тебя безлимитный доступ.")
        return

    reset_daily_requests(user_id)
    data = get_user_data(user_id)

    daily = data['daily_free_requests']
    paid = data['requests_remaining']
    total = data['total_requests_made']

    balance_text = (
        f"Твой баланс:\n\n"
        f"Бесплатных запросов: {daily}/3\n"
        f"Сброс через: {get_time_until_reset()}\n\n"
        f"Купленных запросов: {paid}\n\n"
        f"Всего выполнено: {total}\n\n"
        f"Для покупки используй /buy"
    )

    await message.answer(balance_text)


@dp.message_handler(commands=['buy'])
async def cmd_buy(message: types.Message):
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    buttons = []
    for requests, stars in PRICING.items():
        discount = ""
        if requests >= 50:
            discount = " (-30%)"
        elif requests >= 30:
            discount = " (-13%)"
        elif requests >= 20:
            discount = " (-10%)"

        buttons.append(
            types.InlineKeyboardButton(
                text=f"{requests} запросов - {stars} Stars{discount}",
                callback_data=f"buy_{requests}"
            )
        )

    keyboard.add(*buttons)

    buy_text = (
        "Выбери пакет запросов:\n\n"
        "Чем больше пакет - тем выгоднее цена!\n\n"
        "1 Star ~ 0.02$\n"
        "Telegram Stars можно купить в @PremiumBot"
    )

    await message.answer(buy_text, reply_markup=keyboard)


@dp.callback_query_handler(lambda c: c.data == 'buy_access')
async def process_buy_access(callback_query: types.CallbackQuery):
    await callback_query.answer()
    await cmd_buy(callback_query.message)


@dp.callback_query_handler(lambda c: c.data.startswith('buy_'))
async def process_buy_callback(callback_query: types.CallbackQuery):
    await callback_query.answer()

    requests_count = int(callback_query.data.split('_')[1])
    stars_amount = PRICING[requests_count]

    user_id = callback_query.from_user.id
    ALLOWED_USER_IDS.add(user_id)

    try:
        invoice = await bot.send_invoice(
            chat_id=callback_query.message.chat.id,
            title=f"{requests_count} запросов к AI",
            description=f"Покупка пакета из {requests_count} запросов к Perplexity AI",
            payload=json.dumps({
                'user_id': user_id,
                'requests': requests_count,
                'stars': stars_amount
            }),
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{requests_count} запросов", amount=stars_amount)]
        )

        logger.info(f"Invoice sent to user {user_id} for {requests_count} requests ({stars_amount} Stars)")

    except Exception as e:
        logger.error(f"Invoice creation error: {e}")
        await callback_query.message.answer("Ошибка создания счета. Попробуйте позже.")


@dp.pre_checkout_query_handler()
async def process_pre_checkout_query(pre_checkout_query: types.PreCheckoutQuery):
    try:
        payload_data = json.loads(pre_checkout_query.invoice_payload)
        logger.info(f"Pre-checkout for user {payload_data['user_id']}: {payload_data['requests']} requests")

        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=True
        )
    except Exception as e:
        logger.error(f"Pre-checkout error: {e}")
        await bot.answer_pre_checkout_query(
            pre_checkout_query.id,
            ok=False,
            error_message="Ошибка обработки платежа. Попробуйте позже."
        )


@dp.message_handler(content_types=types.ContentType.SUCCESSFUL_PAYMENT)
async def process_successful_payment(message: types.Message):
    try:
        payload_data = json.loads(message.successful_payment.invoice_payload)
        user_id = payload_data['user_id']
        requests_count = payload_data['requests']
        stars_amount = payload_data['stars']
        telegram_charge_id = message.successful_payment.telegram_payment_charge_id

        data = get_user_data(user_id)
        data['requests_remaining'] += requests_count
        update_user_data(user_id, data)

        save_payment(user_id, requests_count, stars_amount, telegram_charge_id)

        logger.info(
            f"Payment successful: user {user_id} purchased {requests_count} requests. "
            f"Total: {data['requests_remaining']}. Charge ID: {telegram_charge_id}"
        )

        await message.answer(
            f"Оплата прошла успешно!\n\n"
            f"Добавлено запросов: {requests_count}\n"
            f"Всего купленных: {data['requests_remaining']}\n\n"
            f"Спасибо за покупку!"
        )

        await send_random_sticker(user_id, 'success')

    except Exception as e:
        logger.error(f"Payment processing error: {e}", exc_info=True)
        await message.answer("Платеж получен, но произошла ошибка. Свяжитесь с поддержкой.")


@dp.message_handler(commands=['paysupport'])
async def cmd_paysupport(message: types.Message):
    support_text = (
        "Поддержка по платежам:\n\n"
        "Если у вас возникли проблемы с оплатой или начислением запросов, "
        "опишите проблему в ответном сообщении.\n\n"
        "Обязательно укажите:\n"
        "- Что именно произошло\n"
        "- Когда вы совершали платеж\n"
        "- Сколько запросов должно было быть начислено\n\n"
        "Мы ответим в ближайшее время."
    )
    await message.answer(support_text)


@dp.message_handler(is_allowed_user=False, content_types=types.ContentType.ANY)
async def handle_unauthorized(message: types.Message):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(text="Купить доступ", callback_data="buy_access")
    )
    await message.answer(
        "У вас закончились бесплатные запросы. Купите пакет для продолжения работы.",
        reply_markup=keyboard
    )


@dp.message_handler(is_allowed_user=True, content_types=types.ContentType.PHOTO)
async def handle_photo(message: types.Message):
    user_id = message.from_user.id

    if not can_make_request(user_id):
        keyboard = types.InlineKeyboardMarkup()
        keyboard.add(
            types.InlineKeyboardButton(text="Купить запросы", callback_data="buy_access")
        )
        await message.answer(
            "У вас закончились запросы.\n"
            f"Бесплатные сбросятся через: {get_time_until_reset()}",
            reply_markup=keyboard
        )
        return

    processing_msg = None

    try:
        processing_msg = await message.answer("Обрабатываю изображение...")

        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_url = f'https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file.file_path}'

        image_base64 = await download_image_as_base64(file_url)

        if not image_base64:
            await processing_msg.edit_text("Ошибка обработки изображения. Попробуйте ещё раз.")
            return

        try:
            await processing_msg.edit_text("Анализирую с помощью AI...")
        except (MessageNotModified, MessageToEditNotFound):
            pass

        caption = message.caption if message.caption else ""
        school_keywords = ['реши', 'задача', 'задание', 'домашка', 'урок', 'предмет', 'помоги', 'решение']
        is_school = any(keyword in caption.lower() for keyword in school_keywords)

        question = caption if caption else "Проанализируй это изображение детально и подробно."
        answer = await query_perplexity(question=question, image_base64=image_base64, is_school_task=is_school)

        try:
            await processing_msg.delete()
        except:
            pass

        consume_request(user_id)

        await send_long_message(message.chat.id, answer)

        await send_random_sticker(user_id, 'happy')

    except Exception as e:
        logger.error(f"Photo handling error: {e}", exc_info=True)
        if processing_msg:
            try:
                await processing_msg.edit_text("Произошла ошибка. Попробуйте позже.")
            except:
                await message.answer("Произошла ошибка при обработке изображения.")


@dp.message_handler(is_allowed_user=True, content_types=types.ContentType.TEXT)
async def handle_text(message: types.Message):
    user_id = message.from_user.id

    try:
        if message.text.startswith('/'):
            return

        if not can_make_request(user_id):
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(
                types.InlineKeyboardButton(text="Купить запросы", callback_data="buy_access")
            )
            await message.answer(
                "У вас закончились запросы.\n"
                f"Бесплатные сбросятся через: {get_time_until_reset()}",
                reply_markup=keyboard
            )
            return

        answer = await query_perplexity(question=message.text)

        consume_request(user_id)

        await send_long_message(message.chat.id, answer)

        await send_random_sticker(user_id, 'happy')

    except Exception as e:
        logger.error(f"Text handling error: {e}", exc_info=True)
        await message.answer("Произошла ошибка. Попробуйте позже.")


@dp.errors_handler()
async def errors_handler(update: types.Update, exception: Exception):
    logger.error(f"Update handling error: {exception}", exc_info=True)

    if update.message:
        try:
            await update.message.answer("Произошла непредвиденная ошибка. Попробуйте позже.")
        except Exception:
            pass

    return True


async def on_startup(dp):
    init_db()
    logger.info("Bot started successfully")
    logger.info(f"Admin users: {len(ADMIN_USER_IDS)}")
    logger.info(f"AI model: {AI_MODEL}")
    logger.info(f"Payment system: Telegram Stars enabled")
    logger.info(f"Database: SQLite (bot_data.db)")


async def on_shutdown(dp):
    logger.info("Bot stopped")
    await bot.close()


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
