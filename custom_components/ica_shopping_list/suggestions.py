"""Private ICA article search results and short-lived selected-add keys."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .api import IcaAuthRequired, IcaError

_LOGGER = logging.getLogger(__name__)

REQUIRED_ARTICLE_FIELDS = frozenset({
    "_id", "id", "name", "pluralName", "productEan",
    "storeArticleGroupId", "expandedArticleGroupName", "expandedArticleGroupId",
    "articleGroupName", "articleGroupId", "status", "latestChange",
})
OPTIONAL_ARTICLE_FIELDS = frozenset({
    "alternativeSpelling",
    "maxiFormatCategoryId", "maxiFormatCategoryName", "kvantumFormatCategoryId",
    "kvantumFormatCategoryName", "supermarketFormatCategoryId",
    "supermarketFormatCategoryName", "naraFormatCategoryId", "naraFormatCategoryName",
})
ARTICLE_FIELDS = REQUIRED_ARTICLE_FIELDS | OPTIONAL_ARTICLE_FIELDS
_STRING_FIELDS = frozenset({
    "_id", "name", "pluralName", "productEan", "expandedArticleGroupName",
    "articleGroupName", "latestChange",
})
_INTEGER_FIELDS = frozenset({
    "id", "storeArticleGroupId", "expandedArticleGroupId", "articleGroupId", "status",
})
_NULLABLE_STRING_FIELDS = OPTIONAL_ARTICLE_FIELDS - _INTEGER_FIELDS

MAX_QUERY_LENGTH = 80
MAX_RESULTS = 10
MAX_DISPLAY_LENGTH = 256
QUERY_CACHE_SIZE = 128
SELECTION_STORE_SIZE = QUERY_CACHE_SIZE * MAX_RESULTS
SUCCESS_TTL = 300
EMPTY_TTL = 60
SELECTION_TTL = 300


class SuggestionError(Exception):
    """A stable, browser-safe error code without ICA request details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _CachedResult:
    text: str
    primary: str
    secondary: str | None
    article: Mapping[str, str | int | None]


@dataclass(frozen=True)
class _CacheEntry:
    results: tuple[_CachedResult, ...]
    expires: float


@dataclass
class _Selection:
    entity_id: str
    list_id: str
    query: str
    text: str
    article: Mapping[str, str | int | None]
    issued: float
    expires: float
    used: bool = False


def normalize_public_text(value: Any, *, maximum: int = MAX_DISPLAY_LENGTH) -> str:
    """Return safe display text without changing ICA's stored article values."""
    if not isinstance(value, str):
        raise SuggestionError("unsupported_contract")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise SuggestionError("unsupported_contract")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise SuggestionError("unsupported_contract")
    return normalized


def validate_article(document: Any) -> Mapping[str, str | int | None]:
    """Accept ICA's measured flat article fields without reconstructing them."""
    if (
        not isinstance(document, dict)
        or not REQUIRED_ARTICLE_FIELDS.issubset(document)
        or not set(document).issubset(ARTICLE_FIELDS)
    ):
        raise SuggestionError("unsupported_contract")
    for field in _STRING_FIELDS:
        if not isinstance(document[field], str):
            raise SuggestionError("unsupported_contract")
    for field in _INTEGER_FIELDS:
        if isinstance(document[field], bool) or not isinstance(document[field], int):
            raise SuggestionError("unsupported_contract")
    for field in _NULLABLE_STRING_FIELDS.intersection(document):
        if document[field] is not None and not isinstance(document[field], str):
            raise SuggestionError("unsupported_contract")
    # Scalars make a shallow copy a complete immutable, verbatim retention.
    return MappingProxyType(dict(document))


class Suggestions:
    """Bounded per-entry search cache and independent opaque selection store."""

    def __init__(self, api: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._api = api
        self._clock = clock
        self._cache: OrderedDict[tuple[str, int], _CacheEntry] = OrderedDict()
        self._selections: OrderedDict[str, _Selection] = OrderedDict()
        self._pending: dict[tuple[str, int], asyncio.Task[tuple[_CachedResult, ...]]] = {}
        self._rate_tokens = 2.0
        self._rate_updated = clock()
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    async def async_suggest(
        self, entity_id: str, list_id: str, query: str, limit: int
    ) -> list[dict[str, str]]:
        query = self._validate_query(query)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_RESULTS:
            raise SuggestionError("invalid_query")
        key = (query.casefold(), limit)
        now = self._clock()
        results: tuple[_CachedResult, ...] | None = None
        task: asyncio.Task[tuple[_CachedResult, ...]] | None = None
        async with self._lock:
            entry = self._cache.get(key)
            if entry and entry.expires > now:
                self._cache.move_to_end(key)
                results = entry.results
            else:
                if entry:
                    self._cache.pop(key, None)
                task = self._pending.get(key)
                if task is None:
                    self._consume_rate_token(now)
                    task = asyncio.create_task(self._async_fetch(key, query, limit))
                    self._pending[key] = task
        if results is None:
            if task is None:
                raise RuntimeError("Suggestion cache did not provide a fetch task")
            try:
                results = await task
            finally:
                async with self._lock:
                    if self._pending.get(key) is task and task.done():
                        self._pending.pop(key, None)
        return await self._issue_selections(entity_id, list_id, query, results)

    async def async_consume(
        self, key: str, entity_id: str, list_id: str, text: str
    ) -> Mapping[str, str | int | None]:
        try:
            text = normalize_public_text(text)
        except SuggestionError as err:
            raise SuggestionError("invalid_selection") from err
        now = self._clock()
        async with self._lock:
            selection = self._selections.get(key)
            if selection is None:
                raise SuggestionError("invalid_selection")
            if selection.expires <= now:
                self._selections.pop(key, None)
                raise SuggestionError("expired_selection")
            self._discard_expired_selections(now)
            if (selection.used or selection.entity_id != entity_id
                    or selection.list_id != list_id or selection.text != text):
                raise SuggestionError("invalid_selection")
            # Mark before the one allowed POST. A timeout is not safely replayable.
            selection.used = True
            return selection.article

    async def async_clear(self) -> None:
        async with self._lock:
            for task in self._pending.values():
                task.cancel()
            self._pending.clear()
            self._cache.clear()
            self._selections.clear()
            self._cooldown_until = 0.0
            self._rate_tokens = 2.0
            self._rate_updated = self._clock()

    def _validate_query(self, query: Any) -> str:
        try:
            normalized = normalize_public_text(query, maximum=MAX_QUERY_LENGTH)
        except SuggestionError as err:
            raise SuggestionError("invalid_query") from err
        if len(normalized) < 2:
            raise SuggestionError("invalid_query")
        return normalized

    def _consume_rate_token(self, now: float) -> None:
        if now < self._cooldown_until:
            raise SuggestionError("rate_limited")
        self._rate_tokens = min(2.0, self._rate_tokens + (now - self._rate_updated) * 4)
        self._rate_updated = now
        if self._rate_tokens < 1:
            raise SuggestionError("rate_limited")
        self._rate_tokens -= 1

    async def _async_fetch(
        self, key: tuple[str, int], query: str, limit: int
    ) -> tuple[_CachedResult, ...]:
        try:
            payload = await self._api.search_articles(query)
            documents = payload.get("documents") if isinstance(payload, dict) else None
            if not isinstance(documents, list):
                raise SuggestionError("unsupported_contract")
            valid_results: list[_CachedResult] = []
            skipped = 0
            for document in documents:
                if len(valid_results) == limit:
                    break
                try:
                    valid_results.append(self._public_result(document))
                except SuggestionError:
                    # ICA can mix older or malformed records into an otherwise
                    # usable response. Never make one result hide its peers.
                    skipped += 1
            if skipped:
                _LOGGER.debug("Skipped %d invalid ICA article search documents", skipped)
            results = tuple(valid_results)
        except IcaAuthRequired as err:
            raise SuggestionError("auth_required") from err
        except IcaError as err:
            async with self._lock:
                self._cooldown_until = self._clock() + 1
            raise SuggestionError("unavailable") from err
        now = self._clock()
        async with self._lock:
            self._cache[key] = _CacheEntry(results, now + (SUCCESS_TTL if results else EMPTY_TTL))
            self._cache.move_to_end(key)
            while len(self._cache) > QUERY_CACHE_SIZE:
                self._cache.popitem(last=False)
        return results

    def _public_result(self, document: Any) -> _CachedResult:
        article = validate_article(document)
        text = normalize_public_text(article["name"])
        secondary_source = article["articleGroupName"] or article["expandedArticleGroupName"]
        secondary = normalize_public_text(secondary_source) if secondary_source else None
        return _CachedResult(
            text=text,
            primary=text[:1].upper() + text[1:],
            secondary=secondary,
            article=article,
        )

    async def _issue_selections(
        self, entity_id: str, list_id: str, query: str, results: tuple[_CachedResult, ...]
    ) -> list[dict[str, str]]:
        now = self._clock()
        async with self._lock:
            self._discard_expired_selections(now)
            response: list[dict[str, str]] = []
            for result in results:
                key = secrets.token_urlsafe(32)
                self._selections[key] = _Selection(
                    entity_id=entity_id, list_id=list_id, query=query, text=result.text,
                    article=result.article, issued=now, expires=now + SELECTION_TTL,
                )
                public = {"selection_key": key, "text": result.text, "primary": result.primary}
                if result.secondary:
                    public["secondary"] = result.secondary
                response.append(public)
            while len(self._selections) > SELECTION_STORE_SIZE:
                self._selections.popitem(last=False)
            return response

    def _discard_expired_selections(self, now: float) -> None:
        for key, selection in list(self._selections.items()):
            if selection.expires <= now:
                self._selections.pop(key, None)
