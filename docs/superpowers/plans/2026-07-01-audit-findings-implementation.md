# Audit Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved production-hardening, persistence, Telegram UX, test, and maintainability findings without changing the apartment-search product contract.

**Architecture:** Apply the changes in four independently verifiable workstreams: runtime/container hardening, PostgreSQL and 2GIS correctness, Telegram state/UX presentation, and pure Krisha HTML parsing. PostgreSQL remains the source of durable business state, Redis stores ephemeral FSM/parser state, and external APIs degrade to explicit unknown values instead of fabricated data.

**Tech Stack:** Python 3.12, aiogram 3, SQLAlchemy async/PostgreSQL 16, Alembic, Redis, ARQ, Playwright, uv, Podman/Docker Compose, GitHub Actions, pytest/pytest-cov.

---

### Task 1: Runtime observability

**Files:**
- Create: `config/observability.py`
- Modify: `config/settings.py`
- Modify: `bot/app.py`
- Modify: `scheduler/app.py`
- Modify: `scheduler/arq_worker.py`
- Modify: `scheduler/service.py`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_settings.py`
- Test: `tests/test_observability.py`
- Test: `tests/test_scheduler.py`

- [ ] Write failing tests for optional blank Sentry/LangSmith settings, validated log levels, Sentry initialization, LangSmith environment export, all three runtime entrypoints, and both scheduler exception logs.
- [ ] Run:

```bash
uv run pytest tests/test_settings.py tests/test_observability.py tests/test_scheduler.py -q
```

  Expect failures because optional integration settings and `configure_observability()` do not exist.
- [ ] Add:

```python
def configure_observability(settings: Settings | None = None) -> None:
    active = settings or get_settings()
    logging.basicConfig(
        level=active.app.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
```

  Initialize Sentry only with a configured DSN, export LangSmith variables only when
  key and project are both configured, and never log secret values.
- [ ] Call the configurator from bot/scheduler `main()` and ARQ `worker_startup()`;
  add `logger.exception("monitor job failed user=%s ...")` to both caught monitor paths.
- [ ] Run the focused tests, Ruff, and mypy; expect all to pass.
- [ ] Commit:

```bash
git add config bot/app.py scheduler .env.example README.md tests
git commit -m "feat: configure runtime observability"
```

### Task 2: Container, Compose, and deployment hardening

**Files:**
- Modify: `Containerfile`
- Modify: `podman-compose.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/container.yml`
- Modify: `deploy/vps/bootstrap_ubuntu_24.sh`
- Create: `deploy/systemd/wait_for_datastores.sh`
- Modify: `deploy/systemd/krisha-agent-compose.service.template`
- Modify: `.env.example`
- Modify: `tests/test_infra_files.py`

- [ ] Write failing infrastructure tests asserting:

```text
uv.lock copied; uv sync uses --locked; multi-stage runtime; /opt/uv-python;
USER pwuser; loopback datastore ports; authenticated Redis; healthy dependencies;
bounded logging; datastore wait before migrate; UFW; CI image smoke.
```

- [ ] Run `uv run pytest tests/test_infra_files.py -q`; expect failures.
- [ ] Replace the single-stage image with builder/runtime stages based on the same
  Playwright image. Install locked dependencies in the builder, copy only `/app` and
  `/opt/uv-python` into runtime, keep application files root-owned, and end with:

```dockerfile
USER pwuser
CMD ["python", "-m", "bot"]
```

- [ ] Bind PostgreSQL/Redis host ports to `127.0.0.1`, require a non-empty Redis
  password, authenticate the Redis health check, use long-form `service_healthy`
  dependencies, add `json-file` logging with `max-size: 10mb`, and set scheduler
  `stop_grace_period`.
- [ ] Add a bounded datastore wait script used before migration by systemd. Enable UFW
  with default-deny incoming and an explicit SSH allowance.
- [ ] Change CI sync to `uv sync --dev --locked`. Make the container workflow load and
  smoke the built image as non-root:

```bash
docker run --rm --entrypoint sh krisha-agent:test -c \
  'test "$(id -u)" = 1000 && python -c "import bot.app, scheduler.app"'
```

- [ ] Run infrastructure tests, `uv lock --check`, and build the image.
- [ ] Commit:

```bash
git add Containerfile podman-compose.yml .github deploy .env.example tests/test_infra_files.py
git commit -m "build: harden container and service startup"
```

### Task 3: Graceful scheduler shutdown

**Files:**
- Modify: `scheduler/app.py`
- Test: `tests/test_scheduler.py`

- [ ] Write async failing tests for a pre-set stop event, interruption of idle wait,
  completion of an active iteration, owned/external queue cleanup, bot cleanup, and
  signal-handler registration/removal.
- [ ] Run the focused scheduler tests and verify they fail.
- [ ] Add an event-driven loop:

```python
async def _wait_for_stop(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        pass
```

  Register SIGTERM/SIGINT in the async entry wrapper, stop before starting another
  iteration, and retain existing `finally` ownership rules.
- [ ] Run focused tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add scheduler/app.py tests/test_scheduler.py
git commit -m "feat: stop scheduler loops gracefully"
```

### Task 4: 2GIS unknown counts

**Files:**
- Modify: `agent/tools/two_gis_client.py`
- Test: `tests/test_two_gis_client.py`
- Test: `tests/test_enrich_node.py`
- Test: `tests/test_scoring_node.py`

- [ ] Write failing tests for HTTP errors, invalid JSON/total, valid zero, positive
  cache reuse, versioned count keys, partial summaries, and scorer `unknown` output.
- [ ] Run the focused tests and verify the current implementation returns/caches zero.
- [ ] Change:

```python
@dataclass(slots=True, frozen=True)
class NearbySummary:
    schools: int | None
    parks: int | None
    metro: int | None
```

  Return `None` for request or payload failures, accept only non-boolean non-negative
  integer totals, cache only integers, and use `2gis:cnt:v2:` keys.
- [ ] Run focused tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add agent/tools/two_gis_client.py tests
git commit -m "fix: preserve unknown 2gis counts"
```

### Task 5: Atomic PostgreSQL writes and feedback index

**Files:**
- Modify: `db/repositories.py`
- Modify: `db/models.py`
- Create: `alembic/versions/<revision>_add_user_feedback_index.py`
- Modify: `tests/test_db_schema.py`

- [ ] Write failing SQL/repository tests for PostgreSQL `ON CONFLICT` statements,
  input-order reconstruction, feedback metadata preservation, and newly-seen semantics.
- [ ] Replace read-before-write paths with:

```python
from sqlalchemy.dialects.postgresql import insert

insert(Model).values(values).on_conflict_do_update(
    index_elements=[...],
    set_={...},
).returning(Model)
```

  Use `on_conflict_do_nothing(...).returning(SeenApartment.apartment_id)` for seen
  links. Deduplicate write values and reconstruct results in original order.
- [ ] Replace `idx_apartment_feedback_decision_decided_at` with:

```python
Index(
    "idx_apartment_feedback_user_decision_deleted_decided",
    "user_id",
    "decision",
    "deleted_at",
    desc("decided_at"),
)
```

  Add reversible Alembic upgrade/downgrade operations.
- [ ] Run schema/repository tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add db alembic tests/test_db_schema.py
git commit -m "fix: make apartment persistence concurrency safe"
```

### Task 6: Real PostgreSQL tests and coverage gate

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_repositories.py`
- Create: `tests/integration/test_scheduler_persistence.py`
- Modify: `tests/test_scheduler.py`
- Modify: `.github/workflows/ci.yml`

- [ ] Add `pytest-cov` and integration fixtures that require `TEST_DATABASE_URL`, reject
  database names not ending in `_test`, apply Alembic migrations, expose independent
  async sessions, and truncate application tables after each test.
- [ ] Add real PostgreSQL tests for concurrent upserts, per-user feedback,
  save/delete/trash/restore/count, due monitor ordering/limits, and scheduler transaction
  behavior. Add unit coverage for `process_monitor_target_job()`.
- [ ] Configure a healthy PostgreSQL 16 CI service, migration
  upgrade/downgrade/re-upgrade, and:

```bash
uv run pytest --cov=agent --cov=bot --cov=config --cov=db --cov=scheduler \
  --cov-report=term-missing --cov-fail-under=70
```

- [ ] Run the integration suite against a disposable `*_test` database and confirm no
  tests skip in CI mode.
- [ ] Commit:

```bash
git add pyproject.toml uv.lock tests/integration tests/test_scheduler.py .github/workflows/ci.yml
git commit -m "test: exercise persistence on postgres"
```

### Task 7: Redis-backed Telegram FSM

**Files:**
- Modify: `bot/app.py`
- Test: `tests/test_bot_app.py`
- Modify: `tests/test_bot_router.py`

- [ ] Write failing tests that production dispatcher storage is Redis-backed, receives
  the authenticated configured URL, and injected memory storage remains usable in unit
  tests.
- [ ] Add:

```python
def create_fsm_storage() -> RedisStorage:
    return RedisStorage.from_url(get_settings().redis.redis_url)

def create_dispatcher(
    service: SearchBotService | None = None,
    *,
    storage: BaseStorage | None = None,
) -> Dispatcher:
    return Dispatcher(storage=storage if storage is not None else create_fsm_storage())
```

  Rely on aiogram's registered FSM shutdown hook to close the Redis pool.
- [ ] Run bot app/router tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add bot/app.py tests/test_bot_app.py tests/test_bot_router.py
git commit -m "fix: persist telegram dialog state in redis"
```

### Task 8: Search intent and dedup UX

**Files:**
- Modify: `agent/nodes/intent_node.py`
- Modify: `agent/graph.py`
- Modify: `agent/nodes/search_node.py`
- Modify: `agent/tools/krisha_parser.py`
- Modify: `bot/service.py`
- Modify: `bot/dialog_agent.py`
- Modify: `bot/router.py`
- Test: `tests/test_intent_node.py`
- Test: `tests/test_search_graph.py`
- Test: `tests/test_krisha_parser.py`
- Test: `tests/test_bot_service.py`
- Test: `tests/test_dialog_agent.py`
- Test: `tests/test_bot_router.py`

- [ ] Write failing tests proving unchanged refinement never persists/runs a search,
  unsupported/missing city produces an explicit default-city notice, and `/foryou`
  uses a dedup namespace separate from manual search.
- [ ] Add `ParsedIntent(criteria, defaulted_city)` while preserving
  `IntentNode.parse() -> SearchCriteria`. Add notices to `SearchExecution`.
- [ ] Raise `CriteriaUnchangedError` before `_persist_and_run_search()` when refined
  criteria equal active criteria; handle it in direct commands, FSM, and dialog turns
  without clearing the refinement state.
- [ ] Thread `dedup_namespace` through the default graph-node factory and
  `KrishaParser` constructor. Use:

```text
krisha:seen:<namespace>:<user_id>:<external_id>
```

  Manual search keeps the default namespace; recommendations use `foryou`.
- [ ] Run all focused tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add agent bot tests
git commit -m "fix: make search refinements and recommendations explicit"
```

### Task 9: Shared apartment-card sender

**Files:**
- Create: `bot/card_sender.py`
- Modify: `bot/router.py`
- Modify: `scheduler/notifier.py`
- Test: `tests/test_card_sender.py`
- Modify: `tests/test_bot_router.py`
- Modify: `tests/test_scheduler.py`

- [ ] Write failing tests for photo success, Telegram photo rejection with text
  fallback, no-photo text delivery, custom keyboard, and recommendation suffix.
- [ ] Implement one helper accepting bound async text/photo callables:

```python
async def send_apartment_card(
    item: EnrichedApartment,
    *,
    index: int,
    reply_markup: InlineKeyboardMarkup,
    send_text: TextSender,
    send_photo: PhotoSender,
    caption_suffix: str | None = None,
) -> None:
    ...
```

- [ ] Replace all five duplicated loops: search, saved, trash, recommendations, and
  monitor notifications.
- [ ] Run focused tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add bot/card_sender.py bot/router.py scheduler/notifier.py tests
git commit -m "refactor: centralize apartment card delivery"
```

### Task 10: Extract pure Krisha HTML parsing

**Files:**
- Create: `agent/tools/krisha_html_parser.py`
- Modify: `agent/tools/krisha_parser.py`
- Modify: `agent/tools/__init__.py`
- Create: `tests/test_krisha_html_parser.py`
- Modify: `tests/test_krisha_parser.py`

- [ ] Move fixture-based parsing tests to a pure parser test that does not create
  Playwright or Redis fakes. Keep orchestration/dedup/anti-bot integration tests on
  `KrishaParser`.
- [ ] Run the new tests before implementation and verify imports/class are missing.
- [ ] Move `ListingPreview`, blocked-page detection, listing/detail parsing, selectors,
  and extraction helpers into `KrishaHTMLParser`. Inject it into `KrishaParser`:

```python
def __init__(..., html_parser: KrishaHTMLParser | None = None) -> None:
    self._html_parser = html_parser or KrishaHTMLParser()
```

  Keep compatibility delegation methods for public parser calls used by existing code.
- [ ] Confirm fixture outputs, canary behavior, criteria filtering, URL building,
  browser lifecycle, and Redis claim/release behavior remain unchanged.
- [ ] Run parser/canary tests, Ruff, and mypy.
- [ ] Commit:

```bash
git add agent/tools tests
git commit -m "refactor: isolate krisha html parsing"
```

### Task 11: Full verification and final audit

**Files:**
- Modify only files required by failures found during verification.

- [ ] Run:

```bash
uv lock --check
uv sync --dev --locked
uv run ruff check .
uv run mypy agent bot config db scheduler
uv run pytest
```

- [ ] Run PostgreSQL integration tests and coverage with a disposable database ending
  in `_test`; require at least 70%.
- [ ] Run Alembic upgrade, one-revision downgrade, and re-upgrade.
- [ ] Build the container without cache and verify UID 1000, imports, Alembic,
  Playwright Chromium launch, and Compose configuration without printing real secrets.
- [ ] Inspect `git diff --check`, staged file scope, and recent commits. Do not include
  `.env`, credentials, caches, generated coverage files, or unrelated user changes.
- [ ] If verification requires a corrective edit, add a regression test first and make
  a focused final commit:

```bash
git commit -m "fix: complete audit hardening"
```
