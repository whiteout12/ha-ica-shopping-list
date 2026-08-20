"""Pure tests for the private article and opaque-key boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from custom_components.ica_shopping_list.api import IcaAuthRequired, IcaError
from custom_components.ica_shopping_list.suggestions import (
    ARTICLE_FIELDS,
    REQUIRED_ARTICLE_FIELDS,
    SELECTION_TTL,
    SuggestionError,
    Suggestions,
    QUERY_CACHE_SIZE,
    validate_article,
)

FIXTURES = Path(__file__).parent / "fixtures"
RIS = json.loads((FIXTURES / "article_search" / "ris.json").read_text())
RI = json.loads((FIXTURES / "article_search" / "ri.json").read_text())


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Api:
    def __init__(self, response: dict = RIS) -> None:
        self.response = response
        self.calls: list[str] = []

    async def search_articles(self, query: str) -> dict:
        self.calls.append(query)
        return self.response


def test_article_schema_is_exact_and_verbatim() -> None:
    document = RIS["documents"][0]
    article = validate_article(document)
    assert set(article) == set(ARTICLE_FIELDS)
    assert dict(article) == document
    with pytest.raises(TypeError):
        article["name"] = "changed"  # type: ignore[index]


def test_sparse_measured_documents_are_accepted_verbatim() -> None:
    sparse = RIS["documents"][2]
    article = validate_article(sparse)
    assert set(article) == set(sparse)
    assert REQUIRED_ARTICLE_FIELDS.issubset(article)
    assert "alternativeSpelling" not in article
    assert "maxiFormatCategoryId" not in article


@pytest.mark.parametrize("change", [
    lambda document: document.pop("id"),
    lambda document: document.update({"unexpected": "field"}),
    lambda document: document.update({"id": True}),
    lambda document: document.update({"name": {"nested": "value"}}),
    lambda document: document.update({"alternativeSpelling": {"nested": "value"}}),
])
def test_article_schema_rejects_non_measured_documents(change) -> None:
    document = dict(RIS["documents"][0])
    change(document)
    with pytest.raises(SuggestionError, match="unsupported_contract"):
        validate_article(document)


def test_sanitized_fixture_preserves_measured_top_level_shapes() -> None:
    assert RIS["stats"]["totalHits"] == 143
    assert isinstance(RIS["facets"], list)
    assert isinstance(RIS["spellSuggestions"], list)
    assert [document["name"] for document in RIS["documents"]].count("riscrisp") == 2


async def test_suggestions_preserve_order_duplicates_and_article_selection() -> None:
    api = Api()
    api.response = {"documents": [RIS["documents"][0], RIS["documents"][0]]}
    suggestions = Suggestions(api)
    public = await suggestions.async_suggest("todo.ica", "list-1", " ris ", 8)
    assert [item["text"] for item in public] == ["långkornigt ris", "långkornigt ris"]
    assert [item["primary"] for item in public] == ["Långkornigt ris", "Långkornigt ris"]
    assert public[0]["selection_key"] != public[1]["selection_key"]
    assert set(public[0]) == {"selection_key", "text", "primary", "secondary"}
    article = await suggestions.async_consume(
        public[0]["selection_key"], "todo.ica", "list-1", "långkornigt ris"
    )
    assert dict(article) == RIS["documents"][0]
    with pytest.raises(SuggestionError, match="invalid_selection"):
        await suggestions.async_consume(
            public[0]["selection_key"], "todo.ica", "list-1", "långkornigt ris"
        )


async def test_query_cache_and_selection_expiry_are_independent() -> None:
    clock = Clock()
    api = Api()
    suggestions = Suggestions(api, clock=clock)
    first = await suggestions.async_suggest("todo.ica", "list-1", "ris", 1)
    await suggestions.async_suggest("todo.ica", "list-1", "ris", 1)
    assert api.calls == ["ris"]
    clock.now = SELECTION_TTL - 1
    article = await suggestions.async_consume(
        first[0]["selection_key"], "todo.ica", "list-1", first[0]["text"]
    )
    assert article["_id"] == "article-long-rice"
    second = await suggestions.async_suggest("todo.ica", "list-1", "ris", 1)
    clock.now += SELECTION_TTL + 1
    with pytest.raises(SuggestionError, match="expired_selection"):
        await suggestions.async_consume(
            second[0]["selection_key"], "todo.ica", "list-1", second[0]["text"]
        )


async def test_empty_results_use_cache_and_invalid_boundaries_do_not_call_ica() -> None:
    api = Api(RI)
    suggestions = Suggestions(api)
    assert await suggestions.async_suggest("todo.ica", "list-1", "ri", 8) == []
    assert await suggestions.async_suggest("todo.ica", "list-1", "ri", 8) == []
    assert api.calls == ["ri"]
    for query in ("r", "\u202eris", "x" * 81):
        with pytest.raises(SuggestionError, match="invalid_query"):
            await suggestions.async_suggest("todo.ica", "list-1", query, 8)
    assert api.calls == ["ri"]


async def test_search_errors_are_safe_and_do_not_retry_authentication() -> None:
    class FailingApi:
        async def search_articles(self, query: str) -> dict:
            raise IcaAuthRequired

    suggestions = Suggestions(FailingApi())
    with pytest.raises(SuggestionError, match="auth_required"):
        await suggestions.async_suggest("todo.ica", "list-1", "ris", 8)


async def test_invalid_documents_are_skipped_but_malformed_top_level_fails(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    invalid = dict(RIS["documents"][0], unexpected="field")
    api = Api({"documents": [invalid, RIS["documents"][2]]})
    suggestions = Suggestions(api)
    public = await suggestions.async_suggest("todo.ica", "list-1", "ris", 8)
    assert [item["text"] for item in public] == ["Ris Diet"]
    assert "Skipped 1 invalid ICA article search documents" in caplog.text
    assert "unexpected" not in caplog.text

    malformed = Suggestions(Api({"documents": "not-a-list"}))
    with pytest.raises(SuggestionError, match="unsupported_contract"):
        await malformed.async_suggest("todo.ica", "list-1", "ris", 8)


async def test_result_cap_display_cap_and_secondary_fallback() -> None:
    fallback = dict(RIS["documents"][2], articleGroupName="")
    api = Api({"documents": [fallback, RIS["documents"][3], RIS["documents"][4]]})
    suggestions = Suggestions(api)
    public = await suggestions.async_suggest("todo.ica", "list-1", "ris", 2)
    assert len(public) == 2
    assert public[0]["secondary"] == "Ris"

    too_long = dict(RIS["documents"][0], name="x" * 257)
    assert await Suggestions(Api({"documents": [too_long]})).async_suggest(
        "todo.ica", "list-1", "ris", 8
    ) == []


async def test_single_flight_coalesces_same_query() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class HeldApi(Api):
        async def search_articles(self, query: str) -> dict:
            self.calls.append(query)
            started.set()
            await release.wait()
            return self.response

    api = HeldApi()
    suggestions = Suggestions(api)
    first = asyncio.create_task(suggestions.async_suggest("todo.ica", "list-1", "ris", 1))
    await started.wait()
    second = asyncio.create_task(suggestions.async_suggest("todo.ica", "list-1", "ris", 1))
    release.set()
    await asyncio.gather(first, second)
    assert api.calls == ["ris"]


async def test_lru_eviction_does_not_shorten_an_issued_selection() -> None:
    clock = Clock()
    api = Api()
    suggestions = Suggestions(api, clock=clock)
    original = await suggestions.async_suggest("todo.ica", "list-1", "q00", 1)
    for index in range(1, QUERY_CACHE_SIZE + 1):
        clock.now += 0.25
        await suggestions.async_suggest("todo.ica", "list-1", f"q{index:02}", 1)
    assert ("q00", 1) not in suggestions._cache
    assert len(suggestions._cache) == QUERY_CACHE_SIZE
    article = await suggestions.async_consume(
        original[0]["selection_key"], "todo.ica", "list-1", original[0]["text"]
    )
    assert article["_id"] == "article-long-rice"


async def test_selection_bound_evicts_oldest_and_expired_entries(monkeypatch) -> None:
    import custom_components.ica_shopping_list.suggestions as module

    monkeypatch.setattr(module, "SELECTION_STORE_SIZE", 2)
    suggestions = Suggestions(Api())
    first = await suggestions.async_suggest("todo.ica", "list-1", "ris", 1)
    second = await suggestions.async_suggest("todo.ica", "list-1", "ris", 1)
    third = await suggestions.async_suggest("todo.ica", "list-1", "ris", 1)
    with pytest.raises(SuggestionError, match="invalid_selection"):
        await suggestions.async_consume(
            first[0]["selection_key"], "todo.ica", "list-1", first[0]["text"]
        )
    assert await suggestions.async_consume(
        second[0]["selection_key"], "todo.ica", "list-1", second[0]["text"]
    )
    assert await suggestions.async_consume(
        third[0]["selection_key"], "todo.ica", "list-1", third[0]["text"]
    )


async def test_rate_limit_and_clear_cancel_pending_state() -> None:
    clock = Clock()
    api = Api()
    suggestions = Suggestions(api, clock=clock)
    await suggestions.async_suggest("todo.ica", "list-1", "aa", 1)
    await suggestions.async_suggest("todo.ica", "list-1", "ab", 1)
    with pytest.raises(SuggestionError, match="rate_limited"):
        await suggestions.async_suggest("todo.ica", "list-1", "ac", 1)

    started = asyncio.Event()

    class HeldApi(Api):
        async def search_articles(self, query: str) -> dict:
            started.set()
            await asyncio.Event().wait()
            return self.response

    held = Suggestions(HeldApi())
    pending = asyncio.create_task(held.async_suggest("todo.ica", "list-1", "ris", 1))
    await started.wait()
    await held.async_clear()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not held._cache and not held._selections and not held._pending
