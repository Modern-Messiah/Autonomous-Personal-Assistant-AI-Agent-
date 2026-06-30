# 2GIS Unknown Nearby Counts Design

## Goal

Prevent temporary or malformed 2GIS responses from being represented as a real zero
and influencing apartment scores for up to seven days.

## Validated finding

`TwoGISClient._count_nearby_api()` currently returns `0` after an
`httpx.HTTPError`. `_count_nearby()` then stores that value in Redis using the normal
seven-day count TTL.

The fallback for a successful response without `result.total` is also incorrect:
the request uses `page_size=1`, so `len(result.items)` is not the total number of
nearby places. It can only be zero or one.

`DeepSeekApartmentScorer` already renders `None` as `unknown`. The information is
lost earlier, inside the 2GIS client.

## Data invariant

Nearby counts have three distinct states:

- a non-negative integer, including a legitimate zero, means 2GIS returned a valid
  total;
- `None` means the count is unknown because the request failed or the response did
  not contain a valid total;
- a missing geocoding result still means that the entire nearby summary is
  unavailable.

An unknown count must never be written to the normal count cache.

## Design

### Nullable count contract

Change `NearbySummary.schools`, `parks`, and `metro` from `int` to `int | None`.
Change `_count_nearby()` and `_count_nearby_api()` to return `int | None`.

Each category remains independent. For example, a summary may contain a known school
count while parks and metro are unknown.

`EnrichNode` already copies these values into nullable `EnrichedApartment` fields, and
the DeepSeek scorer already formats nullable infrastructure fields as `unknown`.
Those components need regression tests rather than a new fallback.

### Response handling

`_count_nearby_api()` returns a count only when `result.total` is a non-negative JSON
integer. JSON booleans are rejected even though Python treats `bool` as a subclass of
`int`.

The method returns `None` when:

- the HTTP request times out or fails;
- the response status is unsuccessful;
- the response body is not valid JSON;
- `result.total` is absent, negative, boolean, or not an integer.

It will not infer a total from `result.items`.

Failures are logged at warning level with the query and failure category, but without
the API key or complete request URL.

### Cache behavior and old poisoned entries

`_count_nearby()` caches every valid integer, including `0`, for the configured normal
TTL. It does not cache `None`.

Existing Redis entries cannot distinguish a legitimate zero from a zero created by an
old HTTP failure. Change the count cache-key namespace from `2gis:cnt:` to a versioned
namespace such as `2gis:cnt:v2:`. This invalidates only nearby-count entries by natural
expiration and forces one fresh lookup after deployment; geocoding cache entries remain
usable.

Malformed cached values are ignored and refreshed. A cached valid integer is returned
normally.

## Error and load behavior

Not caching `None` allows the next search to recover immediately after 2GIS recovers.
During a sustained 2GIS outage this can increase request volume, but a short-lived
failure sentinel would delay recovery and would add another cache state. Retries,
backoff, and a circuit breaker are separate resilience work and are not part of this
fix.

The existing enrichment boundary continues to prevent a 2GIS failure from aborting the
whole apartment search.

## Tests and validation

Add focused tests using `httpx.MockTransport` and a recording fake cache:

- HTTP 5xx and transport errors produce `None` and no count-cache write;
- invalid JSON produces `None` and no count-cache write;
- absent, invalid, boolean, and negative totals produce `None`;
- a valid `total=0` returns zero and is cached for the normal TTL;
- a positive total is cached and reused;
- old unversioned count entries are not read;
- a partial `NearbySummary` propagates nullable fields through enrichment;
- the DeepSeek prompt renders unknown nearby values as `unknown`, not `0`.

Run the full pytest, Ruff, and strict mypy gates.

## Non-goals

- changing geocoding miss caching;
- adding HTTP retries, a circuit breaker, or rate limiting;
- changing the scoring weights or prompt;
- treating a missing address as a 2GIS failure;
- changing the seven-day TTL for valid counts.
