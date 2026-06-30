# Graceful Scheduler Shutdown Design

## Goal

Allow inline scheduler and ARQ producer loops to stop on SIGTERM or SIGINT after their
current iteration, while reliably closing owned Telegram and Redis resources.

## Validated finding

`scheduler/app.py` contains unconditional `while True` loops followed by
`asyncio.sleep()`. The scheduler entrypoint installs no SIGTERM handler.

Container and service managers normally stop processes with SIGTERM and later escalate
to SIGKILL. With the current code, SIGTERM can terminate Python before normal loop exit,
and cleanup in `finally` is not guaranteed to run.

SIGKILL cannot be caught or handled by application code. Graceful cleanup is possible
only during the orchestrator's termination grace period.

## Shutdown invariant

- SIGTERM and SIGINT request shutdown instead of terminating the scheduler immediately.
- An active scheduler iteration is allowed to finish.
- Idle sleep is interrupted immediately when shutdown is requested.
- No new iteration starts after the stop request.
- Owned ARQ pools and Telegram sessions close exactly once.
- Existing one-shot scheduler functions remain unchanged.

## Design

### Stop event and signal registration

Add an async scheduler entry wrapper that:

1. creates an `asyncio.Event`;
2. registers SIGTERM and SIGINT callbacks through the running event loop;
3. invokes `run_scheduler_forever(stop_event=event)`;
4. removes installed signal handlers during final cleanup.

Signal callbacks only set the event and perform no blocking work.

Signal installation will tolerate platforms that do not implement
`loop.add_signal_handler`; Linux remains the production target.

### Event-driven loops

Add an optional stop event to:

- `run_scheduler_forever`;
- `run_scheduler_enqueue_forever`.

Each loop follows:

```text
while stop is not requested:
    finish one complete iteration
    wait for either stop_event or poll timeout
```

The interruptible wait replaces `asyncio.sleep()`. A signal during an active iteration
sets the event but does not cancel database, queue, notification, or parser operations
halfway through.

The existing `finally` blocks continue to own cleanup:

- inline runtime closes its Telegram bot session;
- ARQ producer closes an internally-created Redis pool;
- externally-injected service and queue objects are not closed by the scheduler.

### Orchestrator grace period

Set an explicit Compose `stop_grace_period` for `scheduler-producer` long enough for a
normal enqueue/purge iteration to finish. The systemd `TimeoutStopSec` must not be
shorter than that grace period.

The standalone inline scheduler can execute slower search work. It will finish the
current iteration when launched directly, but no finite orchestrator grace period can
guarantee cleanup for a permanently hung external request.

ARQ worker signal behavior is managed by ARQ itself and is not replaced by these loops.

### Logging

Using the H-04 runtime logging configuration, record:

- receipt of a shutdown request;
- completion of the active iteration;
- closure of owned resources;
- completion of scheduler shutdown.

Do not log credentials or full search criteria.

## Tests and validation

Add async tests that verify:

- a pre-set stop event prevents a new iteration;
- setting the event during idle wait exits immediately;
- setting the event during an active iteration allows that iteration to complete but
  prevents the next;
- internally-owned ARQ pools close on shutdown;
- externally supplied pools are not closed;
- internally-created Telegram sessions close on shutdown;
- signal handlers set the stop event and are removed during cleanup;
- existing one-shot scheduler behavior remains unchanged.

Run full pytest, Ruff, and strict mypy gates.

Runtime validation in an isolated container will:

1. start the scheduler producer;
2. send SIGTERM;
3. confirm the process exits before the grace deadline;
4. confirm logs show shutdown and pool cleanup;
5. confirm no SIGKILL escalation was required.

## Remaining risk

- SIGKILL, kernel termination, host loss, and runtime crashes cannot run cleanup.
- A hung in-flight external request can outlast the configured grace period.
- Bot polling and ARQ worker have separate framework-managed shutdown paths.
- Graceful shutdown does not make an interrupted multi-service deployment atomic.

## Non-goals

- cancelling active monitor jobs;
- changing ARQ worker retry or signal behavior;
- graceful shutdown for PostgreSQL or Redis images;
- distributed leader election;
- exactly-once scheduler execution.
