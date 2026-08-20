# ICA autocomplete contract v1

**Recorded 2026-08-20.** The protected HAR was unpacked only under `/var/tmp`.
It is not present in this repository. Fixtures contain deterministic placeholder
IDs and EANs; they contain no account, list, store, cookie, token, timing,
request metadata, or other identifiers.

## Measured request and response shape

- Search uses `GET /sverige/digx/shoppinglistarticlesearch/v1/search?query=`.
- The `ri` observation had no documents; `ris` had ten documents and
  `stats.totalHits: 143`.
- Search responses have `documents`, `stats`, `facets`, and `spellSuggestions`.
- The selected `långkornigt ris` document was copied verbatim into the row POST
  alongside `isStriked: false`, `quantity: {}`, and the selected `text`.

The HAR capture omitted `Authorization` on both search and a known-authenticated
row POST. Its OPTIONS preflight requested `authorization`, so omission is not
evidence of anonymous search. The integration therefore uses its existing bearer
token path for search. **This one-time live authorization verification is a
pre-tag release gate for the unreleased `0.2.0` branch.** This development
environment has no approved ICA session, and CI makes no live ICA calls.

## Article and browser boundary

The server requires these 12 measured flat scalar keys: `_id`, `id`, `name`,
`pluralName`, `productEan`,
`storeArticleGroupId`, `expandedArticleGroupName`, `expandedArticleGroupId`,
`articleGroupName`, `articleGroupId`, `status`, and `latestChange`. It accepts
these nine optional keys when present: `alternativeSpelling`,
`maxiFormatCategoryId`, `maxiFormatCategoryName`, `kvantumFormatCategoryId`,
`kvantumFormatCategoryName`, `supermarketFormatCategoryId`,
`supermarketFormatCategoryName`, `naraFormatCategoryId`, and
`naraFormatCategoryName`. Unknown keys and wrong scalar types are rejected. It
retains each valid document as copied immutable data and sends it as `article`;
it never derives article data from browser input. A malformed document is skipped
within an otherwise valid result list, while a malformed top-level `documents`
response is an `unsupported_contract` error.

Contract v1 returns `add_strategy: "ica_add_suggestion"`. Each public suggestion
has only an opaque five-minute `selection_key`, normalized `text`, `primary`, and
optional group-name `secondary`. Same-name ICA documents remain separate results.
`text` remains ICA's add text; `primary` applies ICA-style first-character
capitalization for display.
The card starts requests at three trimmed characters based on `ri`/`ris`; the
integration accepts two through 80 characters and limits one through ten.

## Verification boundary

The test suite uses mocked ICA HTTP and isolated Home Assistant WebSocket
handler doubles rather than `pytest-homeassistant-custom-component`, which this
repository intentionally does not load. The doubles implement the production
handler interactions (permissions, registry lookup, entity state, config-entry
lookup, and WebSocket result/error methods) and verify permission-before-registry
ordering, entry/list isolation, and no upstream call on invalid or denied input.
Full WebSocket-client harness coverage remains a release-environment follow-up.
