# Runtime Observability Design

## Goal

Make application logs reliably visible in container output, stop silently swallowing
monitor failures, and make Sentry and LangSmith optional integrations that are actually
activated when configured.

## Current problem

- The project creates module loggers but never configures the root logger.
- `INFO` messages from the parser canary and retention purge are normally suppressed.
- Log formatting and levels depend on whichever framework starts the process.
- `SchedulerService` catches monitor failures in two execution paths without recording
  the exception or affected user.
- `API__SENTRY_DSN`, `API__LANGSMITH_API_KEY`, and
  `API__LANGSMITH_PROJECT` are required settings even though no runtime initializes
  Sentry or LangSmith.
- Configuring only `bot.app.main()` and `scheduler.app.main()` would leave the separate
  ARQ worker process unconfigured.

## Design

### Central runtime configuration

Add `config/observability.py` with one public function:

```python
configure_observability(settings: Settings | None = None) -> None
```

The function will:

1. Configure the root logger with `APP__LOG_LEVEL` and the format
   `%(asctime)s %(levelname)s %(name)s %(message)s`.
2. Explicitly set the root level even when a framework already installed handlers.
3. Initialize Sentry only when a non-empty DSN is configured, using `APP__ENV` as the
   Sentry environment.
4. Export the standard LangSmith runtime variables only when both the API key and
   project are configured:
   `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, and `LANGSMITH_PROJECT`.
5. Emit one startup `INFO` record stating whether Sentry and LangSmith are enabled,
   without logging credentials.

Runtime configuration will be called by all three production processes:

- `bot.app.main()` for Telegram polling;
- `scheduler.app.main()` for the inline scheduler or ARQ producer;
- `scheduler.arq_worker.worker_startup()` for the ARQ worker and parser canary.

### Optional integration settings

Keep the existing environment-variable names for backward compatibility:

- `API__SENTRY_DSN`
- `API__LANGSMITH_API_KEY`
- `API__LANGSMITH_PROJECT`

Change them to optional settings and normalize blank strings to `None`.

Behavior:

- no Sentry DSN: Sentry is disabled and startup succeeds;
- Sentry DSN present: `sentry_sdk.init()` is called;
- both LangSmith key and project present: tracing is enabled;
- either LangSmith value missing: LangSmith remains disabled and startup succeeds.

`API__TWO_GIS_API_KEY`, `API__DEEPSEEK_API_KEY`, and the Telegram/database/Redis
settings remain required because the default search runtime needs them.

### Monitor failure logging

Add a module logger to `scheduler/service.py`.

Both exception handlers in `run_pending_monitors()` and
`process_monitor_target()` will call `logger.exception()` before returning the existing
failure summary. The record will include internal `user_id` and Telegram user ID, but
will not include criteria payloads, tokens, URLs, or other credentials.

The existing control flow remains unchanged: one failed inline target does not stop the
batch, and one failed ARQ target still returns a failed summary. Changing ARQ retry
semantics is outside this fix.

## Error handling and safety

- Observability configuration must never print secrets.
- Missing optional integrations must not prevent bot or scheduler startup.
- Invalid log levels should fail settings validation with a clear error rather than
  silently falling back to an arbitrary level.
- A Sentry initialization error should be visible during startup instead of being
  silently swallowed.
- Caught monitor exceptions will reach Docker logs and Sentry's logging integration
  when Sentry is enabled.

## Tests

Add tests that verify:

- blank Sentry/LangSmith values are accepted as disabled integrations;
- configured Sentry receives the expected DSN and environment;
- complete LangSmith settings export the standard runtime variables;
- incomplete LangSmith settings leave tracing disabled;
- all three runtime entrypoints call the shared configurator;
- both scheduler failure paths emit an exception record containing the affected user;
- existing scheduler summaries and continuation behavior remain unchanged.

Run the full pytest, Ruff, and strict mypy gates. After rebuilding containers, verify
that the startup observability record and canary/purge `INFO` records appear in
`docker compose logs`.

## Documentation and deployment

Update `.env.example` and `README.md` to mark Sentry and LangSmith as optional and to
describe their activation rules.

All runtime containers must be rebuilt and restarted from the same image after the
change so the bot, producer, and worker use the same observability configuration.

## Non-goals

- JSON log formatting or external log aggregation;
- changing monitor retry semantics;
- adding custom Sentry performance sampling;
- adding LangSmith trace annotations beyond the standard LangGraph integration;
- modifying parser, search, ranking, or Telegram behavior.
