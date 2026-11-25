"""
Telegram-бот для регистрации на сессию ДПДГ (aiogram 3.x).

Порядок шагов:
1. /start -> отдельное согласие на обработку персональных данных (до сбора данных).
2. Отдельное ознакомление и принятие условий оферты.
3. Ввод ФИО, телефона и почты через запятую.
4. Отправка заявки администратору в ЛС и ссылки на оплату клиенту.

Установка зависимостей:
    pip install aiogram

Перед запуском укажите свои данные в блоке НАСТРОЙКИ ниже
(или задайте соответствующие переменные окружения).
"""

import asyncio
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone

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
# Опционально: отдельная ссылка на текст согласия на обработку ПДн (если не задана, используется PRIVACY_URL)
PDN_CONSENT_URL = os.getenv("PDN_CONSENT_URL", PRIVACY_URL)
PAYMENT_URL = os.getenv("PAYMENT_URL", "https://example.com/pay")
# ===================================================

MSK_TZ = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGITS_RE = re.compile(r"\D+")


def now_msk() -> str:
    """Возвращает текущее московское время (UTC+3) в читаемом формате."""
    return datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")


class Registration(StatesGroup):
    waiting_for_pdn_consent = State()
    waiting_for_offer_consent = State()
    waiting_for_data = State()


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


def build_pdn_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔒 Политика обработки ПДн", url=PRIVACY_URL)],
    ]
    if PDN_CONSENT_URL and PDN_CONSENT_URL != PRIVACY_URL:
        rows.append(
            [InlineKeyboardButton(text="📝 Текст согласия на обработку ПДн", url=PDN_CONSENT_URL)]
        )
    rows.append(
        [InlineKeyboardButton(text="✅ Даю согласие на обработку ПДн", callback_data="agree_pdn")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 Оферта", url=OFFER_URL)],
            [InlineKeyboardButton(text="✅ Принимаю условия оферты", callback_data="agree_offer")],
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
    await state.set_state(Registration.waiting_for_pdn_consent)
    await message.answer(
        "Здравствуйте! Для записи на сессию ДПДГ нам понадобятся ваши контактные данные "
        "(фамилия, имя, телефон и почта).\n\n"
        "Перед тем как их отправить, пожалуйста, ознакомьтесь с политикой обработки "
        "персональных данных и подтвердите отдельное согласие на их обработку:",
        reply_markup=build_pdn_keyboard(),
    )


@dp.callback_query(Registration.waiting_for_pdn_consent, F.data == "agree_pdn")
async def handle_pdn_consent(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(pdn_consent_at=now_msk())
    await state.set_state(Registration.waiting_for_offer_consent)

    await callback.answer("Согласие на обработку ПДн принято")

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(
            "Теперь, пожалуйста, ознакомьтесь с офертой и подтвердите согласие с её условиями:",
            reply_markup=build_offer_keyboard(),
        )


@dp.message(Registration.waiting_for_pdn_consent)
async def remind_pdn_consent(message: Message) -> None:
    await message.answer(
        "Чтобы продолжить запись, сначала нажмите кнопку «✅ Даю согласие на обработку ПДн»:",
        reply_markup=build_pdn_keyboard(),
    )


@dp.callback_query(Registration.waiting_for_offer_consent, F.data == "agree_offer")
async def handle_offer_consent(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(offer_consent_at=now_msk())
    await state.set_state(Registration.waiting_for_data)

    await callback.answer("Условия оферты приняты")

    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.message.answer(
            "Спасибо! Теперь напишите, пожалуйста, ваши фамилию и имя, телефон и почту "
            "через запятую в формате:\n\n"
            "<code>Иванов Иван, +79991234455, 123@123.com</code>"
        )


@dp.message(Registration.waiting_for_offer_consent)
async def remind_offer_consent(message: Message) -> None:
    await message.answer(
        "Чтобы продолжить, нажмите кнопку «✅ Принимаю условия оферты»:",
        reply_markup=build_offer_keyboard(),
    )


@dp.message(Registration.waiting_for_data, F.text)
async def handle_user_data(message: Message, state: FSMContext, bot: Bot) -> None:
    parsed, error = parse_user_data(message.text or "")
    if error or parsed is None:
        await message.answer(error or "Не удалось распознать данные. Попробуйте ещё раз.")
        return

    consent_data = await state.get_data()
    await state.clear()

    user = message.from_user
    username_line = f"@{html.escape(user.username)}" if (user and user.username) else "не указан"
    user_id = user.id if user else 0
    pdn_consent_at = consent_data.get("pdn_consent_at", "подтверждено")
    offer_consent_at = consent_data.get("offer_consent_at", "подтверждено")

    admin_text = (
        "<b>Новая заявка на сессию ДПДГ</b>\n\n"
        f"<b>ФИО:</b> {html.escape(parsed['full_name'])}\n"
        f"<b>Телефон:</b> {html.escape(parsed['phone'])}\n"
        f"<b>Почта:</b> {html.escape(parsed['email'])}\n"
        f"<b>Telegram:</b> {username_line} "
        f"(<a href=\"tg://user?id={user_id}\">профиль</a>, ID: <code>{user_id}</code>)\n"
        f"<b>Согласие на обработку ПДн:</b> да ({pdn_consent_at})\n"
        f"<b>Принятие оферты:</b> да ({offer_consent_at})"
    )

    if ADMIN_ID:
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=admin_text)
        except Exception as exc:
            logger.exception("Не удалось отправить заявку администратору (ADMIN_ID=%s): %s", ADMIN_ID, exc)
    else:
        logger.warning("ADMIN_ID не задан — заявка не отправлена в ЛС: %s", parsed)

    await message.answer(
        "Спасибо! Ваши данные получены.\n\n"
        f"Для завершения записи перейдите по ссылке для оплаты:\n{PAYMENT_URL}",
        reply_markup=build_payment_keyboard(),
        disable_web_page_preview=True,
    )


@dp.message(Registration.waiting_for_data)
async def handle_non_text_data(message: Message) -> None:
    await message.answer(
        "Отправьте, пожалуйста, данные обычным текстовым сообщением в формате:\n"
        "<code>Иванов Иван, +79991234455, 123@123.com</code>"
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
