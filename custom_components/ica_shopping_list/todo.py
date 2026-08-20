"""One to-do entity per ICA shopping list."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LISTS, DOMAIN
from .coordinator import IcaCoordinator


def _as_ica_shows_it(text: str) -> str:
    """Capitalise the first character, the way ICA's own site and app do.

    Only the first character: `str.capitalize` would lower the rest and turn
    "ICA Basic mjölk" into "Ica basic mjölk". Anything that does not start with
    a letter — "3 äpplen" — is left exactly as it was.
    """
    return text[:1].upper() + text[1:]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: IcaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        IcaTodoList(coordinator, entry, list_id)
        for list_id in entry.options.get(CONF_LISTS, [])
    )


class IcaTodoList(CoordinatorEntity[IcaCoordinator], TodoListEntity):
    """An ICA list, as Home Assistant's to-do platform sees it.

    `TodoItem` has room for a summary, a status, a due date and a description.
    ICA rows carry a quantity as well, which has nowhere to go — so this
    integration never sets one, and every write goes through the coordinator so
    that the ones people set in the ICA app survive being ticked off here.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, coordinator: IcaCoordinator, entry: ConfigEntry,
                 list_id: str) -> None:
        super().__init__(coordinator)
        self._list_id = list_id
        self._attr_unique_id = f"{entry.entry_id}_{list_id}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "ICA",
            "manufacturer": "ICA",
            "entry_type": "service",
        }

    @property
    def _list(self):
        return (self.coordinator.data or {}).get(self._list_id)

    @property
    def available(self) -> bool:
        # A list deleted in the ICA app leaves its entity here, unavailable
        # rather than silently gone, so it is obvious something changed and the
        # selection can be corrected in the integration's options.
        return super().available and self._list is not None

    @property
    def name(self) -> str | None:
        entry = self._list
        return entry.name if entry else None

    @property
    def todo_items(self) -> list[TodoItem] | None:
        entry = self._list
        if entry is None:
            return None
        rows = sorted(entry.raw_rows, key=lambda r: r.get("order") or 0)
        return [
            TodoItem(
                uid=row.get("id"),
                summary=_as_ica_shows_it(row.get("text") or ""),
                status=(TodoItemStatus.COMPLETED if row.get("isStriked")
                        else TodoItemStatus.NEEDS_ACTION),
            )
            for row in rows
            if row.get("id")
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self.coordinator.async_write(
            "add", list_id=self._list_id, text=item.summary or "")

    async def async_update_todo_item(self, item: TodoItem) -> None:
        # Only the two fields ICA and Home Assistant agree on are sent. The rest
        # of the row is carried over untouched by the coordinator.
        await self.coordinator.async_write(
            "update", list_id=self._list_id, row_id=item.uid,
            text=item.summary or "",
            isStriked=item.status == TodoItemStatus.COMPLETED,
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self.coordinator.async_write("delete", row_id=uid)
