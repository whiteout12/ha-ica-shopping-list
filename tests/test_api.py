"""What ICA actually does, encoded as tests.

Each of these was a wrong assumption at some point during the reverse
engineering, and each one fails silently rather than loudly when it is wrong —
which is why they are pinned here rather than left as comments.

No Home Assistant anywhere: api.py does not import it, so its tests should not
drag one in either. That is not tidiness — starting Home Assistant for tests
that never needed it left one of its threads running past the cleanup check and
failed a suite in which every test had passed.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from custom_components.ica_shopping_list.api import (
    BASE_URL,
    LISTS_URL,
    Ica,
    IcaAuthRequired,
    IcaError,
)

INFO = f"{BASE_URL}/api/user/information"
TOKEN = "_0XBPWQQ_11111111-2222-3333-4444-555555555555"


def _info(login_state: int, token: str | None = TOKEN) -> dict:
    payload = {"loginState": login_state, "customerId": 1234567, "firstName": "Test"}
    if token:
        payload["accessToken"] = token
        payload["tokenExpires"] = "2099-01-01T00:00:00.1234567Z"
    return payload


def _sent(mocked: aioresponses, method: str, url: str) -> dict:
    """The JSON body of the last request to that endpoint."""
    return mocked.requests[(method, URL(url))][-1].kwargs["json"]


async def test_a_live_session_yields_a_token() -> None:
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        async with aiohttp.ClientSession() as session:
            assert await Ica(session).token() == TOKEN


async def test_login_state_two_is_a_live_session() -> None:
    """A password login in a browser profile that already held a session reports
    2, browses its own lists, and is in every way working. Requiring 1 rejected
    real sessions."""
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(2))
        async with aiohttp.ClientSession() as session:
            assert await Ica(session).token() == TOKEN


async def test_login_state_zero_is_dead_even_with_a_token() -> None:
    """A dead session keeps handing out an accessToken, and that token 403s
    against the list API. Trusting the token alone resurrects dead sessions."""
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(0))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(IcaAuthRequired):
                await Ica(session).token()


async def test_a_response_with_no_token_is_dead() -> None:
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1, token=None))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(IcaAuthRequired):
                await Ica(session).token()


async def test_a_rejected_token_asks_for_reauthentication() -> None:
    """403 from the list API means the session died under a token that was fine
    a minute ago — not a transport problem to retry forever."""
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        mocked.get(f"{LISTS_URL}/list/all", status=403)
        async with aiohttp.ClientSession() as session:
            with pytest.raises(IcaAuthRequired):
                await Ica(session).lists()


async def test_update_row_sends_the_whole_row() -> None:
    """PUT replaces a row. A body built from only the changed fields is accepted
    and blanks everything else — quantities being the ones people notice."""
    row = {
        "id": "row-1", "text": "mjölk", "isStriked": False, "order": 0,
        "quantity": {"amount": 3}, "shoppingListRowId": 1034461398,
        "article": {"id": 11103, "group": {"id": 10}},
    }
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        mocked.put(f"{LISTS_URL}/row/row-1", payload=row)
        async with aiohttp.ClientSession() as session:
            await Ica(session).update_row(row, isStriked=True)

        sent = _sent(mocked, "PUT", f"{LISTS_URL}/row/row-1")

    assert sent["isStriked"] is True
    assert sent["quantity"] == {"amount": 3}, "a quantity nobody mentioned was dropped"
    assert sent["shoppingListRowId"] == 1034461398
    assert sent["text"] == "mjölk"


async def test_add_row_lets_ica_classify_the_text() -> None:
    """`article: null` is accepted and ICA resolves the text itself, so there is
    no article to look up first."""
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        mocked.post(f"{LISTS_URL}/list/list-1/row", payload={"id": "row-9"})
        async with aiohttp.ClientSession() as session:
            await Ica(session).add_row("list-1", "bröd")

        sent = _sent(mocked, "POST", f"{LISTS_URL}/list/list-1/row")

    assert sent["article"] is None
    assert sent["quantity"] == {}, "creates send {}, reads return null — ICA's asymmetry"
    assert sent["text"] == "bröd"


async def test_a_row_without_an_id_never_leaves_the_process() -> None:
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(IcaError):
                await Ica(session).update_row({"text": "mjölk"}, isStriked=True)


async def test_a_delete_answers_with_nothing_and_that_is_success() -> None:
    """ICA returns 200 and an empty body. Reading that as a parse failure would
    report every successful delete as an error."""
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        mocked.delete(f"{LISTS_URL}/row/row-1", status=200, body="")
        async with aiohttp.ClientSession() as session:
            await Ica(session).delete_row("row-1")


async def test_lists_keeps_the_untouched_rows() -> None:
    with aioresponses() as mocked:
        mocked.get(INFO, payload=_info(1))
        mocked.get(f"{LISTS_URL}/list/all", payload=[
            {"id": "list-1", "name": "Att handla", "rows": [
                {"id": "row-1", "text": "mjölk", "isStriked": False,
                 "quantity": {"amount": 2}, "order": 0},
            ]},
        ])
        async with aiohttp.ClientSession() as session:
            lists = await Ica(session).lists()

    assert [entry.name for entry in lists] == ["Att handla"]
    assert lists[0].row("row-1")["quantity"] == {"amount": 2}
