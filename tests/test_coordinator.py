"""Selected-add path tests without a Home Assistant runtime."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.ica_shopping_list.api import IcaAuthRequired, IcaError
from custom_components.ica_shopping_list.coordinator import IcaCoordinator
from custom_components.ica_shopping_list import async_unload_entry
from custom_components.ica_shopping_list.const import DOMAIN
from homeassistant.helpers.update_coordinator import UpdateFailed


ARTICLE = {"name": "långkornigt ris"}


def _coordinator(api: object) -> IcaCoordinator:
    coordinator = object.__new__(IcaCoordinator)
    coordinator.api = api
    coordinator._fetch = AsyncMock(return_value={"list-1": object()})
    coordinator.async_set_updated_data = Mock()
    coordinator._renew = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


async def test_selected_add_posts_once_then_fetches_and_publishes_directly() -> None:
    api = Mock()
    api.add_suggestion = AsyncMock()
    coordinator = _coordinator(api)

    await coordinator.async_add_suggestion("list-1", "långkornigt ris", ARTICLE)

    api.add_suggestion.assert_awaited_once_with("list-1", "långkornigt ris", ARTICLE)
    coordinator._fetch.assert_awaited_once()
    coordinator.async_set_updated_data.assert_called_once_with(coordinator._fetch.return_value)
    coordinator._renew.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


async def test_selected_add_auth_failure_never_renews_or_retries() -> None:
    api = Mock()
    api.add_suggestion = AsyncMock(side_effect=IcaAuthRequired)
    coordinator = _coordinator(api)

    with pytest.raises(IcaAuthRequired):
        await coordinator.async_add_suggestion("list-1", "långkornigt ris", ARTICLE)

    api.add_suggestion.assert_awaited_once()
    coordinator._fetch.assert_not_awaited()
    coordinator._renew.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


async def test_selected_add_fetch_failure_never_renews_or_retries() -> None:
    api = Mock()
    api.add_suggestion = AsyncMock()
    coordinator = _coordinator(api)
    coordinator._fetch.side_effect = IcaError("offline")

    with pytest.raises(UpdateFailed, match="offline"):
        await coordinator.async_add_suggestion("list-1", "långkornigt ris", ARTICLE)

    api.add_suggestion.assert_awaited_once()
    coordinator._renew.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


async def test_entry_unload_clears_private_suggestion_state() -> None:
    coordinator = Mock()
    coordinator.suggestions.async_clear = AsyncMock()
    entry = Mock(entry_id="entry-1")
    hass = Mock()
    hass.data = {DOMAIN: {"entry-1": coordinator}}
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry)
    coordinator.suggestions.async_clear.assert_awaited_once()
    assert "entry-1" not in hass.data[DOMAIN]
