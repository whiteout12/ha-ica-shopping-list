"""ICA shopping lists for Home Assistant."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import Ica
from .const import DOMAIN
from .coordinator import IcaCoordinator
from .websocket import async_register_commands

PLATFORMS = [Platform.TODO]


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    """Register the integration-wide WebSocket commands once."""
    async_register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Keep direct config-entry setup safe for test harnesses and old reload
    # paths that did not invoke async_setup first.
    async_register_commands(hass)
    # An explicit cookie jar of its own. These cookies *are* the ICA session,
    # and Home Assistant's shared session is shared with everything else.
    api = Ica(async_create_clientsession(
        hass, cookie_jar=aiohttp.CookieJar(quote_cookie=False)))
    coordinator = IcaCoordinator(hass, entry, api)
    await coordinator.async_restore_session()

    await coordinator.async_config_entry_first_refresh()
    # Move the config flow's session into the store, so the one in the entry is
    # only ever a first-run handoff and the store is the single source after.
    await coordinator.async_save_session()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload_on_options_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            await coordinator.suggestions.async_clear()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Leave nothing behind: the stored session is a live credential."""
    coordinator: IcaCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_forget_session()


async def _reload_on_options_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Changing the chosen lists adds and removes entities, which needs a reload."""
    await hass.config_entries.async_reload(entry.entry_id)
