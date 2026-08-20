"""ICA's web API, and only the parts a shopping list needs.

Every fact in here was measured against production rather than inferred, by a
throwaway soak harness built for the purpose. The three that shape this module:

* **Authentication is a token, not a status code.** An expired session returns
  HTTP 200 with `loginState: 0` — and *still hands out an accessToken*, which
  403s against the list API. So a session is live when a token came back **and**
  `loginState` is not 0. Neither half alone is enough; both were tried.
* **A login inherits the cookie jar it is given.** Logging in with an empty jar
  creates a second session and leaves the old one running. Logging in through
  the jar that holds the previous session replaces it. That is why `Ica` owns a
  jar for its lifetime and why it is persisted: without it, every renewal would
  strand a live session on the account.
* **`PUT` replaces a whole row.** There is no patch, no etag, no version. An
  update built from anything less than ICA's own row silently blanks the rest —
  including quantities, which this integration never sets but people do.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from html.parser import HTMLParser
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.ica.se"
IDP_URL = "https://ims.icagruppen.se"
LISTS_URL = "https://apimgw-pub.ica.se/sverige/digx/shopping-list/v1/api"
ARTICLE_SEARCH_URL = (
    "https://apimgw-pub.ica.se/sverige/digx/shoppinglistarticlesearch/v1/search"
)

INFO_PATH = "/api/user/information"
LOGIN_PATH = "/logga-in/"
AUTHENTICATOR_PATH = "/authn/authenticate/IcaCustomers"
AUTHORIZE_PATH = "/oauth/v2/authorize"
COOKIE_NAME = "thSessionId"

# The token is re-minted lazily and lives about 4m50s, so it is fetched on
# demand and never persisted. A minute of slack keeps a long call from finishing
# with an expired one.
TOKEN_SLACK = timedelta(seconds=60)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class IcaError(Exception):
    """ICA could not be reached, or answered something unusable."""


class IcaUnexpectedResponse(IcaError):
    """ICA was reached and answered something this code does not understand.

    Kept apart from a transport failure on purpose. "Could not reach ICA" sent
    somebody looking at their network when the real answer was that the login
    form had changed, or that the session cookie never arrived.
    """


class IcaAuthRequired(IcaError):
    """The session is gone and only a password can get a new one."""


class IcaCredentialsRejected(IcaAuthRequired):
    """ICA refused these credentials. Never retry this — accounts lock."""


class _FormParser(HTMLParser):
    """Collects one form's fields. Values matter: the IdP's response to a
    successful login is an auto-submitting form carrying the grant."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.fields: dict[str, str] = {}
        self._in_form = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._in_form = True
            self.action = attributes.get("action", "")
        elif tag == "input" and self._in_form and attributes.get("name"):
            self.fields[attributes["name"]] = attributes.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in_form = False


def dump_cookies(jar: aiohttp.CookieJar) -> list[dict[str, Any]]:
    """Freeze a jar for HA's store. Explicit fields rather than aiohttp's pickle
    format, so a stored session stays readable and restorable across versions."""
    return [
        {
            "name": morsel.key,
            "value": morsel.value,
            "domain": morsel["domain"],
            "path": morsel["path"] or "/",
            "expires": morsel["expires"],
            "secure": bool(morsel["secure"]),
        }
        for morsel in jar
    ]


def load_cookies(jar: aiohttp.CookieJar, records: list[dict[str, Any]] | None) -> None:
    """Restore a frozen jar. A record that cannot be rebuilt is skipped: a
    damaged jar should cost a login, never the whole integration."""
    for record in records or []:
        try:
            cookie: SimpleCookie = SimpleCookie()
            cookie[record["name"]] = record["value"]
            morsel = cookie[record["name"]]
            domain = record.get("domain") or ""
            morsel["domain"] = domain
            morsel["path"] = record.get("path") or "/"
            if record.get("expires"):
                morsel["expires"] = record["expires"]
            if record.get("secure"):
                morsel["secure"] = True
            jar.update_cookies({record["name"]: morsel},
                               URL(f"https://{domain.lstrip('.') or 'www.ica.se'}/"))
        except (KeyError, TypeError, ValueError):
            _LOGGER.debug("Skipping an unreadable stored cookie")


@dataclass
class IcaList:
    """One shopping list, with its rows exactly as ICA sent them.

    `raw_rows` is not a convenience — an update has to be built from ICA's own
    row, so the untouched original is the thing worth keeping.
    """

    id: str
    name: str
    raw_rows: list[dict[str, Any]]

    def row(self, row_id: str) -> dict[str, Any] | None:
        return next((r for r in self.raw_rows if r.get("id") == row_id), None)


class Ica:
    """A single ICA session: its cookies, its token, and the calls it can make."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        # The session must own a jar nobody else writes to. Logging in through
        # a shared jar would mix ICA's cookies with every other integration's.
        self._session = session
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._token_lock = asyncio.Lock()

    # -- session state -----------------------------------------------------

    @property
    def cookies(self) -> list[dict[str, Any]]:
        return dump_cookies(self._session.cookie_jar)

    def restore(self, records: list[dict[str, Any]] | None) -> None:
        load_cookies(self._session.cookie_jar, records)

    # -- authentication ----------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        """Log in with a password. One attempt, never a retry.

        A rejected password re-renders the login form rather than returning 401,
        so "no grant came back" is how a refusal looks. Retrying that in a loop
        is how an ICA account gets locked, which is why the caller is handed
        `IcaCredentialsRejected` instead of a return value it might loop on.

        The jar is *not* cleared first. Logging in through the existing session's
        cookies is what makes the new session replace it rather than join it.
        """
        await self._get(f"{BASE_URL}{LOGIN_PATH}")

        _LOGGER.debug("After the entry point the jar holds: %s",
                      [m.key for m in self._session.cookie_jar] or "nothing")

        form_html = await self._get(f"{IDP_URL}{AUTHENTICATOR_PATH}")
        form = _FormParser()
        form.feed(form_html)
        if "password" not in form.fields:
            raise IcaUnexpectedResponse(
                f"ICA's login form has changed: expected a password field, saw "
                f"{sorted(form.fields) or 'none'}"
            )

        payload = dict(form.fields)          # keep any hidden fields untouched
        payload["userName"] = username
        payload["password"] = password
        grant_html = await self._post(
            f"{IDP_URL}{form.action or AUTHENTICATOR_PATH}", data=payload)

        grant = _FormParser()
        grant.feed(grant_html)
        if "token" not in grant.fields:
            raise IcaCredentialsRejected(
                "ICA did not accept that personal identity number or password.")

        await self._post(f"{IDP_URL}{grant.action or AUTHORIZE_PATH}", data=grant.fields)

        held = [m.key for m in self._session.cookie_jar]
        if COOKIE_NAME not in held:
            raise IcaUnexpectedResponse(
                f"Signed in, but no {COOKIE_NAME} was kept. The jar holds: "
                f"{held or 'nothing'}"
            )
        self._token = self._token_expires = None

    async def token(self) -> str:
        """A usable access token, minting one only when the cached one is stale."""
        if self._has_fresh_token():
            return self._token  # type: ignore[return-value]

        # List reads, normal writes, suggestions, and selected adds all share
        # this lock. A stale token must cause one mint, not one per concurrent
        # WebSocket request.
        async with self._token_lock:
            if self._has_fresh_token():
                return self._token  # type: ignore[return-value]
            return await self._mint_token()

    def _has_fresh_token(self) -> bool:
        return bool(
            self._token
            and self._token_expires
            and datetime.now(timezone.utc) + TOKEN_SLACK < self._token_expires
        )

    async def _mint_token(self) -> str:
        async with self._session.get(f"{BASE_URL}{INFO_PATH}",
                                     headers={"Accept": "application/json"},
                                     timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                raise IcaError(f"ICA answered HTTP {response.status} for the session check")
            payload = await response.json(content_type=None)

        token = payload.get("accessToken")
        # Both halves matter. A dead session still returns a token, and that
        # token 403s against the list API; requiring loginState == 1 instead
        # would reject the perfectly live sessions that report 2.
        if payload.get("loginState") == 0 or not token:
            raise IcaAuthRequired(
                f"ICA session is no longer valid (loginState="
                f"{payload.get('loginState')})")

        self._token = token
        self._token_expires = _parse_time(payload.get("tokenExpires")) or (
            datetime.now(timezone.utc) + timedelta(minutes=4))
        return token

    # -- shopping lists ----------------------------------------------------

    async def lists(self) -> list[IcaList]:
        payload = await self._api("GET", "/list/all")
        return [
            IcaList(id=entry["id"], name=entry.get("name") or "ICA",
                    raw_rows=[r for r in entry.get("rows") or [] if isinstance(r, dict)])
            for entry in payload or []
            if isinstance(entry, dict) and entry.get("id")
        ]

    async def add_row(self, list_id: str, text: str) -> dict[str, Any]:
        """Add an item. `article` is null on purpose — ICA classifies the text
        itself, so there is no article to look up first."""
        return await self._api("POST", f"/list/{list_id}/row", json={
            "isStriked": False, "quantity": {}, "text": text, "article": None})

    async def search_articles(self, query: str) -> Any:
        """Search ICA's article index using the current session token.

        The browser HAR omitted Authorization even on its authenticated row
        POST while its preflight requested it. Search therefore deliberately
        uses the same bearer path as all other ICA API calls. This method never
        logs in or retries an authentication failure.
        """
        return await self._authenticated_request(
            "GET", ARTICLE_SEARCH_URL, endpoint="/article-search", params={"query": query}
        )

    async def add_suggestion(
        self, list_id: str, text: str, article: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Add one server-held search result exactly once.

        The article is intentionally not reconstructed here: the selection
        store has already validated and retained ICA's measured document.
        """
        return await self._api("POST", f"/list/{list_id}/row", json={
            "isStriked": False, "quantity": {}, "text": text, "article": article,
        })

    async def update_row(self, row: dict[str, Any], **changes: Any) -> dict[str, Any]:
        """Replace a row, changing only what was asked for.

        `row` must be ICA's own row. Building this body from anything smaller —
        a TodoItem, say — drops every field it cannot represent, and quantities
        are the ones people notice.
        """
        if not row.get("id"):
            raise IcaError("That row has no id and cannot be updated.")
        return await self._api("PUT", f"/row/{row['id']}", json={**row, **changes})

    async def delete_row(self, row_id: str) -> None:
        await self._api("DELETE", f"/row/{row_id}")

    # -- plumbing ----------------------------------------------------------

    async def _api(self, method: str, path: str, **kw: Any) -> Any:
        return await self._authenticated_request(
            method, f"{LISTS_URL}{path}", endpoint=_endpoint_label(path), **kw
        )

    async def _authenticated_request(
        self, method: str, url: str, *, endpoint: str, **kw: Any
    ) -> Any:
        token = await self.token()
        try:
            async with self._session.request(
                method, url,
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT, **kw,
            ) as response:
                if response.status in (401, 403):
                    # The token was fine minutes ago; the session died under it.
                    self._token = None
                    raise IcaAuthRequired(f"ICA refused the token (HTTP {response.status})")
                if response.status >= 400:
                    raise IcaError(
                        f"ICA answered HTTP {response.status} for {method} {endpoint}"
                    )
                # Read the body rather than trusting Content-Length: it is
                # absent on a chunked response, and a DELETE answers 200 with
                # nothing at all, which is a success and not a parse failure.
                body = (await response.text()).strip()
                if not body:
                    return None
                try:
                    return json.loads(body)
                except ValueError as err:
                    raise IcaUnexpectedResponse(
                        f"ICA answered {method} {endpoint} with something that is not "
                        f"JSON") from err
        except aiohttp.ClientError as err:
            raise IcaError(f"Could not reach ICA {endpoint}: {err}") from err

    async def _get(self, url: str) -> str:
        try:
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as response:
                return await response.text()
        except aiohttp.ClientError as err:
            raise IcaError(f"Could not reach {url}: {err}") from err

    async def _post(self, url: str, data: dict[str, str]) -> str:
        try:
            async with self._session.post(url, data=data,
                                          timeout=REQUEST_TIMEOUT) as response:
                return await response.text()
        except aiohttp.ClientError as err:
            raise IcaError(f"Could not reach {url}: {err}") from err


def _endpoint_label(path: str) -> str:
    """Describe an ICA endpoint without including list or row identifiers."""
    if path == "/list/all":
        return path
    if path.startswith("/list/"):
        return "/list/{list_id}/row"
    if path.startswith("/row/"):
        return "/row/{row_id}"
    return "/shopping-list"


def _parse_time(value: Any) -> datetime | None:
    """ICA sends .NET tick precision — seven fractional digits. Python parses
    six, so the tail is trimmed rather than letting the whole value fail."""
    if not isinstance(value, str) or not value:
        return None
    text = value.rstrip("Zz")
    if "." in text:
        head, _, fraction = text.partition(".")
        text = f"{head}.{fraction[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
