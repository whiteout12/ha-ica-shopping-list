"""Fetching, renewing, and the one place that decides when to ask for help."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Ica, IcaAuthRequired, IcaCredentialsRejected, IcaError, IcaList
from .const import CONF_COOKIES, DOMAIN, STORAGE_KEY, STORAGE_VERSION, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class IcaCoordinator(DataUpdateCoordinator[dict[str, IcaList]]):
    """Keeps one ICA session alive and the lists it can see up to date.

    The session outlives any single token and, if a password was saved, any
    single login. The ladder is deliberate:

        token expired      -> mint another, silently
        session gone       -> log in again through the stored cookies, silently
        password rejected  -> stop, and ask the user

    Only the last one reaches them. That is the whole point of offering to save
    the password: without it, the middle rung is missing and every expired
    session becomes a notification.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: Ica) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.entry = entry
        self.api = api
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")

    # -- session persistence ----------------------------------------------

    async def async_restore_session(self) -> None:
        """Reload the cookie jar, from the store or from the config flow.

        Restoring matters for more than saving a login: renewing *through* the
        old session's cookies is what makes the new session replace it. Starting
        from an empty jar would leave a live session behind on the account every
        single time, for as long as the integration runs.

        On a fresh install the store is empty and the jar comes from the config
        flow, which already signed in. Without that handoff a setup that chose
        not to save the password would ask to be repaired immediately, having
        just been told the correct one.
        """
        stored = await self._store.async_load()
        self.api.restore((stored or {}).get("cookies") or self.entry.data.get(CONF_COOKIES))

    async def async_save_session(self) -> None:
        await self._store.async_save({"cookies": self.api.cookies})

    async def async_forget_session(self) -> None:
        await self._store.async_remove()

    # -- the update loop ---------------------------------------------------

    async def _async_update_data(self) -> dict[str, IcaList]:
        try:
            return await self._fetch()
        except IcaAuthRequired:
            _LOGGER.debug("ICA session expired; trying to renew it")
        except IcaError as err:
            raise UpdateFailed(str(err)) from err

        await self._renew()
        try:
            return await self._fetch()
        except IcaError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> dict[str, IcaList]:
        return {entry.id: entry for entry in await self.api.lists()}

    async def _renew(self) -> None:
        """Log in again, or hand the problem to the user."""
        password = self.entry.data.get(CONF_PASSWORD)
        if not password:
            # By their choice at setup: no password is held, so nobody can
            # renew this but them. HA turns this into a notification and a
            # Reconfigure button.
            raise ConfigEntryAuthFailed(
                "Your ICA session has ended. Sign in again to continue.")
        try:
            await self.api.login(self.entry.data[CONF_USERNAME], password)
        except IcaCredentialsRejected as err:
            # Never retried: repeated failed logins lock an ICA account, and a
            # coordinator that retried would do so every five minutes forever.
            raise ConfigEntryAuthFailed(str(err)) from err
        except IcaError as err:
            raise UpdateFailed(f"Could not renew the ICA session: {err}") from err
        await self.async_save_session()
        _LOGGER.info("Renewed the ICA session")

    # -- writes ------------------------------------------------------------

    async def async_write(self, action: str, **kwargs: Any) -> None:
        """Run one write and refresh, so the card reflects it immediately.

        Everything here goes through the coordinator rather than the entity, so
        that a write always starts from the rows the last fetch returned — a PUT
        replaces the whole row, and a body built from anything else quietly
        blanks the fields it could not see.
        """
        try:
            await getattr(self, f"_write_{action}")(**kwargs)
        except IcaAuthRequired:
            await self._renew()
            await getattr(self, f"_write_{action}")(**kwargs)
        except IcaError as err:
            raise UpdateFailed(str(err)) from err
        await self.async_request_refresh()

    async def _write_add(self, list_id: str, text: str) -> None:
        await self.api.add_row(list_id, text)

    async def _write_update(self, list_id: str, row_id: str, **changes: Any) -> None:
        row = (self.data or {}).get(list_id)
        raw = row.row(row_id) if row else None
        if raw is None:
            raise UpdateFailed("That item is gone from ICA — it was removed elsewhere.")
        await self.api.update_row(raw, **changes)

    async def _write_delete(self, row_id: str, **_: Any) -> None:
        await self.api.delete_row(row_id)
