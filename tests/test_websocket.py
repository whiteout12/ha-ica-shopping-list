"""Isolated Home Assistant WebSocket handler security tests.

The repository deliberately does not load pytest-homeassistant-custom-component.
These tests use faithful small doubles for the handler's HA APIs: permission
methods, entity registry lookup, state presence, config-entry lookup, and
WebSocket result/error methods. They exercise the command handlers directly,
including the required permission-before-registry ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.exceptions import Unauthorized

from custom_components.ica_shopping_list.const import DOMAIN
from custom_components.ica_shopping_list.coordinator import IcaCoordinator
from custom_components.ica_shopping_list.suggestions import Suggestions
from custom_components.ica_shopping_list import websocket


@dataclass
class RegistryEntry:
    entity_id: str
    unique_id: str
    config_entry_id: str | None
    domain: str = "todo"
    platform: str = DOMAIN


class Registry:
    def __init__(self, entries: dict[str, RegistryEntry]) -> None:
        self.entries = entries
        self.calls = 0

    def async_get(self, entity_id: str) -> RegistryEntry | None:
        self.calls += 1
        return self.entries.get(entity_id)


class Permissions:
    def __init__(self, all_policies: set[str] = set(), entities: set[tuple[str, str]] = set()) -> None:
        self.all_policies = all_policies
        self.entities = entities

    def access_all_entities(self, policy: str) -> bool:
        return policy in self.all_policies

    def check_entity(self, entity_id: str, policy: str) -> bool:
        return (entity_id, policy) in self.entities


class Connection:
    def __init__(self, permissions: Permissions) -> None:
        self.user = SimpleNamespace(permissions=permissions)
        self.results: list[tuple[int, dict]] = []
        self.errors: list[tuple[int, str, str]] = []

    def send_result(self, identifier: int, result: dict) -> None:
        self.results.append((identifier, result))

    def send_error(self, identifier: int, code: str, message: str) -> None:
        self.errors.append((identifier, code, message))


class SearchApi:
    def __init__(self) -> None:
        self.calls = 0

    async def search_articles(self, query: str) -> dict:
        self.calls += 1
        return {"documents": [ARTICLE]}


async def _call(handler, hass, connection, message) -> None:
    """Invoke the async command body without HA's scheduler wrapper."""
    await handler.__wrapped__(hass, connection, message)


ARTICLE = {
    "_id": "private-id", "id": 1, "name": "långkornigt ris",
    "pluralName": "långkornigt ris", "alternativeSpelling": None,
    "productEan": "PRIVATE-EAN", "storeArticleGroupId": 1,
    "expandedArticleGroupName": "Private group", "expandedArticleGroupId": 1,
    "articleGroupName": "Private group", "articleGroupId": 1, "status": 1,
    "latestChange": "2026-01-01T00:00:00Z", "maxiFormatCategoryId": None,
    "maxiFormatCategoryName": None, "kvantumFormatCategoryId": None,
    "kvantumFormatCategoryName": None, "supermarketFormatCategoryId": None,
    "supermarketFormatCategoryName": None, "naraFormatCategoryId": None,
    "naraFormatCategoryName": None,
}


def _coordinator(api: SearchApi | None = None) -> IcaCoordinator:
    coordinator = object.__new__(IcaCoordinator)
    coordinator.suggestions = Suggestions(api or SearchApi())
    coordinator.async_add_suggestion = AsyncMock()
    return coordinator


def _hass(entries: dict[str, RegistryEntry], coordinators: dict[str, IcaCoordinator]) -> SimpleNamespace:
    config_entries = {
        "one": SimpleNamespace(domain=DOMAIN, options={"lists": ["list-a", "list-b"]}),
        "two": SimpleNamespace(domain=DOMAIN, options={"lists": ["list-z"]}),
    }
    return SimpleNamespace(
        data={DOMAIN: coordinators},
        states=SimpleNamespace(get=lambda entity_id: object() if entity_id in entries else None),
        config_entries=SimpleNamespace(async_get_entry=lambda entry_id: config_entries.get(entry_id)),
    )


@pytest.fixture
def handler_env(monkeypatch):
    api = SearchApi()
    first = _coordinator(api)
    second = _coordinator()
    entries = {
        "todo.first": RegistryEntry("todo.first", "one_list-a", "one"),
        "todo.second": RegistryEntry("todo.second", "one_list-b", "one"),
        "todo.other": RegistryEntry("todo.other", "two_list-z", "two"),
    }
    registry = Registry(entries)
    monkeypatch.setattr(websocket.er, "async_get", lambda hass: registry)
    return _hass(entries, {"one": first, "two": second}), registry, api, first, second, entries


@pytest.mark.parametrize("permissions", [
    Permissions(all_policies={"read"}),
    Permissions(entities={("todo.first", "read")}),
])
async def test_suggestions_allow_all_entity_and_entity_scoped_read(handler_env, permissions) -> None:
    hass, _, _, _, _, _ = handler_env
    connection = Connection(permissions)
    await _call(websocket.websocket_suggestions, hass, connection, {
        "id": 1, "entity_id": "todo.first", "query": "ris", "limit": 8,
    })
    result = connection.results[0][1]
    assert result["suggestions"] and result["suggestions"][0]["text"] == "långkornigt ris"
    serialized = str(result)
    for private in ("PRIVATE-EAN", "private-id", "entry-one-private", "list-a"):
        assert private not in serialized


async def test_denied_permission_does_not_touch_registry_or_ica(handler_env) -> None:
    hass, registry, api, _, _, _ = handler_env
    with pytest.raises(Unauthorized):
        await _call(websocket.websocket_suggestions, hass, Connection(Permissions()), {
            "id": 1, "entity_id": "todo.first", "query": "ris", "limit": 8,
        })
    assert registry.calls == api.calls == 0


@pytest.mark.parametrize("change", [
    lambda entries: entries.update({"todo.first": RegistryEntry("todo.first", "one_list-a", "one", platform="other")}),
    lambda entries: entries.update({"todo.first": RegistryEntry("todo.first", "one_list-a", "one", domain="sensor")}),
    lambda entries: entries.update({"todo.first": RegistryEntry("todo.first", "one_list-a", None)}),
    lambda entries: entries.update({"todo.first": RegistryEntry("todo.first", "wrong_list-a", "one")}),
    lambda entries: entries.update({"todo.first": RegistryEntry("todo.first", "one_", "one")}),
    lambda entries: entries.update({"todo.first": RegistryEntry("todo.first", "one_not-configured", "one")}),
])
async def test_generic_or_malformed_entities_do_not_call_ica(handler_env, change) -> None:
    hass, _, api, _, _, entries = handler_env
    change(entries)
    connection = Connection(Permissions(all_policies={"read"}))
    await _call(websocket.websocket_suggestions, hass, connection, {
        "id": 1, "entity_id": "todo.first", "query": "ris", "limit": 8,
    })
    assert connection.errors[0][1] == "unsupported_entity"
    assert api.calls == 0


@pytest.mark.parametrize("permissions", [
    Permissions(all_policies={"control"}),
    Permissions(entities={("todo.first", "control")}),
])
async def test_add_all_entity_and_entity_scoped_control(handler_env, permissions) -> None:
    hass, _, _, first, _, _ = handler_env
    key = (await first.suggestions.async_suggest("todo.first", "list-a", "ris", 1))[0]
    connection = Connection(permissions)
    await _call(websocket.websocket_add_suggestion, hass, connection, {
        "id": 1, "entity_id": "todo.first", "selection_key": key["selection_key"],
        "text": key["text"],
    })
    assert connection.results == [(1, {"success": True})]
    first.async_add_suggestion.assert_awaited_once()


async def test_denied_control_does_not_consume_or_post(handler_env) -> None:
    hass, registry, _, first, _, _ = handler_env
    key = (await first.suggestions.async_suggest("todo.first", "list-a", "ris", 1))[0]
    with pytest.raises(Unauthorized):
        await _call(websocket.websocket_add_suggestion, hass, Connection(Permissions()), {
            "id": 1, "entity_id": "todo.first", "selection_key": key["selection_key"],
            "text": key["text"],
        })
    assert registry.calls == 0
    first.async_add_suggestion.assert_not_awaited()


async def test_selection_tamper_expiry_replay_and_entry_list_isolation(handler_env) -> None:
    hass, _, _, first, second, _ = handler_env
    control = Connection(Permissions(all_policies={"control"}))
    selected = (await first.suggestions.async_suggest("todo.first", "list-a", "ris", 1))[0]
    message = {"id": 1, "entity_id": "todo.first", "selection_key": selected["selection_key"], "text": "tampered"}
    await _call(websocket.websocket_add_suggestion, hass, control, message)
    assert control.errors[-1][1] == "invalid_selection"
    first.async_add_suggestion.assert_not_awaited()

    message["text"] = selected["text"]
    message["entity_id"] = "todo.second"
    await _call(websocket.websocket_add_suggestion, hass, control, message)
    assert control.errors[-1][1] == "invalid_selection"

    message["entity_id"] = "todo.other"
    await _call(websocket.websocket_add_suggestion, hass, control, message)
    assert control.errors[-1][1] == "invalid_selection"
    second.async_add_suggestion.assert_not_awaited()
    first.async_add_suggestion.assert_not_awaited()

    message["entity_id"] = "todo.first"
    await _call(websocket.websocket_add_suggestion, hass, control, message)
    first.async_add_suggestion.assert_awaited_once()
    await _call(websocket.websocket_add_suggestion, hass, control, message)
    assert control.errors[-1][1] == "invalid_selection"

    expired = (await first.suggestions.async_suggest("todo.first", "list-a", "ris", 1))[0]
    first.suggestions._selections[expired["selection_key"]].expires = -1
    await _call(websocket.websocket_add_suggestion, hass, control, {
        "id": 2, "entity_id": "todo.first", "selection_key": expired["selection_key"],
        "text": expired["text"],
    })
    assert control.errors[-1][1] == "expired_selection"
    assert first.async_add_suggestion.await_count == 1


def test_registration_is_idempotent(monkeypatch) -> None:
    register = Mock()
    monkeypatch.setattr(websocket.websocket_api, "async_register_command", register)
    hass = SimpleNamespace(data={})
    websocket.async_register_commands(hass)
    websocket.async_register_commands(hass)
    assert register.call_count == 2
