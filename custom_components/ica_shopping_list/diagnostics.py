"""Diagnostics, with the credentials taken out.

The first question on any bug report is whether the session renewed itself or
asked the user, so that is what this leads with.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_LISTS, DOMAIN
from .coordinator import IcaCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: IcaCoordinator = hass.data[DOMAIN][entry.entry_id]
    lists = coordinator.data or {}
    return {
        # Not whether it is right, only whether one is held: without it the
        # integration cannot renew and every expired session becomes a prompt.
        "password_saved": CONF_PASSWORD in entry.data,
        "username_set": CONF_USERNAME in entry.data,
        "chosen_lists": len(entry.options.get(CONF_LISTS, [])),
        "lists_visible": len(lists),
        "rows_per_list": sorted(len(entry.raw_rows) for entry in lists.values()),
        "last_update_success": coordinator.last_update_success,
    }
