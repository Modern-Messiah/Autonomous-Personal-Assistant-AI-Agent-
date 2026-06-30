# PostgreSQL Integration Tests and Feedback Index Design

## Goal

Exercise repository and monitor persistence against real PostgreSQL, enforce a useful
coverage floor in CI, and add the missing per-user feedback index used by `/list`,
`/trash`, and `/foryou`.

## Validated findings

The current suite has 134 passing tests, but database behavior is represented by ORM
metadata checks, migration-text assertions, fake sessions, and monkeypatched repository
functions. No test executes `db/repositories.py` against PostgreSQL.

The measured baseline coverage is:

- 69% overall;
- 19% for `db/repositories.py`;
- 28% for `scheduler/app.py`;
- 0% for `scheduler/arq_worker.py`.

`apartment_feedback` currently has
`idx_apartment_feedback_decision_decided_at(decision, decided_at)`. The main read paths
first resolve one Telegram user and then filter feedback by:

- `user_id`;
- `decision`;
- whether `deleted_at` is null;
- newest `decided_at` for active feedback, or newest `deleted_at` for trash.

The current index does not begin with `user_id`, so it cannot efficiently narrow the
normal per-user access pattern.

## Relationship to the apartment upsert design

The previously approved
`2026-06-30-postgres-apartment-upserts-design.md` defines the `ON CONFLICT` behavior for
apartment records, feedback, and seen links.

This design supplies the real PostgreSQL test infrastructure and concurrency tests
needed to prove that behavior. It does not redefine the upsert contracts.

## Chosen test architecture

Use a PostgreSQL 16 service in GitHub Actions. Do not add testcontainers.

This keeps the database version aligned with production Compose, avoids another Python
dependency and nested container lifecycle, and makes the required database tests
unskippable in CI. Local execution uses an explicitly provided `TEST_DATABASE_URL`,
normally pointing at the existing local Compose PostgreSQL instance.

Pure unit tests remain runnable without PostgreSQL.

## Test database safety and lifecycle

### Dedicated URL

Integration fixtures read only `TEST_DATABASE_URL`. They never fall back to the
application's `DATABASE_URL`, `.env`, or `get_settings()`.

Before any destructive setup or cleanup, validate that the parsed database name ends in
`_test`. Refuse to run otherwise. This prevents an accidental test invocation from
truncating development or production data.

CI uses fixed test-only credentials and a database named `krisha_test`.

### Schema setup

At integration-session startup:

1. export the explicit test URL as `DATABASE_URL` only for Alembic;
2. run `alembic upgrade head`;
3. create an async SQLAlchemy engine and session factory bound directly to the same
   test URL.

At shutdown, dispose the engine. The fixture must not mutate cached production engines
from `db.session`.

### Isolation

Use independent committed sessions where concurrency and visibility are under test.
A single rollback-only outer transaction is insufficient because two concurrent
sessions must observe each other's committed rows and database conflicts.

After every integration test, truncate all application tables with
`RESTART IDENTITY CASCADE`. Cleanup runs in `finally` so a failed assertion does not
contaminate later tests. The suite remains sequential; parallel test workers are a
separate concern.

When `TEST_DATABASE_URL` is absent, integration tests are marked skipped locally. CI
always supplies it, so a skip in CI is treated as a configuration failure.

## PostgreSQL integration coverage

### Repository identity and concurrency

Implement the M-05 cases against two independent sessions:

- concurrent apartment upserts for the same `(source, external_id)` create one row;
- apartment return order and cardinality match input order, including repeated input;
- concurrent feedback upserts create one valid composite-key row;
- feedback upsert restores `deleted_at` while preserving Notion sync metadata;
- concurrent seen inserts create one composite-key row without `IntegrityError`;
- only the transaction that inserted a seen link reports it as newly seen;
- an intentional same-URL/different-identity conflict still fails;
- unrelated writes in successful transactions remain committable.

### Feedback and soft-delete behavior

Exercise the complete persistence flow without monkeypatches:

- save and reject decisions remain isolated by user;
- `/list` returns active saved rows in newest-first order;
- `/foryou` profile inputs load only the requesting user's saved and rejected rows;
- soft delete removes a saved apartment from the active list and places it in trash;
- restore removes it from trash and returns it to the active list;
- counts ignore soft-deleted feedback;
- unknown external IDs return the documented false/no-op result.

### Monitor selection and processing

Create real users, active criteria, and monitor settings to verify
`list_due_monitor_targets()`:

- never-checked enabled monitors are due;
- elapsed intervals are due;
- disabled and not-yet-due monitors are excluded;
- results follow null-first/oldest-first order and respect the requested limit;
- criteria payloads are validated into `SearchCriteria`.

Test `SchedulerService.process_monitor_target()` with the real repository/session
layer and fake external search/notifier boundaries. Verify successful and failed runs,
new-apartment persistence, notification decisions, and `last_checked_at` transaction
behavior.

`process_monitor_target_job()` remains a thin ARQ adapter and gets focused unit tests
for ISO timestamp parsing, service invocation, and summary serialization. Starting a
real ARQ worker or Redis broker is outside this database test layer.

## Per-user feedback index

Add an Alembic migration and matching ORM metadata for:

```text
idx_apartment_feedback_user_decision_deleted_decided
    (user_id, decision, deleted_at, decided_at DESC)
```

Replace, rather than retain, the old
`idx_apartment_feedback_decision_decided_at` index. No current query requires the old
cross-user prefix, and retaining both would add write amplification without serving a
known access path.

With `user_id` and `decision` fixed:

- `deleted_at IS NULL` allows the index to provide active feedback in
  `decided_at DESC` order;
- `deleted_at IS NOT NULL` narrows trash rows, though trash ordering still uses
  `deleted_at DESC`.

The migration downgrade drops the new index and restores the old one.

CI verifies upgrade, one-revision downgrade, and re-upgrade on the disposable test
database. A representative `EXPLAIN` is recorded during implementation validation, but
the test suite does not assert an exact planner string because small-table planner
choices are unstable.

## Coverage gate

Add `pytest-cov` to the development dependency group.

CI runs coverage across `agent`, `bot`, `config`, `db`, and `scheduler`, produces a
term-missing report, and fails below 70%. The current measured baseline is 69%; the new
repository and worker tests must raise it above the gate rather than lowering the
threshold.

The ordinary local `uv run pytest` command remains available without mandatory coverage
collection. CI is the enforcement boundary.

## CI flow

Extend the existing lint/type/test workflow with a PostgreSQL 16 service and health
check. The test job:

1. installs dependencies from the committed lockfile;
2. waits for the PostgreSQL service health check;
3. runs migration upgrade, downgrade, and re-upgrade against `krisha_test`;
4. runs Ruff and strict mypy;
5. runs the full test suite with coverage and a 70% floor;
6. fails if database tests were skipped.

This does not replace the separate container image workflow.

## Alternatives rejected

### Testcontainers only

Testcontainers improves local convenience but adds a dependency, requires a working
container daemon during every database test run, and hides CI service setup behind
another abstraction. The repository already uses Compose, so an explicit test URL is
sufficient locally.

### CI service plus testcontainers

Supporting two database bootstrap paths adds maintenance without testing different
application behavior. It can be added later if local setup becomes a recurring problem.

### SQLite

SQLite cannot prove PostgreSQL `ON CONFLICT`, JSONB, UUID, asyncpg, constraint, locking,
or concurrent-session semantics and would create false confidence.

### Exact EXPLAIN assertions

PostgreSQL may choose a sequential scan for small fixture tables even when the correct
index exists. Schema verification plus representative manual `EXPLAIN` evidence is
more stable than matching a specific CI plan.

## Non-goals

- integration tests for live Telegram, Krisha, DeepSeek, 2GIS, Notion, or LangSmith;
- starting a real ARQ worker or Redis broker;
- parallelizing database tests;
- fixing unrelated user/settings/search-criteria upsert races;
- changing monitor scheduling semantics;
- redesigning feedback or apartment identity;
- enforcing 100% coverage.
