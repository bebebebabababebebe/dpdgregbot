"""
Telegram-бот для регистрации на сессию ДПДГ (aiogram 3.x + встроенный SQLite).

Сценарий клиента:
1. /start -> отдельное согласие на обработку ПДн (до сбора данных).
2. Отдельное принятие условий оферты.
3. Ввод ФИО, телефона и почты через запятую.
4. Отправка заявки администратору в ЛС с кнопкой подтверждения оплаты и выдача кнопок оплаты (Т-Банк / Сбербанк).

Сценарий администратора (в ЛС с ботом):
- Нажатие «✅ Подтвердить оплату» под заявкой переводит клиента в статус оплатившего и уведомляет его.
- Команда /admin — статистика, список оплативших и рассылка ссылки на конференцию всем подтверждённым оплатившим.

Установка зависимостей:
    pip install aiogram
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
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

# Ссылки на оплату (если задана старая переменная PAYMENT_URL, она подхватится для Т-Банка)
TBANK_PAYMENT_URL = os.getenv(
    "TBANK_PAYMENT_URL",
    os.getenv("PAYMENT_URL", "https://example.com/pay-tbank"),
)
SBER_PAYMENT_URL = os.getenv("SBER_PAYMENT_URL", "https://example.com/pay-sber")

# Опционально: ссылка на конференцию по умолчанию (можно также вставить прямо в чате при рассылке)
CONFERENCE_URL = os.getenv("CONFERENCE_URL", "").strip()

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent / "registrations.db"))
# ===================================================

MSK_TZ = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGITS_RE = re.compile(r"\D+")
URL_RE = re.compile(r"https?://\S+")


def now_msk() -> str:
    """Возвращает текущее московское время (UTC+3) в читаемом формате."""
    return datetime.now(MSK_TZ).strftime("%d.%m.%Y %H:%M МСК")


# ==================== БАЗА ДАННЫХ (SQLite) ====================
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL,
                pdn_consent_at TEXT NOT NULL,
                offer_consent_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                paid_at TEXT,
                conference_sent_at TEXT
            )
            """
        )
        conn.commit()


def create_registration(
    *,
    user_id: int,
    username: str | None,
    full_name: str,
    phone: str,
    email: str,
    pdn_consent_at: str,
    offer_consent_at: str,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO registrations (
                user_id, username, full_name, phone, email,
                pdn_consent_at, offer_consent_at, created_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                user_id,
                username or "",
                full_name,
                phone,
                email,
                pdn_consent_at,
                offer_consent_at,
                now_msk(),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_registration(reg_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
        return dict(row) if row else None


def mark_registration_paid(reg_id: int) -> str:
    paid_at = now_msk()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE registrations SET status = 'paid', paid_at = ? WHERE id = ?",
            (paid_at, reg_id),
        )
        conn.commit()
    return paid_at


def get_paid_recipients(only_unsent: bool = True) -> list[dict]:
    """Возвращает уникальных оплативших пользователей (по user_id)."""
    condition = (
        "status = 'paid' AND (conference_sent_at IS NULL OR conference_sent_at = '')"
        if only_unsent
        else "status = 'paid'"
    )
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT user_id, MAX(full_name) AS full_name, MAX(username) AS username, MAX(paid_at) AS paid_at
            FROM registrations
            WHERE {condition}
            GROUP BY user_id
            ORDER BY MAX(id) ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]


def mark_conference_sent(user_ids: list[int]) -> None:
    if not user_ids:
        return
    sent_at = now_msk()
    placeholders = ",".join("?" for _ in user_ids)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE registrations SET conference_sent_at = ? WHERE status = 'paid' AND user_id IN ({placeholders})",
            [sent_at, *user_ids],
        )
        conn.commit()


def get_stats() -> dict[str, int]:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM registrations").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM registrations WHERE status = 'pending'").fetchone()[0]
        paid = conn.execute("SELECT COUNT(DISTINCT user_id) FROM registrations WHERE status = 'paid'").fetchone()[0]
        unsent = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM registrations "
            "WHERE status = 'paid' AND (conference_sent_at IS NULL OR conference_sent_at = '')"
        ).fetchone()[0]
        return {"total": total, "pending": pending, "paid": paid, "unsent": unsent}


# ==================== СОСТОЯНИЯ (FSM) ====================
class Registration(StatesGroup):
    waiting_for_pdn_consent = State()
    waiting_for_offer_consent = State()
    waiting_for_data = State()


class AdminBroadcast(StatesGroup):
    waiting_for_link = State()
    waiting_for_confirm = State()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def is_admin(user_id: int | None) -> bool:
    return bool(ADMIN_ID and user_id == ADMIN_ID)


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

    return {"full_name": full_name, "phone": phone, "email": email}, None


def build_pdn_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔒 Политика обработки ПДн", url=PRIVACY_URL)]]
    if PDN_CONSENT_URL and PDN_CONSENT_URL != PRIVACY_URL:
        rows.append([InlineKeyboardButton(text="📝 Текст согласия на обработку ПДн", url=PDN_CONSENT_URL)])
    rows.append([InlineKeyboardButton(text="✅ Даю согласие на обработку ПДн", callback_data="agree_pdn")])
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
            [InlineKeyboardButton(text="💳 Т-Банк", url=TBANK_PAYMENT_URL)],
            [InlineKeyboardButton(text="💳 Сбербанк", url=SBER_PAYMENT_URL)],
        ]
    )


def build_admin_confirm_payment_keyboard(reg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm_pay:{reg_id}")]
        ]
    )


def build_admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Отправить ссылку новым оплатившим",
                    callback_data="admin_send_conf:unsent",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Отправить ссылку всем оплатившим",
                    callback_data="admin_send_conf:all",
                )
            ],
            [InlineKeyboardButton(text="📋 Список оплативших", callback_data="admin_list_paid")],
        ]
    )


def format_admin_application(reg: dict) -> str:
    username_line = f"@{html.escape(reg['username'])}" if reg.get("username") else "не указан"
    status_line = (
        f"✅ подтверждена ({reg['paid_at']})"
        if reg.get("status") == "paid"
        else "⏳ ожидает подтверждения"
    )
    return (
        f"<b>Заявка #{reg['id']} на сессию ДПДГ</b>\n\n"
        f"<b>ФИО:</b> {html.escape(reg['full_name'])}\n"
        f"<b>Телефон:</b> {html.escape(reg['phone'])}\n"
        f"<b>Почта:</b> {html.escape(reg['email'])}\n"
        f"<b>Telegram:</b> {username_line} "
        f"(<a href=\"tg://user?id={reg['user_id']}\">профиль</a>, ID: <code>{reg['user_id']}</code>)\n"
        f"<b>Согласие на обработку ПДн:</b> да ({reg['pdn_consent_at']})\n"
        f"<b>Принятие оферты:</b> да ({reg['offer_consent_at']})\n"
        f"<b>Оплата:</b> {status_line}"
    )


dp = Dispatcher(storage=MemoryStorage())


# ==================== КЛИЕНТСКИЙ СЦЕНАРИЙ ====================
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
    user_id = user.id if user else 0
    username = user.username if user else None
    pdn_consent_at = consent_data.get("pdn_consent_at", now_msk())
    offer_consent_at = consent_data.get("offer_consent_at", now_msk())

    reg_id = create_registration(
        user_id=user_id,
        username=username,
        full_name=parsed["full_name"],
        phone=parsed["phone"],
        email=parsed["email"],
        pdn_consent_at=pdn_consent_at,
        offer_consent_at=offer_consent_at,
    )
    reg = get_registration(reg_id)

    if ADMIN_ID and reg:
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=format_admin_application(reg),
                reply_markup=build_admin_confirm_payment_keyboard(reg_id),
            )
        except Exception as exc:
            logger.exception("Не удалось отправить заявку администратору (ADMIN_ID=%s): %s", ADMIN_ID, exc)
    else:
        logger.warning("ADMIN_ID не задан — заявка #%s не отправлена в ЛС", reg_id)

    await message.answer(
        "Спасибо! Ваши данные получены.\n\n"
        "Для завершения записи выберите удобный способ оплаты по кнопкам ниже.\n\n"
        "После подтверждения оплаты мы пришлём вам уведомление и ссылку на конференцию.",
        reply_markup=build_payment_keyboard(),
        disable_web_page_preview=True,
    )


@dp.message(Registration.waiting_for_data)
async def handle_non_text_data(message: Message) -> None:
    await message.answer(
        "Отправьте, пожалуйста, данные обычным текстовым сообщением в формате:\n"
        "<code>Иванов Иван, +79991234455, 123@123.com</code>"
    )


# ==================== АДМИН-ФУНКЦИИ ====================
@dp.callback_query(F.data.startswith("confirm_pay:"))
async def handle_confirm_payment(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору.", show_alert=True)
        return

    try:
        reg_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректный ID заявки.", show_alert=True)
        return

    reg = get_registration(reg_id)
    if not reg:
        await callback.answer("Заявка не найдена в базе.", show_alert=True)
        return

    if reg["status"] == "paid":
        await callback.answer("Оплата по этой заявке уже подтверждена.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    mark_registration_paid(reg_id)
    updated_reg = get_registration(reg_id)

    if callback.message and updated_reg:
        try:
            await callback.message.edit_text(
                format_admin_application(updated_reg),
                reply_markup=None,
            )
        except Exception:
            pass

    notify_note = "Оплата подтверждена, клиент уведомлён."
    try:
        await bot.send_message(
            chat_id=reg["user_id"],
            text=(
                "Оплата подтверждена! Вы записаны на сессию ДПДГ.\n"
                "Ссылку на конференцию мы пришлём вам отдельным сообщением."
            ),
        )
    except Exception as exc:
        logger.warning("Не удалось уведомить клиента %s о подтверждении оплаты: %s", reg["user_id"], exc)
        notify_note = "Оплата подтверждена (но клиенту не удалось отправить сообщение)."

    await callback.answer(notify_note, show_alert=False)


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return

    await state.clear()
    stats = get_stats()
    await message.answer(
        "<b>Панель администратора</b>\n\n"
        f"Всего заявок: <b>{stats['total']}</b>\n"
        f"Ожидают подтверждения оплаты: <b>{stats['pending']}</b>\n"
        f"Подтверждённых оплативших: <b>{stats['paid']}</b>\n"
        f"Оплативших без ссылки на конференцию: <b>{stats['unsent']}</b>",
        reply_markup=build_admin_panel_keyboard(),
    )


@dp.callback_query(F.data == "admin_list_paid")
async def handle_list_paid(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору.", show_alert=True)
        return

    recipients = get_paid_recipients(only_unsent=False)
    await callback.answer()

    if not recipients:
        if callback.message:
            await callback.message.answer("Пока нет подтверждённых оплативших.")
        return

    lines = ["<b>Подтверждённые оплатившие:</b>\n"]
    for idx, item in enumerate(recipients, start=1):
        uname = f"@{html.escape(item['username'])}" if item["username"] else f"ID {item['user_id']}"
        lines.append(f"{idx}. {html.escape(item['full_name'])} ({uname}) — {item['paid_at']}")

    if callback.message:
        await callback.message.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("admin_send_conf:"))
async def start_conference_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору.", show_alert=True)
        return

    mode = callback.data.split(":", 1)[1]
    only_unsent = mode == "unsent"
    recipients = get_paid_recipients(only_unsent=only_unsent)

    if not recipients:
        msg = (
            "Нет оплативших, кому ссылка ещё не отправлялась."
            if only_unsent
            else "Пока нет подтверждённых оплативших."
        )
        await callback.answer(msg, show_alert=True)
        return

    await state.set_state(AdminBroadcast.waiting_for_link)
    await state.update_data(only_unsent=only_unsent)
    await callback.answer()

    buttons = []
    if CONFERENCE_URL:
        buttons.append(
            [InlineKeyboardButton(text="📎 Использовать ссылку из настроек", callback_data="broadcast_use_default")]
        )
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")])

    extra = f"\n\nТекущая ссылка в настройках: {html.escape(CONFERENCE_URL)}" if CONFERENCE_URL else ""
    if callback.message:
        await callback.message.answer(
            f"Получателей: <b>{len(recipients)}</b>.\n\n"
            "Пришлите сюда ссылку на конференцию (или готовый текст сообщения со ссылкой), "
            f"который нужно отправить оплатившим:{extra}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            disable_web_page_preview=True,
        )


def build_broadcast_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Отправить сейчас", callback_data="broadcast_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")],
        ]
    )


async def show_broadcast_preview(target_message: Message, state: FSMContext, text_or_url: str) -> None:
    data = await state.get_data()
    only_unsent = data.get("only_unsent", True)
    recipients = get_paid_recipients(only_unsent=only_unsent)

    match = URL_RE.search(text_or_url)
    url = match.group(0) if match else ""
    if text_or_url.strip() == url and url:
        message_text = (
            "Ссылка на конференцию для сессии ДПДГ готова!\n\n"
            f"Переходите по ссылке:\n{url}"
        )
    else:
        message_text = text_or_url.strip()

    await state.update_data(broadcast_text=message_text, broadcast_url=url)
    await state.set_state(AdminBroadcast.waiting_for_confirm)

    await target_message.answer(
        f"<b>Предпросмотр рассылки ({len(recipients)} чел.):</b>\n\n"
        f"{html.escape(message_text)}\n\n"
        "Отправить это сообщение подтверждённым оплатившим?",
        reply_markup=build_broadcast_preview_keyboard(),
        disable_web_page_preview=True,
    )


@dp.callback_query(AdminBroadcast.waiting_for_link, F.data == "broadcast_use_default")
async def use_default_conference_url(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору.", show_alert=True)
        return
    if not CONFERENCE_URL:
        await callback.answer("Ссылка в настройках не задана.", show_alert=True)
        return

    await callback.answer()
    if callback.message:
        await show_broadcast_preview(callback.message, state, CONFERENCE_URL)


@dp.message(AdminBroadcast.waiting_for_link, F.text)
async def receive_conference_link(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await show_broadcast_preview(message, state, message.text or "")


@dp.callback_query(F.data == "broadcast_cancel")
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer("Рассылка отменена")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("Рассылка отменена.")


@dp.callback_query(AdminBroadcast.waiting_for_confirm, F.data == "broadcast_confirm")
async def execute_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступно только администратору.", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()

    only_unsent = data.get("only_unsent", True)
    broadcast_text = data.get("broadcast_text", "")
    broadcast_url = data.get("broadcast_url", "")

    recipients = get_paid_recipients(only_unsent=only_unsent)
    reply_markup = (
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🎥 Перейти на конференцию", url=broadcast_url)]]
        )
        if broadcast_url
        else None
    )

    await callback.answer("Начинаю отправку...")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    sent_ids: list[int] = []
    failed: list[str] = []

    for item in recipients:
        try:
            await bot.send_message(
                chat_id=item["user_id"],
                text=html.escape(broadcast_text),
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            sent_ids.append(item["user_id"])
            await asyncio.sleep(0.05)
        except Exception as exc:
            logger.warning("Не удалось отправить ссылку пользователю %s: %s", item["user_id"], exc)
            failed.append(f"{item['full_name']} (ID {item['user_id']})")

    mark_conference_sent(sent_ids)

    report = f"Готово! Ссылка отправлена: <b>{len(sent_ids)}</b> из <b>{len(recipients)}</b>."
    if failed:
        report += "\n\nНе удалось доставить:\n• " + "\n• ".join(html.escape(name) for name in failed)

    if callback.message:
        await callback.message.answer(report)


async def main() -> None:
    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_ОТ_BOTFATHER":
        raise ValueError("Укажите BOT_TOKEN перед запуском бота.")

    init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
