"""Feedback service: saved list, trash, restore/purge, and Notion sync."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.models.enriched import EnrichedApartment
from db import (
    ApartmentDecision,
    clear_apartment_feedback,
    count_feedback_apartments,
    delete_apartment_feedback,
    list_apartment_records_by_external_ids,
    list_apartment_records_by_urls,
    list_feedback_apartments,
    list_trashed_apartments,
    restore_apartment_feedback,
    tombstone_apartment_feedback,
    update_apartment_feedback_notion_sync,
    upsert_apartment_feedback,
    upsert_telegram_user,
)

# What a /trash restore actually did: a deleted-from-saved item goes back to the
# saved list; a rejected item has its rejection lifted so it can reappear in search.
RestoreOutcome = Literal["restored_to_saved", "unrejected"]


class NotionApartmentSync(Protocol):
    """Minimal sync contract for pushing saved apartments to Notion."""

    async def sync_apartment(
        self,
        apartment: EnrichedApartment,
        *,
        page_id: str | None = None,
    ) -> str: ...


class FeedbackService:
    """Owns explicit user decisions: 💾 save, 🚫 reject, trash and recovery."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        notion_sync: NotionApartmentSync | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._notion_sync = notion_sync

    async def get_saved_apartments(
        self,
        *,
        telegram_user_id: int,
        limit: int = 10,
    ) -> list[EnrichedApartment]:
        """Return recently saved apartments for one Telegram user."""
        async with self._session_factory() as session:
            return await list_feedback_apartments(
                session,
                telegram_user_id=telegram_user_id,
                decision="saved",
                limit=limit,
            )

    async def count_saved_apartments(self, *, telegram_user_id: int) -> int:
        """Total number of apartments the user has saved."""
        async with self._session_factory() as session:
            return await count_feedback_apartments(
                session,
                telegram_user_id=telegram_user_id,
                decision="saved",
            )

    async def delete_saved_apartment(
        self,
        *,
        telegram_user_id: int,
        external_id: str,
    ) -> bool:
        """Remove one apartment from the user's saved list (soft delete; recoverable)."""
        async with self._session_factory() as session:
            removed = await delete_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
            )
            await session.commit()
            return removed

    async def get_trashed_apartments(
        self,
        *,
        telegram_user_id: int,
        limit: int = 10,
    ) -> list[EnrichedApartment]:
        """Return recoverable apartments for the /trash list.

        Two kinds land here: items deleted from the saved list (soft-deleted
        "saved" feedback) and rejected items. Both can be brought back via
        :meth:`restore_apartment`. Rejected items come first (most likely the
        user's latest action), then deleted-from-saved, capped at ``limit``.
        """
        async with self._session_factory() as session:
            rejected = await list_feedback_apartments(
                session,
                telegram_user_id=telegram_user_id,
                decision="rejected",
                limit=limit,
            )
            deleted_saved = await list_trashed_apartments(
                session,
                telegram_user_id=telegram_user_id,
                limit=limit,
            )
        return (rejected + deleted_saved)[:limit]

    async def restore_apartment(
        self,
        *,
        telegram_user_id: int,
        external_id: str,
    ) -> RestoreOutcome | None:
        """Bring one apartment back from the trash.

        A deleted-from-saved item is un-deleted (back to the saved list); a
        rejected item has its rejection cleared so it can reappear in searches.
        Returns which happened, or None if nothing matched.
        """
        async with self._session_factory() as session:
            if await restore_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
            ):
                await session.commit()
                return "restored_to_saved"
            if await clear_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
                decision="rejected",
            ):
                await session.commit()
                return "unrejected"
            return None

    async def purge_trashed_apartment(
        self,
        *,
        telegram_user_id: int,
        external_id: str,
    ) -> bool:
        """Permanently dismiss a trashed apartment ("delete forever").

        Leaves /trash for good and stays hidden from search; not recoverable.
        Returns True if a trashed row was affected.
        """
        async with self._session_factory() as session:
            purged = await tombstone_apartment_feedback(
                session,
                telegram_user_id=telegram_user_id,
                external_id=external_id,
            )
            await session.commit()
            return purged

    async def save_apartment(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        external_id: str,
    ) -> bool:
        """Save one apartment (by krisha external id) to the user's list."""
        return await self._record_single_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            external_id=external_id,
            decision="saved",
        )

    async def reject_apartment(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        external_id: str,
    ) -> bool:
        """Reject one apartment so it is hidden from future manual searches."""
        return await self._record_single_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            external_id=external_id,
            decision="rejected",
        )

    async def _record_single_feedback(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        external_id: str,
        decision: ApartmentDecision,
    ) -> bool:
        async with self._session_factory() as session:
            records = await list_apartment_records_by_external_ids(
                session,
                external_ids=[external_id],
            )
        if not records:
            return False
        recorded = await self.record_apartment_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=[records[0].url],
            decision=decision,
        )
        return recorded > 0

    async def save_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ) -> int:
        """Persist a positive decision for the current apartment selection."""
        return await self.record_apartment_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=apartment_urls,
            decision="saved",
        )

    async def reject_apartments(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
    ) -> int:
        """Persist a negative decision for the current apartment selection."""
        return await self.record_apartment_feedback(
            telegram_user_id=telegram_user_id,
            username=username,
            apartment_urls=apartment_urls,
            decision="rejected",
        )

    async def record_apartment_feedback(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        apartment_urls: list[str],
        decision: ApartmentDecision,
    ) -> int:
        """Persist one user decision for the apartments currently in focus."""
        unique_urls = list(dict.fromkeys(apartment_urls))
        if not unique_urls:
            return 0

        user_id: int | None = None
        apartments_count = 0
        apartments_to_sync: list[tuple[uuid.UUID, EnrichedApartment, str | None]] = []

        async with self._session_factory() as session:
            user = await upsert_telegram_user(
                session,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            user_id = user.id
            apartments = await list_apartment_records_by_urls(
                session,
                urls=unique_urls,
            )
            if not apartments:
                return 0
            apartments_count = len(apartments)
            feedback_records = await upsert_apartment_feedback(
                session,
                user_id=user.id,
                apartments=apartments,
                decision=decision,
            )
            if decision == "saved" and self._notion_sync is not None:
                feedback_by_apartment_id = {
                    record.apartment_id: record
                    for record in feedback_records
                }
                apartments_to_sync = [
                    (
                        apartment.id,
                        EnrichedApartment.model_validate(apartment.payload),
                        feedback_by_apartment_id[apartment.id].notion_page_id,
                    )
                    for apartment in apartments
                    if apartment.id in feedback_by_apartment_id
                ]
            await session.commit()

        if (
            decision == "saved"
            and self._notion_sync is not None
            and user_id is not None
            and apartments_to_sync
        ):
            synced_pages = await self._sync_saved_apartments_to_notion(
                apartments_to_sync=apartments_to_sync,
            )
            if synced_pages:
                async with self._session_factory() as session:
                    await update_apartment_feedback_notion_sync(
                        session,
                        user_id=user_id,
                        synced_pages=synced_pages,
                        synced_at=datetime.now(UTC),
                    )
                    await session.commit()

        return apartments_count

    async def _sync_saved_apartments_to_notion(
        self,
        *,
        apartments_to_sync: list[tuple[uuid.UUID, EnrichedApartment, str | None]],
    ) -> dict[uuid.UUID, str]:
        """Best-effort Notion sync for saved apartments."""
        if self._notion_sync is None:
            return {}

        synced_pages: dict[uuid.UUID, str] = {}
        for apartment_id, apartment, page_id in apartments_to_sync:
            try:
                synced_page_id = await self._notion_sync.sync_apartment(
                    apartment,
                    page_id=page_id,
                )
            except Exception:
                continue
            synced_pages[apartment_id] = synced_page_id
        return synced_pages
