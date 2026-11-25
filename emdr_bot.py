"""
Telegram-бот для регистрации на сессию ДПДГ (aiogram 3.x).

Установка зависимостей:
    pip install aiogram

Перед запуском укажите свои данные в блоке НАСТРОЙКИ ниже
(или задайте соответствующие переменные окружения).
Важно: чтобы бот мог писать вам в личные сообщения, сначала
откройте бота в Telegram и нажмите /start со своего аккаунта.
"""

import asyncio
import html
import logging
import os
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Ваш Telegram ID (узнать можно через @userinfobot)

OFFER_URL = os.getenv("OFFER_URL", "https://example.com/offer")
PRIVACY_URL = os.getenv("PRIVACY_URL", "https://example.com/privacy")
PAYMENT_URL = os.getenv("PAYMENT_URL", "https://example.com/pay")
# ===================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGITS_RE = re.compile(r"\D+")


class Registration(StatesGroup):
    waiting_for_data = State()
    waiting_for_agreement = State()


def parse_user_data(text: str) -> tuple[dict[str, str] | None, str | None]:
    """Разбирает и проверяет строку формата 'Иванов Иван, +79991234455, 123@123.com'."""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 3:
        return None, (
            "Пожалуйста, укажите ровно 3 значения через запятую:\n"
            "<code>Иванов Иван, +79991234455, 123@123.com</code>"
        )

    full_name, phone, email = parts

    if len(full_name.split()) < 2:
        return None, (
            "Укажите, пожалуйста, и фамилию, и имя.\n"
            "Пример: <code>Иванов Иван, +79991234455, 123@123.com</code>"
        )

    digits = PHONE_DIGITS_RE.sub("", phone)
    if not (10 <= len(digits) <= 15):
        return None, (
            "Проверьте номер телефона — в нём должно быть от 10 до 15 цифр.\n"
            "Пример: <code>Иванов Иван, +79991234455, 123@123.com</code>"
        )

    if not EMAIL_RE.match(email):
        return None, (
            "Похоже, почта указана с ошибкой.\n"
            "Пример: <code>Иванов Иван, +79991234455, 123@123.com</code>"
        )

    return {
        "full_name": full_name,
        "phone": phone,
        "email": email,
    }, None


def build_agreement_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
            [InlineKeyboardButton(text="🔒 Обработка персональных данных", url=PRIVACY_URL)],
            [InlineKeyboardButton(text="✅ Согласен(на) с условиями", callback_data="agree_terms")],
        ]
    )


def build_payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Перейти к оплате", url=PAYMENT_URL)],
        ]
    )


dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Registration.waiting_for_data)
    await message.answer(
        "Здравствуйте! Для записи на сессию ДПДГ напишите, пожалуйста, "
        "ваши фамилию и имя, телефон и почту через запятую в формате:\n\n"
        "<code>Иванов Иван, +79991234455, 123@123.com</code>"
    )


@dp.message(Registration.waiting_for_data, F.text)
async def handle_user_data(message: Message, state: FSMContext) -> None:
    parsed, error = parse_user_data(message.text or "")
    if error or parsed is None:
        await message.answer(error or "Не удалось распознать данные. Попробуйте ещё раз.")
        return

    await state.update_data(**parsed)
    await state.set_state(Registration.waiting_for_agreement)

    await message.answer(
        "Спасибо! Перед оплатой, пожалуйста, ознакомьтесь с офертой и "
        "условиями обработки персональных данных и подтвердите согласие:",
        reply_markup=build_agreement_keyboard(),
    )


@dp.message(Registration.waiting_for_data)
async def handle_non_text_data(message: Message) -> None:
    await message.answer(
        "Отправьте, пожалуйста, данные обычным текстовым сообщением в формате:\n"
        "<code>Иванов Иван, +79991234455, 123@123.com</code>"
    )


@dp.callback_query(Registration.waiting_for_agreement, F.data == "agree_terms")
async def handle_agreement(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not data:
        await callback.answer("Данные не найдены. Нажмите /start, чтобы начать заново.", show_alert=True)
        return

    # Сразу очищаем состояние, чтобы избежать повторной отправки при двойном нажатии
    await state.clear()

    user = callback.from_user
    username_line = f"@{html.escape(user.username)}" if user.username else "не указан"
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    admin_text = (
        "<b>Новая заявка на сессию ДПДГ</b>\n\n"
        f"<b>ФИО:</b> {html.escape(data['full_name'])}\n"
        f"<b>Телефон:</b> {html.escape(data['phone'])}\n"
        f"<b>Почта:</b> {html.escape(data['email'])}\n"
        f"<b>Telegram:</b> {username_line} "
        f"(<a href=\"tg://user?id={user.id}\">профиль</a>, ID: <code>{user.id}</code>)\n"
        f"<b>Согласие с офертой и ПДн:</b> подтверждено ({timestamp})"
    )

    if ADMIN_ID:
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=admin_text)
        except Exception as exc:
            logger.exception("Не удалось отправить заявку администратору (ADMIN_ID=%s): %s", ADMIN_ID, exc)
    else:
        logger.warning("ADMIN_ID не задан — заявка не отправлена в ЛС: %s", data)

    await callback.answer("Согласие принято!")

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(
            "Спасибо! Ваши данные получены.\n\n"
            f"Для завершения записи перейдите по ссылке для оплаты:\n{PAYMENT_URL}",
            reply_markup=build_payment_keyboard(),
            disable_web_page_preview=True,
        )


@dp.message(Registration.waiting_for_agreement)
async def remind_agreement(message: Message) -> None:
    await message.answer(
        "Чтобы продолжить, нажмите кнопку «✅ Согласен(на) с условиями» выше.",
        reply_markup=build_agreement_keyboard(),
    )


async def main() -> None:
    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER":
        raise ValueError("Укажите BOT_TOKEN перед запуском бота.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())