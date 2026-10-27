"""Telegram-бот генерации SMM-контента для автосервисов (СТО).

Стек: Python 3.11+, aiogram 3.x, asyncio, pydantic-settings.
Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Final

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web

from config import get_settings
from image_client import ImageError, generate_image
from llm_client import LLMError, generate_post

logger = logging.getLogger(__name__)
router = Router(name="sto_content")

# ---------------------------------------------------------------- константы

CITIES: Final[tuple[str, ...]] = (
    "Москва",
    "Санкт-Петербург",
    "Екатеринбург",
    "Новосибирск",
    "Казань",
    "Краснодар",
)
SERVICES: Final[tuple[str, ...]] = (
    "Шиномонтаж",
    "Диагностика",
    "Замена масла",
    "Ремонт ходовой",
    "Акция",
)

OTHER_CITY_CALLBACK: Final[str] = "city:__other__"
MAX_CAPTION: Final[int] = 1024

GREETING: Final[str] = (
    "Привет! Я помогу сделать пост для вашего автосервиса: текст + картинка за 30 секунд.\n\n"
    "Шаг 1 из 3. В каком городе вы работаете?"
)
CITY_ASKED_OTHER: Final[str] = "Напишите название вашего города одним сообщением."
STO_ASKED: Final[str] = (
    "Шаг 2 из 3. Как называется ваш автосервис?\n\n"
    "Напишите название или нажмите «Пропустить»."
)
SERVICE_ASKED: Final[str] = "Шаг 3 из 3. Что рекламируем?"
LLM_FAIL_TEXT: Final[str] = "Сервер перегружен, попробуйте через минуту."
IMAGE_FAIL_TEXT: Final[str] = (
    "⚠️ Изображение временно недоступно, но вот готовый текст для вашего поста!"
)

# Защита от двойной генерации в одном чате.
_inflight: set[int] = set()


# ---------------------------------------------------------------- состояния


class Flow(StatesGroup):
    city = State()
    city_manual = State()
    sto_name = State()
    service = State()
    decision = State()


# ---------------------------------------------------------------- клавиатуры


def city_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(CITIES), 2):
        rows.append(
            [
                InlineKeyboardButton(text=city, callback_data=f"city:{city}")
                for city in CITIES[index : index + 2]
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="✏️ Другой город", callback_data=OTHER_CITY_CALLBACK)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_sto_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить ➡️", callback_data="sto:skip")]
        ]
    )


def service_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=service, callback_data=f"service:{service}")]
            for service in SERVICES
        ]
    )


def retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔁 Попробовать снова", callback_data="retry")]
        ]
    )


def decision_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, ещё вариант", callback_data="regen:yes"
                ),
                InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="regen:no"),
            ]
        ]
    )


# ---------------------------------------------------------------- утилиты


@asynccontextmanager
async def keep_chat_action(
    bot: Bot, chat_id: int, action: str = ChatAction.TYPING
) -> AsyncIterator[None]:
    """Держит в чате статус «печатает…» / «отправляет фото» всё время генерации."""

    async def _pulse() -> None:
        while True:
            await asyncio.sleep(4.5)
            with suppress(TelegramAPIError):
                await bot.send_chat_action(chat_id, action)

    await bot.send_chat_action(chat_id, action)
    task = asyncio.create_task(_pulse())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _safe_edit(message: Message, text: str) -> None:
    with suppress(TelegramAPIError):
        await message.edit_text(text)


# ---------------------------------------------------------------- шаги сценария


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Flow.city)
    logger.info(
        "New session: user_id=%s", message.from_user.id if message.from_user else None
    )
    await message.answer(GREETING, reply_markup=city_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. /start — выбрать город.\n"
        "2. Указать название сервиса (можно пропустить).\n"
        "3. Выбрать услугу — бот пришлёт пост с картинкой.\n\n"
        "Команда /cancel — начать заново."
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Сбросил. Нажмите /start, чтобы начать заново.")


@router.callback_query(F.data.startswith("city:"))
async def on_city_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    value = (callback.data or "").removeprefix("city:")
    await callback.answer()

    if callback.message is None:
        return

    if value == "__other__":
        await state.set_state(Flow.city_manual)
        await callback.message.edit_text(CITY_ASKED_OTHER)
        return

    await state.update_data(city=value)
    await state.set_state(Flow.sto_name)
    await callback.message.edit_text(
        f"Город: {value}\n\n{STO_ASKED}", reply_markup=skip_sto_keyboard()
    )


@router.message(Flow.city, F.text)
@router.message(Flow.city_manual, F.text)
async def on_city_typed(message: Message, state: FSMContext) -> None:
    """Город текстом — на случай, если человек не хочет жать кнопки."""
    city = (message.text or "").strip()[:60]
    if len(city) < 2:
        await message.answer("Название города слишком короткое. Попробуйте ещё раз.")
        return

    await state.update_data(city=city)
    await state.set_state(Flow.sto_name)
    await message.answer(
        f"Город: {city}\n\n{STO_ASKED}", reply_markup=skip_sto_keyboard()
    )


@router.message(Flow.sto_name, F.text)
async def on_sto_typed(message: Message, state: FSMContext) -> None:
    await _save_sto_and_ask_service(message, state, (message.text or "").strip()[:80])


@router.callback_query(F.data == "sto:skip")
async def on_sto_skipped(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return
    await _save_sto_and_ask_service(callback.message, state, "")


async def _save_sto_and_ask_service(
    message: Message, state: FSMContext, sto_name: str
) -> None:
    await state.update_data(sto_name=sto_name)
    await state.set_state(Flow.service)
    await message.answer(
        f"Сервис: {sto_name or 'без названия'}\n\n{SERVICE_ASKED}",
        reply_markup=service_keyboard(),
    )


@router.callback_query(F.data.startswith("service:"))
async def on_service_chosen(
    callback: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    service = (callback.data or "").removeprefix("service:")
    await callback.answer()

    if callback.message is None:
        return

    await state.update_data(service=service)
    await state.set_state(Flow.decision)
    await _safe_edit(callback.message, f"Тема: {service}\n\nЗапускаю генерацию…")

    data = await state.get_data()
    await _generate_and_send(bot, callback.message.chat.id, data)


@router.callback_query(F.data == "retry")
async def on_retry(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    if callback.message is None:
        return

    data = await state.get_data()
    if not data.get("service"):
        await callback.message.answer(
            "Данные потерялись. Нажмите /start и начнём заново."
        )
        return

    await _generate_and_send(bot, callback.message.chat.id, data)


@router.callback_query(F.data == "regen:yes")
async def on_regen_yes(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    if callback.message is None:
        return

    data = await state.get_data()
    if not data.get("service"):
        await callback.message.answer(
            "Данные потерялись. Нажмите /start и начнём заново."
        )
        return

    await _generate_and_send(bot, callback.message.chat.id, data)


@router.callback_query(F.data == "regen:no")
async def on_regen_no(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message is None:
        return

    await state.set_state(Flow.service)
    await callback.message.answer(
        "Ок! Выберите услугу, если нужен пост на другую тему.",
        reply_markup=service_keyboard(),
    )


# ---------------------------------------------------------------- генерация


async def _generate_and_send(bot: Bot, chat_id: int, data: dict[str, Any]) -> None:
    """Полный цикл: текст -> картинка -> отправка. Любая ошибка API не ломает бота."""
    if chat_id in _inflight:
        await bot.send_message(chat_id, "Генерация уже идёт, подождите пару секунд.")
        return
    _inflight.add(chat_id)

    city = str(data.get("city") or "")
    sto_name = str(data.get("sto_name") or "")
    service = str(data.get("service") or "")

    status = await bot.send_message(
        chat_id, "⏳ Генерирую контент... это займет 15-30 секунд"
    )

    try:
        # 1. Текст
        async with keep_chat_action(bot, chat_id, ChatAction.TYPING):
            try:
                text = await generate_post(
                    city=city, sto_name=sto_name, service=service
                )
            except LLMError as exc:
                logger.error("LLM generation failed: %s", exc)
                await _safe_edit(status, LLM_FAIL_TEXT)
                await bot.send_message(
                    chat_id, "Что делаем дальше?", reply_markup=retry_keyboard()
                )
                return

        # 2. Картинка (не критична)
        await _safe_edit(status, "🎨 Подбираю изображение под пост...")
        image: bytes | None = None
        try:
            async with keep_chat_action(bot, chat_id, ChatAction.UPLOAD_PHOTO):
                image = await generate_image(service)
        except ImageError as exc:
            logger.error("Image generation failed, fallback to text only: %s", exc)

        # 3. Доставка
        await _deliver(bot, chat_id, text, image)
        logger.info(
            "Post delivered: chat_id=%s, service=%s, with_image=%s",
            chat_id,
            service,
            image is not None,
        )

        await bot.send_message(
            chat_id,
            "Сгенерировать ещё один вариант?",
            reply_markup=decision_keyboard(),
        )
    finally:
        _inflight.discard(chat_id)
        with suppress(TelegramAPIError):
            await status.delete()


async def _deliver(bot: Bot, chat_id: int, text: str, image: bytes | None) -> None:
    if image is None:
        await bot.send_message(chat_id, IMAGE_FAIL_TEXT)
        await bot.send_message(chat_id, text)
        return

    caption = text
    too_long = len(text) > MAX_CAPTION
    if too_long:
        caption = text[: MAX_CAPTION - 1].rstrip() + "…"

    try:
        await bot.send_photo(
            chat_id,
            photo=BufferedInputFile(image, filename="sto_post.jpg"),
            caption=caption,
        )
    except TelegramAPIError:
        logger.exception("send_photo failed, sending text only")
        await bot.send_message(chat_id, IMAGE_FAIL_TEXT)
        await bot.send_message(chat_id, text)
        return

    if too_long:
        await bot.send_message(chat_id, text)


# ---------------------------------------------------------------- fallback


@router.message()
async def on_unexpected(message: Message, state: FSMContext) -> None:
    """Ловит всё, что не подошло другим хендлерам."""
    if await state.get_state() is None:
        await message.answer("Нажмите /start, чтобы начать генерацию поста.")
    else:
        await message.answer(
            "Не понял. Выберите вариант на клавиатуре или нажмите /cancel, чтобы начать заново."
        )


# ---------------------------------------------------------------- ошибки


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    logger.exception(
        "Unhandled error while processing update %s",
        event.update.update_id,
        exc_info=event.exception,
    )
    update = event.update
    chat_id = None
    if update.message is not None:
        chat_id = update.message.chat.id
    elif (
        update.callback_query is not None and update.callback_query.message is not None
    ):
        chat_id = update.callback_query.message.chat.id

    if chat_id is not None:
        with suppress(TelegramAPIError):
            await event.update.bot.send_message(
                chat_id, "Что-то пошло не так. Попробуйте ещё раз или нажмите /start."
            )
    return True


# ---------------------------------------------------------------- точка входа


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "sto-content-bot"})


async def start_health_server() -> web.AppRunner | None:
    """Поднимает /healthz на $PORT, если переменная задана.

    Нужно для Render/Railway: сервис без HTTP-порта считается background
    worker'ом (на Render это платный тип), а health-check не даёт платформе
    ругаться на «зависший» деплой. Локально PORT не задан — сервер не стартует.
    """
    raw_port = os.getenv("PORT", "").strip()
    if not raw_port:
        return None

    try:
        port = int(raw_port)
    except ValueError:
        logger.warning("PORT=%r не является числом, health-сервер не запущен", raw_port)
        return None

    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_get("/healthz", _health_handler)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Health server started on 0.0.0.0:%s (/healthz)", port)
    return runner


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)

    # Health-сервер поднимаем до обращения к Telegram: платформа должна
    # видеть живой порт с первой секунды.
    health_runner = await start_health_server()

    logger.info(
        "Bot starting: llm=%s, image=%s", settings.llm_model, settings.image_provider
    )
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    except TelegramUnauthorizedError:
        logger.error(
            "Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN в .env "
            "(токен должен быть от @BotFather, вида 1234567890:AA...)."
        )
        raise SystemExit(1)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()
        await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
