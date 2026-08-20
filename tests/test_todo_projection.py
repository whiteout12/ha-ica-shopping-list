"""The Todo projection must never expose ICA article metadata."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.ica_shopping_list.api import IcaList
from custom_components.ica_shopping_list.todo import IcaTodoList


def test_todo_projection_contains_only_uid_summary_and_status() -> None:
    entity = object.__new__(IcaTodoList)
    entity._list_id = "list-private"
    entity.coordinator = SimpleNamespace(data={
        "list-private": IcaList("list-private", "Private list", [{
            "id": "row-private", "text": "ris", "isStriked": False, "order": 0,
            "article": {
                "productEan": "PRIVATE-EAN", "articleGroupName": "Private group",
                "accessToken": "PRIVATE-TOKEN", "configEntry": "private-entry",
            },
        }]),
    })

    item = entity.todo_items[0]
    assert item.uid == "row-private"
    assert item.summary == "Ris"
    assert item.status.value == "needs_action"
    serialized = str(item)
    for private in ("PRIVATE-EAN", "Private group", "PRIVATE-TOKEN", "private-entry", "list-private"):
        assert private not in serialized
