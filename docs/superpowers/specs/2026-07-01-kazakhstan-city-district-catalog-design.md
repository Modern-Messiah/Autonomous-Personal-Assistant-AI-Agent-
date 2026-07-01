# Kazakhstan City and District Catalog Design

## Goal

Allow a Telegram user to name any official city of Kazakhstan and, where
applicable, one of that city's official administrative districts in a normal
`/search` request. A city-only request searches the whole city; a city plus
district request returns only apartments whose district can be confirmed.

## Scope

The catalog covers all 90 cities reported by Kazakhstan's Bureau of National
Statistics as of 1 January 2026. The checked-in source snapshot is the official
KATO classifier `НК РК 11-2025`, updated 18 June 2026.

For those cities, the catalog also covers every official `район в городе`
present in that KATO snapshot. Villages, settlements, rural districts,
neighbourhoods and microdistricts are outside this feature.

Krisha location URLs and the district labels found in its apartment cards are
verified separately because an official KATO name is not necessarily the string
or URL slug used by Krisha.

Authoritative references:

- Bureau of National Statistics,
  `https://stat.gov.kz/ru/classifiers/statistical/21/`;
- administrative-territorial units as of 1 January 2026,
  `https://stat.gov.kz/en/industries/social-statistics/demography/publications/476649/`;
- Krisha apartment search,
  `https://krisha.kz/prodazha/kvartiry/`.

## Current Problems

`agent/nodes/intent_node.py` contains a partial city alias table rather than a
complete location registry. `agent/tools/districts.py` covers only Almaty,
Astana, Shymkent and Karaganda.

Unknown cities can currently become the default city, Almaty. Unknown or
unmapped districts disable district filtering. The latter produces a dangerous
false success: a user can request one district and receive results from the
whole city.

District filtering is also soft. A listing whose preview does not expose a
recognisable district is retained. This is useful for recall, but it does not
satisfy the agreed strict district-filter contract.

## Chosen Approach

Use a versioned, checked-in catalog as the source of truth. LLM extraction and
regex extraction both normalize their outputs through this catalog. Do not let
the LLM invent canonical locations, and do not depend on Krisha markup at
runtime to discover the set of supported cities.

The alternatives were rejected for these reasons:

- LLM-only location recognition can hallucinate a city/district relationship
  and cannot provide deterministic validation.
- Runtime discovery from Krisha would couple every search to a brittle external
  taxonomy and increase anti-bot exposure.

## Data Model

Create a data file dedicated to Kazakhstan locations. Each city entry contains:

- stable KATO code;
- canonical application name;
- official Russian and Kazakh names;
- accepted historical, transliterated and inflected aliases;
- verified Krisha city slug;
- zero or more official city-district entries.

Each district entry contains:

- stable KATO code;
- canonical application name;
- official Russian and Kazakh names;
- accepted aliases and grammatical stems;
- district labels observed on Krisha where they differ from the official name.

Application code exposes a small `LocationCatalog` API:

- `canonical_city(text)`;
- `canonical_district(text, city)`;
- `city_slug(city)`;
- `cities_for_district(text)`;
- `districts_for_city(city)`.

Matching normalizes Unicode, letter case, whitespace and dash variants. Aliases
are matched longest-first on token boundaries rather than as arbitrary
substrings. This prevents short city names such as `Oral` from being detected
inside unrelated words.

The catalog loader validates duplicate KATO codes, canonical names, slugs and
ambiguous aliases at import/test time. An ambiguous district name is valid only
when it is resolved within a known city.

## Intent Parsing and Validation

The existing free-form `/search` interface remains unchanged. There is no
button-based region/city selector.

The LLM returns location text extracted from the request. Deterministic catalog
normalization converts that text into canonical application values. The regex
fallback uses the same catalog and therefore supports the same cities and
districts.

Location states are kept distinct:

- no city supplied;
- valid city supplied;
- unrecognised city supplied;
- no district supplied;
- valid district supplied;
- unrecognised or city-mismatched district supplied.

A missing city may continue to use the configured default, with the existing
user-visible default notice. An explicitly supplied but unrecognised city must
never silently become Almaty.

If a district is supplied without a city and maps to exactly one city, the city
may be inferred. If the district name exists in multiple cities, the bot asks
the user to include the city and does not search.

If the district does not belong to the selected city, or cannot be recognised,
the bot reports the location error and does not run a broad city search. The
error includes the recognised city and its supported districts when that list
is non-empty.

Examples:

- `/search 2-комнатная в Конаеве до 30 млн` resolves the city and searches all
  of Konaev.
- `/search квартира в Астане, район Есиль` searches only Yesil district.
- `/search квартира в Астане, Бостандыкский район` is rejected as a
  city/district mismatch.
- `/search квартира в Бостандыкском районе` infers Almaty because the district
  is unique in the catalog.

## Krisha Search and Strict District Filtering

The parser builds a city URL from the catalog's verified Krisha slug rather than
lowercasing an arbitrary city name.

With no district criterion, district logic is skipped and apartments from the
whole selected city remain eligible.

With a district criterion, filtering is strict:

1. A preview with a known non-matching district is discarded before opening its
   detail page.
2. A preview with a known matching district is fetched normally.
3. A preview whose district is absent or unknown is provisionally fetched so
   the detail page can supply the missing address/district.
4. After detail parsing, the apartment is returned only when its district or
   address resolves to one of the requested districts.

An apartment that remains district-unknown after detail parsing is excluded
from a district-specific result. Search continues through available candidates
until it reaches the normal result limit or exhausts the configured pages.

This preserves strict correctness without prematurely losing listings whose
district appears only on the detail page.

## User-Facing Errors

Location validation failures are expected input errors, not parser crashes. The
bot responds in Russian with a short correction:

- unknown city: say it was not recognised and ask for an official city name;
- unknown district: show the selected city and valid districts, if any;
- mismatched district: say that the district does not belong to the selected
  city;
- ambiguous district without a city: ask the user to add the city.

No Krisha browser session, scoring request or database write starts after a
location validation failure.

## Testing

Catalog integrity tests verify:

- exactly 90 unique official cities from the pinned KATO snapshot;
- a non-empty unique Krisha slug for every city;
- all district parent references and KATO codes;
- no illegal alias ambiguity;
- cities without administrative districts return an empty district list.

Intent tests cover Russian, Kazakh, English/transliterated, historical and
inflected city names; unique and ambiguous district inference; invalid
city/district pairs; LLM output normalization; and regex fallback.

Parser tests cover city slug generation, city-only search, preview-level
district rejection, detail-level district confirmation, and exclusion of a
listing whose district remains unknown.

Bot-service tests verify that invalid locations produce a correction and do not
invoke the search graph.

A separate catalog audit checks the 90 configured Krisha URLs without becoming
a normal test-suite dependency. It runs deliberately with rate limiting and
records redirects, missing pages and anti-bot responses. The existing small
production canary remains the continuous markup check; CI must not crawl 90
Krisha locations on every run.

## Data Maintenance

The data file records the KATO classifier version and update date. Updating the
catalog is an explicit maintenance operation:

1. download the newer official KATO release;
2. regenerate/compare official cities and city districts;
3. review additions, removals and renames;
4. verify only changed Krisha slugs/labels;
5. run catalog, intent, parser and bot tests.

The application does not silently fetch and replace geography data at runtime.

## Acceptance Criteria

- Every one of the 90 official cities in the pinned 2026 KATO snapshot resolves
  from its official Russian and Kazakh name and has a verified Krisha slug.
- Every official city district in that snapshot resolves only within its parent
  city.
- City-only requests search the entire city.
- Valid city-plus-district requests return only district-confirmed apartments.
- Unknown and mismatched locations stop the search with a useful message.
- LLM failure or absence preserves equivalent location support through regex
  fallback.
- Existing searches for Almaty, Astana, Shymkent and Karaganda remain
  compatible.
