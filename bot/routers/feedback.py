"""Saved/trash/recommendation feature: /list, /trash, /foryou, card callbacks."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from agent.locations import LocationInputError
from bot.keyboards import (
    APT_REJECT_PREFIX,
    APT_SAVE_PREFIX,
    DELETE_SAVED_PREFIX,
    LIST_CALLBACK_DATA,
    PURGE_TRASH_PREFIX,
    RESTORE_TRASH_PREFIX,
)
from bot.routers.shared import RouterHelpers, typing_action
from bot.service import (
    ActiveCriteriaNotFoundError,
    NoPreferencesError,
    SearchBotService,
    SearchExecutionError,
)


def create_feedback_router(service: SearchBotService, helpers: RouterHelpers) -> Router:
    router = Router(name="krisha-feedback")

    @router.message(Command("list"))
    async def handle_list(message: Message) -> None:
        if message.from_user is None:
            return
        await helpers.send_saved_list(message, message.from_user.id)

    @router.message(Command("trash"))
    async def handle_trash(message: Message) -> None:
        if message.from_user is None:
            return
        await helpers.send_trash_list(message, message.from_user.id)

    @router.message(Command("foryou"))
    async def handle_foryou(message: Message) -> None:
        if message.from_user is None:
            return
        # Recommendation runs a live search (~30s); acknowledge immediately and
        # keep a typing indicator alive so the bot doesn't look frozen.
        await message.answer("⭐ Подбираю под ваш вкус — это займёт около минуты…")
        try:
            async with typing_action(message):
                result = await service.recommend(
                    telegram_user_id=message.from_user.id,
                    username=message.from_user.username,
                )
        except ActiveCriteriaNotFoundError:
            await message.answer(
                "Сначала задайте поиск через /search — потом /foryou подберёт под ваш вкус."
            )
            return
        except NoPreferencesError:
            await message.answer(
                "Пока нечему учиться. Сохраните несколько квартир кнопкой 💾 на карточке, "
                "и /foryou начнёт подбирать похожие."
            )
            return
        except (LocationInputError, SearchExecutionError) as exc:
            await message.answer(exc.user_message)
            return
        await helpers.send_recommendations(message, result)

    @router.callback_query(F.data.startswith(APT_SAVE_PREFIX))
    async def handle_apartment_save_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(APT_SAVE_PREFIX):]
        saved = await service.save_apartment(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            external_id=external_id,
        )
        if saved and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("💾 Сохранено — доступно в /list" if saved else "Квартира не найдена")

    @router.callback_query(F.data.startswith(APT_REJECT_PREFIX))
    async def handle_apartment_reject_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(APT_REJECT_PREFIX):]
        rejected = await service.reject_apartment(
            telegram_user_id=callback.from_user.id,
            username=callback.from_user.username,
            external_id=external_id,
        )
        if rejected and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(
            "🚫 Отклонено — вернуть можно в /trash" if rejected else "Квартира не найдена"
        )

    @router.callback_query(F.data == LIST_CALLBACK_DATA)
    async def handle_list_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        await helpers.send_saved_list(callback.message, callback.from_user.id)
        await callback.answer()

    @router.callback_query(F.data.startswith(DELETE_SAVED_PREFIX))
    async def handle_delete_saved_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(DELETE_SAVED_PREFIX):]
        removed = await service.delete_saved_apartment(
            telegram_user_id=callback.from_user.id,
            external_id=external_id,
        )
        # Remove the card (works for both photo and text messages, unlike
        # edit_text which fails on a photo message).
        if removed and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.delete()
        await callback.answer(
            "🗑 Удалено (вернуть — /trash)" if removed else "Уже удалено"
        )

    @router.callback_query(F.data.startswith(RESTORE_TRASH_PREFIX))
    async def handle_restore_trash_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(RESTORE_TRASH_PREFIX):]
        outcome = await service.restore_apartment(
            telegram_user_id=callback.from_user.id,
            external_id=external_id,
        )
        if outcome is not None and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.delete()
        if outcome == "restored_to_saved":
            text = "♻️ Восстановлено — снова в /list"
        elif outcome == "unrejected":
            text = "♻️ Отклонение снято — снова появится в поиске"
        else:
            text = "Уже восстановлено"
        await callback.answer(text)

    @router.callback_query(F.data.startswith(PURGE_TRASH_PREFIX))
    async def handle_purge_trash_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        external_id = callback.data[len(PURGE_TRASH_PREFIX):]
        purged = await service.purge_trashed_apartment(
            telegram_user_id=callback.from_user.id,
            external_id=external_id,
        )
        if purged and isinstance(callback.message, Message):
            with contextlib.suppress(Exception):
                await callback.message.delete()
        await callback.answer(
            "🗑 Удалено навсегда — больше не покажу" if purged else "Уже удалено"
        )

    return router
