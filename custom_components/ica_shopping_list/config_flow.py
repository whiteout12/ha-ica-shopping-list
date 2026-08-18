"""Setup, reconfiguration and re-authentication.

Three steps that all end up in the same place — credentials, then which lists to
show — because there is nothing else to configure and no reason to make it feel
like more.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import (
    Ica,
    IcaCredentialsRejected,
    IcaError,
    IcaList,
    IcaUnexpectedResponse,
)
from .const import CONF_COOKIES, CONF_LISTS, CONF_SAVE_PASSWORD, DOMAIN

CREDENTIALS_SCHEMA = vol.Schema({
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
    vol.Required(CONF_SAVE_PASSWORD, default=True): bool,
})


def normalise_username(value: str) -> str:
    """ICA wants a personal identity number, YYYYMMDDNNNN.

    People type it with a hyphen or spaces because that is how it is written
    everywhere else, and ICA does not accept those. Stripping them here is the
    difference between "it works" and "ICA did not accept that".
    """
    return "".join(ch for ch in (value or "") if ch.isdigit())


async def _sign_in(hass, username: str, password: str) -> tuple[Ica, list[IcaList]]:
    """Log in and read the lists, so setup fails now rather than later."""
    # An explicit cookie jar. These cookies *are* the session, and Home
    # Assistant's session helper makes no promise about keeping them.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    api = Ica(session)
    await api.login(username, password)
    return api, await api.lists()


class IcaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add the integration, or repair it when a session has ended."""

    VERSION = 1

    def __init__(self) -> None:
        self._api: Ica | None = None
        self._lists: list[IcaList] = []
        self._credentials: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                username = normalise_username(user_input[CONF_USERNAME])
                self._api, self._lists = await _sign_in(
                    self.hass, username, user_input[CONF_PASSWORD])
            except IcaCredentialsRejected:
                errors["base"] = "invalid_auth"
            except IcaUnexpectedResponse:
                errors["base"] = "unexpected_response"
            except IcaError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(username)
                self._abort_if_unique_id_configured()
                self._credentials = {**user_input, CONF_USERNAME: username}
                return await self.async_step_lists()

        return self.async_show_form(
            step_id="user", data_schema=CREDENTIALS_SCHEMA, errors=errors)

    async def async_step_lists(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which ICA lists become to-do entities."""
        if user_input is not None:
            save = self._credentials.pop(CONF_SAVE_PASSWORD, True)
            data = {
                CONF_USERNAME: self._credentials[CONF_USERNAME],
                # The session this flow just signed in with, handed to setup.
                # Without it the integration would have to log in a second time
                # immediately — and if the password was not saved, it could not,
                # so a working setup would ask to be repaired the moment it
                # finished.
                CONF_COOKIES: self._api.cookies if self._api else [],
            }
            if save:
                # The one thing that lets the integration renew by itself. Held
                # in the config entry, which is where HA keeps such things.
                data[CONF_PASSWORD] = self._credentials[CONF_PASSWORD]
            return self.async_create_entry(
                title=self._credentials[CONF_USERNAME],
                data=data,
                options={CONF_LISTS: user_input[CONF_LISTS]},
            )

        return self.async_show_form(
            step_id="lists", data_schema=_lists_schema(self._lists))

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in again after a session ended.

        Offers the save-password choice again on purpose: the most likely reason
        someone is here is that they declined it and got tired of being asked.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                api, _ = await _sign_in(self.hass, entry.data[CONF_USERNAME],
                                        user_input[CONF_PASSWORD])
            except IcaCredentialsRejected:
                errors["base"] = "invalid_auth"
            except IcaUnexpectedResponse:
                errors["base"] = "unexpected_response"
            except IcaError:
                errors["base"] = "cannot_connect"
            else:
                data = {CONF_USERNAME: entry.data[CONF_USERNAME],
                        CONF_COOKIES: api.cookies}
                if user_input.get(CONF_SAVE_PASSWORD, True):
                    data[CONF_PASSWORD] = user_input[CONF_PASSWORD]
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_SAVE_PASSWORD, default=True): bool,
            }),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return IcaOptionsFlow()


class IcaOptionsFlow(OptionsFlow):
    """Change which lists are shown, without touching the credentials."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={CONF_LISTS: user_input[CONF_LISTS]})

        # Offer whatever ICA has now, so lists created since setup appear here —
        # and only here. Nothing arrives in Home Assistant uninvited.
        coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
        await coordinator.async_request_refresh()
        available = list((coordinator.data or {}).values())
        chosen = self.config_entry.options.get(CONF_LISTS, [])

        return self.async_show_form(
            step_id="init", data_schema=_lists_schema(available, chosen))


def _lists_schema(lists: list[IcaList], selected: list[str] | None = None):
    return vol.Schema({
        vol.Required(CONF_LISTS, default=selected or [entry.id for entry in lists]):
            SelectSelector(SelectSelectorConfig(
                options=[{"value": entry.id, "label": entry.name} for entry in lists],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )),
    })
