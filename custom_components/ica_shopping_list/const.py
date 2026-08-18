"""Constants and storage keys."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "ica_shopping_list"

CONF_LISTS = "lists"
CONF_SAVE_PASSWORD = "save_password"
# The session handed from the config flow to setup, so a fresh install does
# not have to log in twice — which it could not do at all without a saved
# password. Superseded by the stored jar as soon as one exists.
CONF_COOKIES = "cookies"

# Shopping lists do not change by the second, and a slower poll keeps well clear
# of anything ICA might rate-limit. Local edits refresh immediately regardless.
UPDATE_INTERVAL = timedelta(minutes=5)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.session"
