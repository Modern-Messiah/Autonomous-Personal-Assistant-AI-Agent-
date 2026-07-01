# Kazakhstan Location Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make free-form `/search` understand all 90 official Kazakhstan cities and every official city district in the pinned 18 June 2026 KATO snapshot, with strict district filtering and explicit location errors.

**Architecture:** Add one checked-in, versioned location catalog shared by LLM normalization, regex fallback, Krisha URL construction, district filtering and preference scoring. Keep user input extraction separate from deterministic location validation. Treat district-unknown previews as provisional, then require a confirmed district after parsing the detail page.

**Tech Stack:** Python 3.12, Pydantic 2, aiogram 3, Playwright, pytest, JSON, standard-library XLSX validation (`zipfile` + `xml.etree.ElementTree`).

---

## File Structure

- Create `agent/locations/__init__.py`: public catalog and resolver exports.
- Create `agent/locations/catalog.py`: immutable catalog models, normalization and lookups.
- Create `agent/locations/resolver.py`: request-level city/district resolution and input errors.
- Create `agent/locations/kz_locations.json`: pinned 2026 KATO city/district data, aliases and Krisha slugs.
- Create `scripts/validate_kz_locations.py`: compare the checked-in catalog with an official KATO XLSX and optionally audit Krisha URLs.
- Create `tests/test_location_catalog.py`: catalog integrity and lookup tests.
- Create `tests/test_location_resolver.py`: request-location behavior and errors.
- Modify `agent/tools/districts.py`: compatibility wrappers around the shared catalog.
- Modify `agent/nodes/intent_node.py`: remove local city/district tables and use the resolver for LLM and regex paths.
- Modify `agent/tools/llm_intent_parser.py`: ask for extracted location text, not invented canonical values.
- Modify `agent/tools/krisha_parser.py`: use verified city slugs and enforce district match after detail parsing.
- Modify `bot/service.py`: propagate expected location-input failures without persisting/running search.
- Modify `bot/router.py`: display location-input failures for `/search` and both `/refine` flows.
- Modify `tests/test_districts.py`, `tests/test_intent_node.py`, `tests/test_llm_intent_parser.py`, `tests/test_krisha_parser.py`, `tests/test_bot_service.py`, and `tests/test_bot_router.py`.
- Modify `README.md`: document city/district behavior and the catalog refresh/audit command.

### Task 1: Add the versioned catalog and deterministic lookup API

**Files:**
- Create: `agent/locations/__init__.py`
- Create: `agent/locations/catalog.py`
- Create: `agent/locations/kz_locations.json`
- Create: `tests/test_location_catalog.py`

- [ ] **Step 1: Write catalog integrity and lookup tests**

Add tests that load the production data rather than a mock:

```python
from agent.locations import LOCATIONS


def test_catalog_contains_pinned_90_official_cities() -> None:
    assert LOCATIONS.metadata.kato_version == "НК РК 11-2025"
    assert LOCATIONS.metadata.updated_at == "2026-06-18"
    assert len(LOCATIONS.cities) == 90
    assert len({city.kato_code for city in LOCATIONS.cities}) == 90
    assert sum(city.krisha_slug is not None for city in LOCATIONS.cities) == 89


def test_catalog_resolves_official_languages_and_historical_aliases() -> None:
    assert LOCATIONS.canonical_city("Конаев") == "Konaev"
    assert LOCATIONS.canonical_city("Қонаев") == "Konaev"
    assert LOCATIONS.canonical_city("Капчагай") == "Konaev"
    assert LOCATIONS.canonical_city("Усть-Каменогорск") == "Ust-Kamenogorsk"
    assert LOCATIONS.canonical_city("Өскемен") == "Ust-Kamenogorsk"


def test_district_lookup_is_scoped_to_city() -> None:
    assert LOCATIONS.canonical_district("Алматинский район", "Astana") == "Almaty"
    assert LOCATIONS.canonical_district("Алмалинский район", "Almaty") == "Almaly"
    assert LOCATIONS.canonical_district("Бостандыкский", "Astana") is None


def test_city_without_city_districts_has_empty_district_list() -> None:
    assert LOCATIONS.districts_for_city("Konaev") == ()
```

Also parameterize every catalog city to verify that its official Russian and
Kazakh names resolve to the same canonical value and every district resolves
only under its parent city.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run pytest tests/test_location_catalog.py -q
```

Expected: collection fails because `agent.locations` does not exist.

- [ ] **Step 3: Add catalog models and normalization**

Implement frozen dataclasses:

```python
@dataclass(frozen=True, slots=True)
class CatalogMetadata:
    kato_version: str
    updated_at: str
    source_url: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class District:
    kato_code: str
    canonical: str
    name_ru: str
    name_kk: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class City:
    kato_code: str
    canonical: str
    name_ru: str
    name_kk: str
    aliases: tuple[str, ...]
    krisha_slug: str | None
    districts: tuple[District, ...]
```

`LocationCatalog.from_path()` must validate:

- exactly one canonical name and KATO code per city, plus 89 unique Krisha
  slugs and one explicit unavailable city (Zhem);
- unique district KATO codes;
- non-empty official names and aliases;
- no alias collision between two cities after normalization;
- district alias collisions are allowed only across different parent cities.

Normalize with Unicode `NFKC`, `casefold()`, `ё -> е`, whitespace collapse and
dash normalization. Compile aliases longest-first and require token boundaries;
do not use arbitrary `alias in text` matching.

Expose:

```python
def canonical_city(self, text: str | None) -> str | None: ...
def canonical_district(self, text: str | None, city: str | None) -> str | None: ...
def city_slug(self, city: str) -> str | None: ...
def cities_for_district(self, text: str) -> tuple[str, ...]: ...
def districts_for_city(self, city: str) -> tuple[District, ...]: ...
def find_city_in_text(self, text: str) -> str | None: ...
def find_districts_in_text(self, text: str, city: str | None) -> tuple[str, ...]: ...
```

- [ ] **Step 4: Add the pinned runtime data**

Populate `agent/locations/kz_locations.json` from:

```text
https://stat.gov.kz/upload/iblock/ecf/td3x7i3ylpv00rgmlwitae1nbngfriqc/КАТО_18.06.2026.xlsx
SHA-256: 3f8f7d84099e8a5a584933d76f9f91bf9c3f3a3400d2f09d62590ac8eb51571c
```

The JSON root is:

```json
{
  "metadata": {
    "kato_version": "НК РК 11-2025",
    "updated_at": "2026-06-18",
    "source_url": "https://stat.gov.kz/ru/classifiers/statistical/21/",
    "source_sha256": "3f8f7d84099e8a5a584933d76f9f91bf9c3f3a3400d2f09d62590ac8eb51571c"
  },
  "cities": []
}
```

Fill `cities` with the 90 `г.`/`қ.` city records from KATO. Attach KATO
`район в городе` records only to their actual city parent. Each city receives a
verified Krisha path slug and Russian, Kazakh, Latin, inflected and historical
aliases where applicable. Zhem has `krisha_slug: null` because live Krisha
region selection and URL verification show no corresponding location. Preserve
the existing canonical city/district values so stored criteria remain readable.

- [ ] **Step 5: Run catalog tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_location_catalog.py -q
```

Expected: all catalog tests pass and report 90 cities.

- [ ] **Step 6: Commit**

```bash
git add agent/locations tests/test_location_catalog.py
git commit -m "feat(locations): add official Kazakhstan catalog"
```

### Task 2: Replace the partial district module without breaking callers

**Files:**
- Modify: `agent/tools/districts.py`
- Modify: `tests/test_districts.py`
- Modify: `bot/preferences.py`

- [ ] **Step 1: Write compatibility and preference tests**

Keep the existing public helpers but require full-catalog behavior:

```python
def test_legacy_canonical_district_uses_full_catalog() -> None:
    assert canonical_district("Есильский р-н", "Astana") == "Yesil"
    assert canonical_district("район из another city", "Konaev") is None


def test_preference_profile_uses_city_scoped_catalog() -> None:
    profile = build_preference_profile(
        [build_enriched(city="Astana", district="Алматинский р-н")],
        [],
    )
    assert profile.liked_districts == {"Almaty"}
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
uv run pytest tests/test_districts.py tests/test_preferences.py -q
```

Expected: the new full-catalog case fails against the four-city table.

- [ ] **Step 3: Convert `districts.py` to compatibility wrappers**

Replace `CITY_DISTRICTS` and substring matching with:

```python
from agent.locations import LOCATIONS


def canonical_district(text: str | None, city: str | None) -> str | None:
    return LOCATIONS.canonical_district(text, city)


def flat_district_aliases() -> dict[str, str]:
    return LOCATIONS.unambiguous_district_aliases()
```

Keep `bot/preferences.py` calling `canonical_district`; no separate district
table may remain.

- [ ] **Step 4: Run and verify GREEN**

```bash
uv run pytest tests/test_districts.py tests/test_preferences.py -q
```

- [ ] **Step 5: Commit**

```bash
git add agent/tools/districts.py bot/preferences.py tests/test_districts.py tests/test_preferences.py
git commit -m "refactor(locations): share district catalog"
```

### Task 3: Resolve and validate request locations for LLM and regex paths

**Files:**
- Create: `agent/locations/resolver.py`
- Create: `tests/test_location_resolver.py`
- Modify: `agent/locations/__init__.py`
- Modify: `agent/nodes/intent_node.py`
- Modify: `tests/test_intent_node.py`

- [ ] **Step 1: Write resolver behavior tests**

Cover the agreed contract:

```python
def test_city_only_search_has_no_district_filter() -> None:
    result = resolve_locations(message="квартира в Конаеве", default_city="Almaty")
    assert result.city == "Konaev"
    assert result.districts is None
    assert result.defaulted_city is False


def test_city_and_valid_district_are_canonicalized() -> None:
    result = resolve_locations(
        message="квартира в Астане, Есильский район",
        default_city="Almaty",
    )
    assert result.city == "Astana"
    assert result.districts == ("Yesil",)


def test_unique_district_can_infer_city() -> None:
    result = resolve_locations(
        message="квартира в Бостандыкском районе",
        default_city="Almaty",
    )
    assert result.city == "Almaty"
    assert result.districts == ("Bostandyk",)
    assert result.defaulted_city is False


def test_mismatched_district_is_rejected() -> None:
    with pytest.raises(LocationInputError, match="не относится"):
        resolve_locations(
            message="квартира в Астане, Бостандыкский район",
            default_city="Almaty",
        )


def test_ambiguous_district_without_city_is_rejected() -> None:
    with pytest.raises(LocationInputError, match="укажи город"):
        resolve_locations(message="квартира в районе Алматы", default_city="Almaty")


def test_invalid_llm_location_is_not_silently_defaulted() -> None:
    with pytest.raises(LocationInputError, match="Город"):
        resolve_locations(
            message="квартира",
            llm_city="Несуществующий",
            default_city="Almaty",
        )
```

Also test a message with no location defaults to Almaty and marks
`defaulted_city=True`.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_location_resolver.py -q
```

Expected: import failure for `agent.locations.resolver`.

- [ ] **Step 3: Implement the resolver and typed input error**

Create:

```python
@dataclass(frozen=True, slots=True)
class ResolvedLocations:
    city: str
    districts: tuple[str, ...] | None
    defaulted_city: bool


class LocationInputError(ValueError):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message
```

Resolution order:

1. normalize a non-null LLM city/district through the catalog;
2. independently scan the original message with catalog aliases;
3. prefer a valid explicit city over the default;
4. infer the city only when a district maps to exactly one city;
5. reject an invalid LLM-extracted location, an ambiguous district, or a
   district not belonging to the selected city;
6. use the default only when no city/district candidate was supplied.

When a selected city has districts, unknown-district errors append their
official Russian names. Cities with no administrative districts receive the
message that district filtering is unavailable for that city.

- [ ] **Step 4: Integrate resolver into `IntentNode`**

Remove `CITY_ALIASES`, `DISTRICT_ALIASES`, `_find_city()` and
`_parse_districts()`. Keep non-location regex parsing unchanged.

Change `ParsedIntent` to retain the resolver metadata:

```python
@dataclass(slots=True, frozen=True)
class ParsedIntent:
    criteria: SearchCriteria
    defaulted_city: bool = False
```

Before constructing `SearchCriteria`, call `resolve_locations()` with the raw
message and any city/district strings returned by the LLM. For regex fallback,
pass only the raw message. During `/refine`, pass existing city/district values
as the base and validate any newly supplied location.

- [ ] **Step 5: Extend intent tests and verify GREEN**

Add parameterized regex-fallback tests for at least:

```text
двухкомнатная в Қонаеве до 30 млн
квартира в Риддере
квартира в Астане, район Сарайшык
квартира в Бостандыкском районе
```

Add LLM-stub tests proving valid raw Russian/Kazakh output is canonicalized and
invalid city/district combinations raise `LocationInputError`.

Run:

```bash
uv run pytest tests/test_location_resolver.py tests/test_intent_node.py -q
```

- [ ] **Step 6: Commit**

```bash
git add agent/locations agent/nodes/intent_node.py tests/test_location_resolver.py tests/test_intent_node.py
git commit -m "feat(intent): validate Kazakhstan locations"
```

### Task 4: Stop asking the LLM to invent canonical geography

**Files:**
- Modify: `agent/tools/llm_intent_parser.py`
- Modify: `tests/test_llm_intent_parser.py`

- [ ] **Step 1: Write a prompt-contract test**

Inspect the posted JSON request and require:

```python
prompt = payload["messages"][1]["content"]
assert "Return city and district names as written by the user" in prompt
assert "suitable for Krisha paths" not in prompt
assert payload["response_format"] == {"type": "json_object"}
```

The mocked response should return:

```python
{
    "city": "Қарағанды",
    "districts": ["Қазыбек би ауданы"],
    "rooms": [2],
    "max_price_kzt": 30_000_000
}
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_llm_intent_parser.py -q
```

Expected: the old prompt still asks for English Krisha-path values.

- [ ] **Step 3: Update the prompt**

Replace canonicalization instructions with:

```text
Return city and district names as written by the user, with surrounding
punctuation removed. Do not translate, transliterate, guess, or replace a
location. Deterministic application code validates locations after extraction.
```

Keep the OpenAI-compatible endpoint, JSON response format, retry and regex
fallback behavior unchanged.

- [ ] **Step 4: Run and verify GREEN**

```bash
uv run pytest tests/test_llm_intent_parser.py tests/test_intent_node.py -q
```

- [ ] **Step 5: Commit**

```bash
git add agent/tools/llm_intent_parser.py tests/test_llm_intent_parser.py
git commit -m "fix(intent): keep LLM location output untrusted"
```

### Task 5: Return location errors before persistence or search

**Files:**
- Modify: `bot/service.py`
- Modify: `bot/router.py`
- Modify: `tests/test_bot_service.py`
- Modify: `tests/test_bot_router.py`

- [ ] **Step 1: Write service and router tests**

Service test:

```python
@pytest.mark.asyncio
async def test_invalid_location_does_not_persist_or_run_search() -> None:
    runner = AsyncMock()
    service = SearchBotService(
        session_factory=session_factory,
        intent_node=StubIntentNode(
            error=LocationInputError(
                "Бостандыкский район не относится к городу Астана."
            )
        ),
        search_runner=runner,
    )

    with pytest.raises(LocationInputError):
        await service.run_search(
            telegram_user_id=42,
            username="tester",
            query="Астана, Бостандыкский район",
        )

    runner.assert_not_awaited()
```

Router tests must assert `/search`, `/refine <text>` and FSM refinement answer
with `exc.user_message`, clear/retain FSM state consistently, and do not send
the generic execution-failure message.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_bot_service.py tests/test_bot_router.py -q
```

- [ ] **Step 3: Catch only the expected location error in router flows**

Import `LocationInputError` and add it before `SearchExecutionError`:

```python
except LocationInputError as exc:
    await message.answer(exc.user_message)
    return
```

Do not convert it to `SearchExecutionError`; it is valid user input feedback,
not an upstream outage. `SearchBotService.run_search()` already parses before
opening a database session, so no persistence code should move ahead of parsing.

- [ ] **Step 4: Run and verify GREEN**

```bash
uv run pytest tests/test_bot_service.py tests/test_bot_router.py -q
```

- [ ] **Step 5: Commit**

```bash
git add bot/service.py bot/router.py tests/test_bot_service.py tests/test_bot_router.py
git commit -m "feat(bot): explain invalid search locations"
```

### Task 6: Use verified slugs and enforce strict district results

**Files:**
- Modify: `agent/tools/krisha_parser.py`
- Modify: `tests/test_krisha_parser.py`

- [ ] **Step 1: Write URL and strict-filter tests**

Add a URL test:

```python
def test_listing_url_uses_catalog_krisha_slug() -> None:
    criteria = build_criteria(city="Ust-Kamenogorsk")
    url = parser._build_listing_urls(criteria)[0]
    assert "/kvartiry/ust-kamenogorsk/" in url
```

Add three async search tests using fake pages:

1. known non-matching preview district never opens a detail page;
2. unknown preview district opens a detail page and is returned when the detail
   address resolves to the requested district;
3. unknown preview district opens a detail page but is excluded and its Redis
   claim is released when detail data still cannot confirm the district.

The third test must assert search continues and can return a later confirmed
listing up to `max_results`.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_krisha_parser.py -q
```

Expected: URL construction bypasses the catalog and detail results are appended
without strict district confirmation.

- [ ] **Step 3: Split preview and detail district decisions**

Use an enum:

```python
class DistrictMatch(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"
```

Implement:

```python
def _preview_district_match(
    preview: ListingPreview,
    criteria: SearchCriteria,
) -> DistrictMatch: ...


def _apartment_matches_district(
    apartment: Apartment,
    criteria: SearchCriteria,
) -> bool: ...
```

`_matches_criteria()` continues treating `UNKNOWN` as provisional. After
`parse_detail_page()`, append only when `_apartment_matches_district()` is true.
When a fetched apartment fails the strict district check, call
`_release_preview()` so a later city-wide request is not poisoned by an
invisible dedup claim.

Replace:

```python
city_slug = quote(criteria.city.strip().lower().replace(" ", "-"))
```

with:

```python
city_slug = quote(LOCATIONS.city_slug(criteria.city))
```

Search must continue through the remaining previews until it collects
`max_results` confirmed apartments or exhausts them.

- [ ] **Step 4: Run and verify GREEN**

```bash
uv run pytest tests/test_krisha_parser.py tests/test_search_graph.py -q
```

- [ ] **Step 5: Commit**

```bash
git add agent/tools/krisha_parser.py tests/test_krisha_parser.py
git commit -m "fix(parser): enforce city and district catalog"
```

### Task 7: Add reproducible KATO and Krisha catalog audits

**Files:**
- Create: `scripts/validate_kz_locations.py`
- Create: `tests/test_validate_kz_locations.py`
- Modify: `README.md`

- [ ] **Step 1: Write validator tests against a tiny XLSX fixture**

Build a minimal XLSX zip in the test with `sharedStrings.xml` and
`worksheets/sheet1.xml`. Verify:

```python
def test_validator_detects_missing_official_city(tmp_path: Path) -> None:
    xlsx = build_kato_fixture(tmp_path, cities=[("101010000", "г.Семей", "Семей қ.")])
    problems = compare_catalog_to_kato(LOCATIONS, xlsx)
    assert any("missing official city" in problem for problem in problems)


def test_validator_accepts_matching_fixture(tmp_path: Path) -> None:
    catalog = build_catalog_fixture(city_code="101010000", district_code=None)
    xlsx = build_kato_fixture(tmp_path, cities=[("101010000", "г.Семей", "Семей қ.")])
    assert compare_catalog_to_kato(catalog, xlsx) == []
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_validate_kz_locations.py -q
```

- [ ] **Step 3: Implement the standard-library XLSX comparison**

The CLI accepts:

```text
uv run python scripts/validate_kz_locations.py \
  --kato-xlsx /tmp/KATO_18.06.2026.xlsx
```

It verifies the pinned SHA-256, extracts `kaz_name`, `rus_name` and KATO codes,
compares official city/city-district records with the JSON catalog, prints every
addition/removal/rename, and exits non-zero on differences.

Add optional:

```text
--audit-krisha --delay-seconds 2
```

This sends one rate-limited request per configured slug, accepts normal Krisha
redirects, reports 404/429/captcha separately, and never runs in the ordinary
unit-test or CI path.

- [ ] **Step 4: Document behavior and maintenance**

README must state:

- all 90 official cities are recognised; 89 are searchable and Zhem receives an
  explicit Krisha limitation;
- districts are optional and city-scoped;
- district requests exclude unconfirmed listings;
- villages, settlements and microdistricts are excluded;
- exact commands for downloading the pinned XLSX and running both audits.

- [ ] **Step 5: Run validator tests and the pinned local comparison**

```bash
uv run pytest tests/test_validate_kz_locations.py -q
uv run python scripts/validate_kz_locations.py --kato-xlsx /tmp/KATO_18.06.2026.xlsx
```

Expected: tests pass and the real comparison prints `90 cities` with no
catalog/KATO differences.

- [ ] **Step 6: Commit**

```bash
git add scripts/validate_kz_locations.py tests/test_validate_kz_locations.py README.md
git commit -m "chore(locations): audit KATO and Krisha mappings"
```

### Task 8: Full verification and live smoke checks

**Files:**
- Modify only if verification exposes a defect.

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest \
  tests/test_location_catalog.py \
  tests/test_location_resolver.py \
  tests/test_districts.py \
  tests/test_intent_node.py \
  tests/test_llm_intent_parser.py \
  tests/test_krisha_parser.py \
  tests/test_bot_service.py \
  tests/test_bot_router.py \
  tests/test_validate_kz_locations.py -q
```

- [ ] **Step 2: Run all static checks and tests**

```bash
uv run ruff check .
uv run mypy .
uv run pytest -q
```

Expected: all commands exit zero.

- [ ] **Step 3: Build the production image**

```bash
docker build -f Containerfile -t krisha-agent:locations .
docker run --rm krisha-agent:locations \
  python -c "from agent.locations import LOCATIONS; assert len(LOCATIONS.cities) == 90"
```

- [ ] **Step 4: Run low-volume live smoke searches**

With configured credentials and services, verify:

```text
/search 2-комнатная в Қонаеве до 30 млн
/search квартира в Астане, Есильский район
/search квартира в Астане, Бостандыкский район
/search квартира в Риддере
```

Expected: city-only searches use their city URL, the valid district request
contains only district-confirmed results, and the mismatched request returns an
input error without starting Krisha search.

- [ ] **Step 5: Review the final diff**

```bash
git status --short
git diff --check
git log --oneline -8
```

Confirm no secrets, downloaded XLSX files or unrelated user changes are
included.
