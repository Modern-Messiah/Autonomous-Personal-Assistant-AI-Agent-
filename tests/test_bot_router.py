"""Router integration tests for Telegram command flows."""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    SendChatAction,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Message, Update

from agent.locations import LocationInputError
from agent.models.apartment import Apartment
from agent.models.criteria import SearchCriteria
from agent.models.enriched import EnrichedApartment
from bot.app import create_dispatcher
from bot.service import (
    SEARCH_EXECUTION_ERROR_MESSAGE,
    ActiveCriteriaNotFoundError,
    CriteriaUnchangedError,
    MonitorStatus,
    NoPreferencesError,
    RecommendationResult,
    SearchExecution,
    SearchExecutionError,
)


class CapturingSession(BaseSession):
    """Session stub that records outgoing bot traffic (messages, photos,
    callback answers, edits, deletions) and tolerates typing indicators."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_texts: list[str] = []
        self.sent_photo_captions: list[str] = []
        self.callback_answers: list[str | None] = []
        self.cleared_keyboards = 0
        self.deleted_messages = 0

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: Any,
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        del timeout
        if isinstance(method, SendMessage):
            self.sent_texts.append(method.text)
            return Message.model_validate(
                {
                    "message_id": len(self.sent_texts),
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "text": method.text,
                },
                context={"bot": bot},
            )
        if isinstance(method, SendPhoto):
            self.sent_photo_captions.append(method.caption or "")
            return Message.model_validate(
                {
                    "message_id": 1000 + len(self.sent_photo_captions),
                    "date": 0,
                    "chat": {"id": method.chat_id, "type": "private"},
                    "caption": method.caption,
                },
                context={"bot": bot},
            )
        if isinstance(method, AnswerCallbackQuery):
            self.callback_answers.append(method.text)
            return True
        if isinstance(method, EditMessageReplyMarkup):
            self.cleared_keyboards += 1
            return True
        if isinstance(method, DeleteMessage):
            self.deleted_messages += 1
            return True
        if isinstance(method, SendChatAction):
            return True
        msg = f"Unexpected Telegram method in test: {type(method).__name__}"
        raise AssertionError(msg)

    async def stream_content(self, *args: Any, **kwargs: Any):
        del args, kwargs
        if False:
            yield b""


class FailingSearchService:
    """Minimal service stub that fails the command search path."""

    async def register_user(self, *, telegram_user_id: int, username: str | None) -> None:
        del telegram_user_id, username

    async def run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        query: str,
    ):
        del telegram_user_id, username, query
        raise SearchExecutionError()

    async def refine_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        message: str,
    ):
        del telegram_user_id, username, message
        raise AssertionError("Refine path should not be called in this test")

    async def get_active_criteria(self, *, telegram_user_id: int):
        del telegram_user_id
        return None

    async def get_saved_apartments(self, *, telegram_user_id: int, limit: int = 10):
        del telegram_user_id, limit
        return []

    async def save_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ):
        del telegram_user_id, username, apartment_urls
        return 0

    async def reject_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ):
        del telegram_user_id, username, apartment_urls
        return 0

    async def get_monitor_status(self, *, telegram_user_id: int):
        del telegram_user_id
        return None

    async def set_monitor_enabled(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        enabled: bool,
    ):
        del telegram_user_id, username, enabled
        return None

    async def set_monitor_interval(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        interval_minutes: int,
    ):
        del telegram_user_id, username, interval_minutes
        return None

    def get_default_monitor_status(self):
        return None


class InvalidLocationSearchService(FailingSearchService):
    """Service stub that reports an expected user-input location error."""

    async def run_search(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        query: str,
    ):
        del telegram_user_id, username, query
        raise LocationInputError("Бостандыкский район не относится к городу Астана.")


def build_command_update(*, text: str) -> Update:
    """Construct a minimal Telegram update for command routing tests."""

    command_length = len(text.split()[0])
    return Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 0,
                "chat": {"id": 77, "type": "private"},
                "from": {"id": 77, "is_bot": False, "first_name": "Denis"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": command_length}],
            },
        }
    )


def build_callback_update(*, data: str) -> Update:
    """Construct a minimal Telegram callback-query update (button press)."""

    return Update.model_validate(
        {
            "update_id": 2,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 77, "is_bot": False, "first_name": "Denis"},
                "chat_instance": "ci-1",
                "data": data,
                "message": {
                    "message_id": 55,
                    "date": 0,
                    "chat": {"id": 77, "type": "private"},
                    "text": "card",
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_search_command_replies_with_user_facing_error_when_search_fails() -> None:
    dispatcher = create_dispatcher(  # type: ignore[arg-type]
        service=FailingSearchService(),
        storage=MemoryStorage(),
    )
    session = CapturingSession()
    bot = Bot(token="123456:ABCDEF", session=session, default=DefaultBotProperties())

    await dispatcher.feed_update(bot, build_command_update(text="/search test"))

    assert session.sent_texts == [
        "Ищу варианты по заданным критериям...",
        SEARCH_EXECUTION_ERROR_MESSAGE,
    ]


@pytest.mark.asyncio
async def test_search_command_replies_with_location_validation_error() -> None:
    dispatcher = create_dispatcher(  # type: ignore[arg-type]
        service=InvalidLocationSearchService(),
        storage=MemoryStorage(),
    )
    session = CapturingSession()
    bot = Bot(token="123456:ABCDEF", session=session, default=DefaultBotProperties())

    await dispatcher.feed_update(bot, build_command_update(text="/search test"))

    assert session.sent_texts == [
        "Ищу варианты по заданным критериям...",
        "Бостандыкский район не относится к городу Астана.",
    ]


def build_criteria() -> SearchCriteria:
    return SearchCriteria(
        user_id=77,
        city="Almaty",
        deal_type="sale",
        property_type="apartment",
        max_price_kzt=45_000_000,
        rooms=[2],
    )


def build_enriched(external_id: str = "9001", *, photos: bool = True) -> EnrichedApartment:
    return EnrichedApartment(
        apartment=Apartment(
            external_id=external_id,
            source="krisha",
            url=f"https://krisha.kz/a/show/{external_id}",
            title=f"Apartment {external_id}",
            price_kzt=31_000_000,
            city="Almaty",
            rooms=2,
            area_m2=53.0,
            photos=[f"https://photos.krisha.kz/{external_id}/1.jpg"] if photos else [],
        )
    )


class StubService:
    """Recording service stub with knobs for every router path under test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.active_criteria: SearchCriteria | None = None
        self.search_result: SearchExecution | None = None
        self.saved: list[EnrichedApartment] = []
        self.trashed: list[EnrichedApartment] = []
        self.monitor_status: MonitorStatus | None = None
        self.save_apartment_result = True
        self.reject_apartment_result = True
        self.delete_saved_result = True
        self.restore_result: str | None = "restored_to_saved"
        self.purge_result = True
        self.refine_error: Exception | None = None
        self.recommend_error: Exception | None = None
        self.recommendation_result: RecommendationResult | None = None

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))

    async def register_user(self, *, telegram_user_id: int, username: str | None) -> None:
        self._record("register_user", telegram_user_id=telegram_user_id, username=username)

    async def run_search(self, *, telegram_user_id: int, username: str | None, query: str):
        self._record("run_search", query=query)
        if self.search_result is None:
            raise SearchExecutionError()
        return self.search_result

    async def refine_search(self, *, telegram_user_id: int, username: str | None, message: str):
        self._record("refine_search", message=message)
        if self.refine_error is not None:
            raise self.refine_error
        if self.search_result is None:
            raise ActiveCriteriaNotFoundError()
        return self.search_result

    async def get_active_criteria(self, *, telegram_user_id: int):
        return self.active_criteria

    async def get_saved_apartments(self, *, telegram_user_id: int, limit: int = 10):
        return self.saved[:limit]

    async def count_saved_apartments(self, *, telegram_user_id: int) -> int:
        return len(self.saved)

    async def get_trashed_apartments(self, *, telegram_user_id: int, limit: int = 10):
        return self.trashed[:limit]

    async def save_apartment(
        self, *, telegram_user_id: int, username: str | None, external_id: str
    ) -> bool:
        self._record("save_apartment", external_id=external_id)
        return self.save_apartment_result

    async def reject_apartment(
        self, *, telegram_user_id: int, username: str | None, external_id: str
    ) -> bool:
        self._record("reject_apartment", external_id=external_id)
        return self.reject_apartment_result

    async def delete_saved_apartment(self, *, telegram_user_id: int, external_id: str) -> bool:
        self._record("delete_saved_apartment", external_id=external_id)
        return self.delete_saved_result

    async def restore_apartment(self, *, telegram_user_id: int, external_id: str):
        self._record("restore_apartment", external_id=external_id)
        return self.restore_result

    async def purge_trashed_apartment(self, *, telegram_user_id: int, external_id: str) -> bool:
        self._record("purge_trashed_apartment", external_id=external_id)
        return self.purge_result

    async def recommend(self, *, telegram_user_id: int, username: str | None):
        self._record("recommend", telegram_user_id=telegram_user_id)
        if self.recommend_error is not None:
            raise self.recommend_error
        assert self.recommendation_result is not None
        return self.recommendation_result

    async def get_monitor_status(self, *, telegram_user_id: int):
        return self.monitor_status

    def get_default_monitor_status(self) -> MonitorStatus:
        return MonitorStatus(enabled=False, interval_minutes=360)

    async def set_monitor_enabled(
        self, *, telegram_user_id: int, username: str | None, enabled: bool
    ) -> MonitorStatus:
        self._record("set_monitor_enabled", enabled=enabled)
        return MonitorStatus(enabled=enabled, interval_minutes=360)

    async def set_monitor_interval(
        self, *, telegram_user_id: int, username: str | None, interval_minutes: int
    ) -> MonitorStatus:
        self._record("set_monitor_interval", interval_minutes=interval_minutes)
        return MonitorStatus(enabled=True, interval_minutes=interval_minutes)

    async def save_apartments(self, **kwargs: Any) -> int:
        return 0

    async def reject_apartments(self, **kwargs: Any) -> int:
        return 0


async def feed(service: StubService, update: Update) -> CapturingSession:
    dispatcher = create_dispatcher(  # type: ignore[arg-type]
        service=service,
        storage=MemoryStorage(),
    )
    session = CapturingSession()
    bot = Bot(token="123456:ABCDEF", session=session, default=DefaultBotProperties())
    await dispatcher.feed_update(bot, update)
    return session


@pytest.mark.asyncio
async def test_start_registers_user_and_shows_guide() -> None:
    service = StubService()
    session = await feed(service, build_command_update(text="/start"))

    assert ("register_user", {"telegram_user_id": 77, "username": None}) in service.calls
    assert len(session.sent_texts) == 1
    assert "Krisha Agent" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_help_shows_guide_without_registration() -> None:
    service = StubService()
    session = await feed(service, build_command_update(text="/help"))

    assert service.calls == []
    assert "Krisha Agent" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_search_without_args_shows_usage_hint() -> None:
    service = StubService()
    session = await feed(service, build_command_update(text="/search"))

    assert service.calls == []
    assert "Добавь поисковый запрос" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_search_success_sends_criteria_cards_and_followup() -> None:
    service = StubService()
    service.search_result = SearchExecution(
        criteria=build_criteria(),
        apartments=[build_enriched("9001"), build_enriched("9002")],
    )
    session = await feed(service, build_command_update(text="/search 2к Алматы"))

    assert ("run_search", {"query": "2к Алматы"}) in service.calls
    assert session.sent_texts[0] == "Ищу варианты по заданным критериям..."
    assert "Текущие критерии:" in session.sent_texts[1]
    # both listings arrive as photo cards, then the follow-up keyboard message
    assert len(session.sent_photo_captions) == 2
    assert "🏠 1." in session.sent_photo_captions[0]
    assert "🏠 2." in session.sent_photo_captions[1]
    assert session.sent_texts[-1] == "Что делаем дальше?"


@pytest.mark.asyncio
async def test_search_with_no_matches_reports_empty() -> None:
    service = StubService()
    service.search_result = SearchExecution(criteria=build_criteria(), apartments=[])
    session = await feed(service, build_command_update(text="/search вилла на луне"))

    assert session.sent_texts[-1] == "Подходящих квартир не найдено."


@pytest.mark.asyncio
async def test_criteria_without_active_search_prompts_search() -> None:
    service = StubService()
    session = await feed(service, build_command_update(text="/criteria"))

    assert session.sent_texts == [
        "Активные критерии не найдены. Сначала выполни поиск через /search."
    ]


@pytest.mark.asyncio
async def test_criteria_shows_active_criteria() -> None:
    service = StubService()
    service.active_criteria = build_criteria()
    session = await feed(service, build_command_update(text="/criteria"))

    assert "Текущие критерии:" in session.sent_texts[0]
    assert "Город: Almaty" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_cancel_clears_refinement_mode() -> None:
    session = await feed(StubService(), build_command_update(text="/cancel"))
    assert session.sent_texts == ["Режим уточнения критериев отменен."]


@pytest.mark.asyncio
async def test_list_empty_and_with_saved_apartments() -> None:
    service = StubService()
    session = await feed(service, build_command_update(text="/list"))
    assert "Сохранённых квартир пока нет" in session.sent_texts[0]

    service.saved = [build_enriched("9001")]
    session = await feed(service, build_command_update(text="/list"))
    assert "Сохранённые квартиры (1):" in session.sent_texts[0]
    assert len(session.sent_photo_captions) == 1


@pytest.mark.asyncio
async def test_trash_empty_shows_explainer() -> None:
    session = await feed(StubService(), build_command_update(text="/trash"))
    assert "Корзина пуста" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_refine_without_args_and_criteria_prompts_search() -> None:
    session = await feed(StubService(), build_command_update(text="/refine"))
    assert session.sent_texts == [
        "Активные критерии не найдены. Сначала выполни поиск через /search."
    ]


@pytest.mark.asyncio
async def test_refine_without_args_opens_guided_menu() -> None:
    service = StubService()
    service.active_criteria = build_criteria()
    session = await feed(service, build_command_update(text="/refine"))

    assert "Что изменить?" in session.sent_texts[0]
    assert "Текущие критерии:" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_refine_with_unrecognized_change_explains() -> None:
    service = StubService()
    service.refine_error = CriteriaUnchangedError()
    session = await feed(service, build_command_update(text="/refine абракадабра"))

    assert session.sent_texts[0] == "Уточняю критерии и запускаю поиск заново..."
    assert "Не удалось распознать изменение критериев." in session.sent_texts[1]  # noqa: RUF001


@pytest.mark.asyncio
async def test_monitor_status_on_off_and_interval() -> None:
    service = StubService()

    session = await feed(service, build_command_update(text="/monitor"))
    assert "Состояние: выключен" in session.sent_texts[0]

    session = await feed(service, build_command_update(text="/monitor on"))
    assert ("set_monitor_enabled", {"enabled": True}) in service.calls
    assert session.sent_texts[0].startswith("Мониторинг включен.")

    session = await feed(service, build_command_update(text="/monitor off"))
    assert ("set_monitor_enabled", {"enabled": False}) in service.calls
    assert session.sent_texts[0].startswith("Мониторинг выключен.")

    session = await feed(service, build_command_update(text="/monitor interval 6h"))
    assert ("set_monitor_interval", {"interval_minutes": 360}) in service.calls
    assert session.sent_texts[0].startswith("Интервал мониторинга обновлен.")

    session = await feed(service, build_command_update(text="/monitor interval"))
    assert "Укажи интервал после команды" in session.sent_texts[0]

    session = await feed(service, build_command_update(text="/monitor interval чуть-чуть"))
    assert "Некорректный интервал" in session.sent_texts[0]

    session = await feed(service, build_command_update(text="/monitor dance"))
    assert "Поддерживаются команды" in session.sent_texts[0]


@pytest.mark.asyncio
async def test_foryou_error_paths_explain_prerequisites() -> None:
    service = StubService()
    service.recommend_error = ActiveCriteriaNotFoundError()
    session = await feed(service, build_command_update(text="/foryou"))
    assert "Сначала задайте поиск через /search" in session.sent_texts[-1]

    service.recommend_error = NoPreferencesError()
    session = await feed(service, build_command_update(text="/foryou"))
    assert "Пока нечему учиться" in session.sent_texts[-1]


@pytest.mark.asyncio
async def test_foryou_with_no_fresh_recommendations() -> None:
    service = StubService()
    service.recommendation_result = RecommendationResult(
        criteria=build_criteria(), recommendations=[]
    )
    session = await feed(service, build_command_update(text="/foryou"))

    assert ("recommend", {"telegram_user_id": 77}) in service.calls
    assert "Сейчас нет свежих вариантов" in session.sent_texts[-1]


@pytest.mark.asyncio
async def test_save_callback_records_feedback_and_clears_keyboard() -> None:
    service = StubService()
    session = await feed(service, build_callback_update(data="apt:save:9001"))

    assert ("save_apartment", {"external_id": "9001"}) in service.calls
    assert session.cleared_keyboards == 1
    assert session.callback_answers == ["💾 Сохранено — доступно в /list"]


@pytest.mark.asyncio
async def test_save_callback_reports_missing_apartment() -> None:
    service = StubService()
    service.save_apartment_result = False
    session = await feed(service, build_callback_update(data="apt:save:404"))

    assert session.cleared_keyboards == 0
    assert session.callback_answers == ["Квартира не найдена"]


@pytest.mark.asyncio
async def test_reject_callback_records_feedback() -> None:
    service = StubService()
    session = await feed(service, build_callback_update(data="apt:reject:9001"))

    assert ("reject_apartment", {"external_id": "9001"}) in service.calls
    assert session.callback_answers == ["🚫 Отклонено — вернуть можно в /trash"]


@pytest.mark.asyncio
async def test_delete_saved_callback_removes_card() -> None:
    service = StubService()
    session = await feed(service, build_callback_update(data="saved:del:9001"))

    assert ("delete_saved_apartment", {"external_id": "9001"}) in service.calls
    assert session.deleted_messages == 1
    assert session.callback_answers == ["🗑 Удалено (вернуть — /trash)"]


@pytest.mark.asyncio
async def test_restore_trash_callback_outcomes() -> None:
    service = StubService()
    session = await feed(service, build_callback_update(data="trash:restore:9001"))
    assert session.callback_answers == ["♻️ Восстановлено — снова в /list"]

    service.restore_result = "unrejected"
    session = await feed(service, build_callback_update(data="trash:restore:9001"))
    assert session.callback_answers == ["♻️ Отклонение снято — снова появится в поиске"]

    service.restore_result = None
    session = await feed(service, build_callback_update(data="trash:restore:9001"))
    assert session.callback_answers == ["Уже восстановлено"]


@pytest.mark.asyncio
async def test_purge_trash_callback_deletes_forever() -> None:
    service = StubService()
    session = await feed(service, build_callback_update(data="trash:purge:9001"))

    assert ("purge_trashed_apartment", {"external_id": "9001"}) in service.calls
    assert session.deleted_messages == 1
    assert session.callback_answers == ["🗑 Удалено навсегда — больше не покажу"]


@pytest.mark.asyncio
async def test_list_callback_sends_saved_list() -> None:
    service = StubService()
    service.saved = [build_enriched("9001")]
    session = await feed(service, build_callback_update(data="dialog:list"))

    assert "Сохранённые квартиры (1):" in session.sent_texts[0]
    assert session.callback_answers == [None]
