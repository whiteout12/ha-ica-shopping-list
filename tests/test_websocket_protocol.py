"""Real Home Assistant WebSocket command registration and dispatch tests.

This deliberately uses Home Assistant's actual ``HomeAssistant`` and
``ActiveConnection`` classes rather than the custom handler doubles in
``test_websocket.py``. It stops the narrowly initialized core in every test;
no HTTP listener, config flow, or pytest HA plugin is started.
"""

from __future__ import annotations

import json
import logging
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.auth.models import RefreshToken, User
from homeassistant.auth.permissions.models import PermissionLookup
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.http import WebSocketAdapter
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ica_shopping_list.const import DOMAIN
from custom_components.ica_shopping_list.coordinator import IcaCoordinator
from custom_components.ica_shopping_list.suggestions import Suggestions
from custom_components.ica_shopping_list.websocket import async_register_commands


ARTICLE = {
    "_id": "private-id", "id": 1, "name": "långkornigt ris",
    "pluralName": "långkornigt ris", "alternativeSpelling": None,
    "productEan": "PRIVATE-EAN", "storeArticleGroupId": 1,
    "expandedArticleGroupName": "Ris", "expandedArticleGroupId": 1,
    "articleGroupName": "Ris", "articleGroupId": 1, "status": 1,
    "latestChange": "2026-01-01T00:00:00Z", "maxiFormatCategoryId": None,
    "maxiFormatCategoryName": None, "kvantumFormatCategoryId": None,
    "kvantumFormatCategoryName": None, "supermarketFormatCategoryId": None,
    "supermarketFormatCategoryName": None, "naraFormatCategoryId": None,
    "naraFormatCategoryName": None,
}


class Api:
    async def search_articles(self, query: str) -> dict:
        return {"documents": [ARTICLE]}


def _coordinator() -> IcaCoordinator:
    coordinator = object.__new__(IcaCoordinator)
    coordinator.suggestions = Suggestions(Api())
    coordinator.async_add_suggestion = AsyncMock()
    return coordinator


def _user(hass: HomeAssistant, *, owner: bool) -> tuple[User, RefreshToken]:
    lookup = PermissionLookup(er.async_get(hass), dr.async_get(hass))
    user = User("protocol", lookup, is_owner=owner, is_active=True)
    return user, RefreshToken(user, None, timedelta(minutes=5))


def _connection(hass: HomeAssistant, *, owner: bool) -> tuple[ActiveConnection, list[dict]]:
    sent: list[dict] = []
    user, token = _user(hass, owner=owner)
    connection = ActiveConnection(
        WebSocketAdapter(logging.getLogger(__name__), {"connid": "test"}),
        hass,
        lambda message: sent.append(
            json.loads(message) if isinstance(message, (bytes, bytearray, str)) else message
        ),
        user,
        token,
    )
    return connection, sent


async def _dispatch(hass: HomeAssistant, connection: ActiveConnection, message: dict) -> None:
    connection.async_handle(message)
    # WebSocket async handlers are deliberately Home Assistant background tasks.
    # Yield until their actual protocol response reaches this real connection.
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
async def websocket_hass(tmp_path):
    """Build only the real core objects required by WebSocket dispatch."""
    hass = HomeAssistant(str(tmp_path))
    coordinator = _coordinator()
    entry = SimpleNamespace(domain=DOMAIN, options={"lists": ["list-a"]})
    hass.config_entries = SimpleNamespace(async_get_entry=lambda entry_id: entry if entry_id == "entry" else None)
    await er.async_load(hass)
    await dr.async_load(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "todo", DOMAIN, "entry_list-a", suggested_object_id="ica",
        config_entry=SimpleNamespace(entry_id="entry", pref_disable_new_entities=False),
    )
    hass.states.async_set("todo.ica", "ok")
    hass.data[DOMAIN] = {"entry": coordinator}
    async_register_commands(hass)
    try:
        yield hass, coordinator
    finally:
        await hass.async_stop(force=True)


async def test_real_protocol_registers_dispatches_and_returns_suggestions_and_add(websocket_hass) -> None:
    hass, coordinator = websocket_hass
    assert "ica_shopping_list/suggestions" in hass.data[websocket_api.DOMAIN]
    assert "ica_shopping_list/add_suggestion" in hass.data[websocket_api.DOMAIN]
    connection, sent = _connection(hass, owner=True)

    await _dispatch(hass, connection, {
        "id": 1, "type": "ica_shopping_list/suggestions", "contract_version": 1,
        "entity_id": "todo.ica", "query": "ris", "limit": 1,
    })
    suggestion = sent[-1]
    assert suggestion["type"] == "result"
    result = suggestion["result"]
    assert result["add_strategy"] == "ica_add_suggestion"
    assert set(result["suggestions"][0]) == {"selection_key", "text", "primary", "secondary"}

    await _dispatch(hass, connection, {
        "id": 2, "type": "ica_shopping_list/add_suggestion", "contract_version": 1,
        "entity_id": "todo.ica", "selection_key": result["suggestions"][0]["selection_key"],
        "text": "långkornigt ris",
    })
    assert sent[-1] == {"id": 2, "type": "result", "success": True, "result": {"success": True}}
    coordinator.async_add_suggestion.assert_awaited_once()
    connection.async_handle_close()


async def test_real_protocol_returns_schema_unauthorized_and_invalid_entity_errors(websocket_hass) -> None:
    hass, _ = websocket_hass
    authorized, sent = _connection(hass, owner=True)
    await _dispatch(hass, authorized, {
        "id": 1, "type": "ica_shopping_list/suggestions", "contract_version": 2,
        "entity_id": "todo.ica", "query": "ris",
    })
    assert sent[-1]["error"]["code"] == "unsupported_contract"

    await _dispatch(hass, authorized, {
        "id": 2, "type": "ica_shopping_list/suggestions", "contract_version": 1,
        "entity_id": "todo.missing", "query": "ris",
    })
    assert sent[-1]["error"]["code"] == "unsupported_entity"
    await _dispatch(hass, authorized, {
        "id": 3, "type": "ica_shopping_list/suggestions", "contract_version": 1,
        "entity_id": "todo.ica", "query": "ris",
    })
    assert sent[-1]["type"] == "result"
    await _dispatch(hass, authorized, {
        "id": 4, "type": "ica_shopping_list/suggestions", "contract_version": 1,
        "entity_id": "todo.ica", "query": "ris", "limit": 11,
    })
    assert sent[-1]["error"]["code"] == "invalid_format"
    authorized.async_handle_close()

    unauthorized, denied = _connection(hass, owner=False)
    await _dispatch(hass, unauthorized, {
        "id": 1, "type": "ica_shopping_list/add_suggestion", "contract_version": 2,
        "entity_id": "todo.ica", "selection_key": "tampered", "text": "ris",
    })
    assert denied[-1]["error"]["code"] == "unsupported_contract"
    await _dispatch(hass, unauthorized, {
        "id": 2, "type": "ica_shopping_list/add_suggestion", "contract_version": 1,
        "entity_id": "todo.ica", "selection_key": "tampered", "text": "ris",
    })
    assert denied[-1]["error"]["code"] == "unauthorized"
    unauthorized.async_handle_close()
