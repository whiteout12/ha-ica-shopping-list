"""Narrow WebSocket contract for ICA-backed article suggestions."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import IcaAuthRequired
from .const import CONF_LISTS, DOMAIN, INTEGRATION_VERSION
from .coordinator import IcaCoordinator
from .suggestions import SuggestionError

CONTRACT_VERSION = 1
_REGISTERED = "_websocket_registered"


def _strict_int(value: Any) -> int:
    if type(value) is not int:
        raise vol.Invalid("expected integer")
    return value


def _has_supported_contract(message: dict[str, Any]) -> bool:
    """Keep a numeric version mismatch a stable protocol error."""
    return message.get("contract_version", CONTRACT_VERSION) == CONTRACT_VERSION


def async_register_commands(hass: HomeAssistant) -> None:
    """Register once; config-entry reloads must not register duplicate commands."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(_REGISTERED):
        return
    websocket_api.async_register_command(hass, websocket_suggestions)
    websocket_api.async_register_command(hass, websocket_add_suggestion)
    data[_REGISTERED] = True


def _resolve_entity(hass: HomeAssistant, entity_id: str) -> tuple[IcaCoordinator, str]:
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None or registry_entry.domain != "todo" or registry_entry.platform != DOMAIN:
        raise SuggestionError("unsupported_entity")
    entry_id = registry_entry.config_entry_id
    if not entry_id or hass.states.get(entity_id) is None:
        raise SuggestionError("unsupported_entity")
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    entry = hass.config_entries.async_get_entry(entry_id)
    if (not isinstance(coordinator, IcaCoordinator) or entry is None
            or entry.domain != DOMAIN):
        raise SuggestionError("unsupported_entity")
    prefix = f"{entry_id}_"
    unique_id = registry_entry.unique_id
    if not unique_id.startswith(prefix):
        raise SuggestionError("unsupported_entity")
    list_id = unique_id.removeprefix(prefix)
    if not list_id or list_id not in entry.options.get(CONF_LISTS, []):
        raise SuggestionError("unsupported_entity")
    return coordinator, list_id


def _can_access(connection: websocket_api.ActiveConnection, entity_id: str, policy: str) -> bool:
    permissions = connection.user.permissions
    return permissions.access_all_entities(policy) or permissions.check_entity(entity_id, policy)


def _resolve_authorized_entity(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    entity_id: str,
    policy: str,
) -> tuple[IcaCoordinator, str]:
    """Check policy before looking up an entity, then resolve ICA ownership."""
    if not _can_access(connection, entity_id, policy):
        raise Unauthorized
    return _resolve_entity(hass, entity_id)


def _send_error(
    connection: websocket_api.ActiveConnection, message: dict[str, Any], error: SuggestionError
) -> None:
    connection.send_error(message["id"], error.code, error.code.replace("_", " "))


@websocket_api.websocket_command({
    vol.Required("type"): "ica_shopping_list/suggestions",
    vol.Required("contract_version"): _strict_int,
    vol.Required("entity_id"): str,
    vol.Required("query"): str,
    vol.Optional("limit", default=8): vol.All(_strict_int, vol.Range(min=1, max=10)),
})
@websocket_api.async_response
async def websocket_suggestions(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, message: dict[str, Any]
) -> None:
    entity_id = message["entity_id"]
    if not _has_supported_contract(message):
        _send_error(connection, message, SuggestionError("unsupported_contract"))
        return
    try:
        coordinator, list_id = _resolve_authorized_entity(
            hass, connection, entity_id, POLICY_READ
        )
        suggestions = await coordinator.suggestions.async_suggest(
            entity_id, list_id, message["query"], message["limit"]
        )
    except SuggestionError as err:
        _send_error(connection, message, err)
        return
    connection.send_result(message["id"], {
        "contract_version": CONTRACT_VERSION,
        "integration_version": INTEGRATION_VERSION,
        "entity_id": entity_id,
        "query": " ".join(message["query"].split()),
        "add_strategy": "ica_add_suggestion",
        "suggestions": suggestions,
    })


@websocket_api.websocket_command({
    vol.Required("type"): "ica_shopping_list/add_suggestion",
    vol.Required("contract_version"): _strict_int,
    vol.Required("entity_id"): str,
    vol.Required("selection_key"): str,
    vol.Required("text"): str,
})
@websocket_api.async_response
async def websocket_add_suggestion(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, message: dict[str, Any]
) -> None:
    entity_id = message["entity_id"]
    if not _has_supported_contract(message):
        _send_error(connection, message, SuggestionError("unsupported_contract"))
        return
    try:
        coordinator, list_id = _resolve_authorized_entity(
            hass, connection, entity_id, POLICY_CONTROL
        )
        article = await coordinator.suggestions.async_consume(
            message["selection_key"], entity_id, list_id, message["text"]
        )
        await coordinator.async_add_suggestion(
            list_id, " ".join(message["text"].split()), article
        )
    except SuggestionError as err:
        _send_error(connection, message, err)
        return
    except IcaAuthRequired:
        _send_error(connection, message, SuggestionError("auth_required"))
        return
    except UpdateFailed:
        _send_error(connection, message, SuggestionError("failed"))
        return
    connection.send_result(message["id"], {"success": True})
