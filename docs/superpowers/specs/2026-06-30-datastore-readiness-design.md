# Datastore Readiness Design

## Goal

Prevent cold-start and reboot races by ensuring PostgreSQL and Redis are healthy before
migrations, bot polling, scheduler production, or ARQ work begins.

## Validated finding

The current Compose file uses short-form `depends_on`. Its normalized configuration
shows `condition: service_started` for PostgreSQL and Redis. A started container is not
necessarily ready to accept connections.

The systemd unit currently executes:

```text
podman-compose up -d postgres redis
podman-compose run --rm migrate
```

without a readiness barrier. The first command is also prefixed with `-`, so systemd
ignores its failure and still attempts the migration. On a cold host or database
recovery, migration can race PostgreSQL initialization and fail the deployment.

## Reliability invariant

- Migration must not start until PostgreSQL and Redis healthchecks pass.
- Bot, scheduler producer, and ARQ worker must not start before both datastores are
  healthy.
- A datastore that never becomes healthy must fail startup within a bounded time.
- The implementation must work with Docker Compose locally and the repository's
  external `podman-compose` provider on Ubuntu.
- Redis readiness checks must continue to work after H-02 enables authentication.

## Design

### Compose dependency conditions

Use long-form dependencies for every application service:

```yaml
depends_on:
  postgres:
    condition: service_healthy
  redis:
    condition: service_healthy
```

Apply this to:

- `migrate`;
- `bot`;
- `scheduler-producer`;
- `scheduler-worker`.

PostgreSQL and Redis retain their existing healthchecks. The Redis healthcheck will use
the authenticated CLI environment introduced by H-02.

This gives Docker Compose and compatible Podman Compose providers a declarative
readiness barrier for local and direct Compose usage.

### Provider-independent systemd barrier

Do not rely on `podman-compose up --wait`, because `podman compose` delegates behavior
to whichever external Compose provider is installed.

Add `deploy/vps/wait_for_datastores.sh`. The script will:

1. Resolve the PostgreSQL and Redis container IDs using
   `podman-compose ... ps -q <service>`.
2. Poll each container's health status through `podman inspect`.
3. Print the current service and status while waiting.
4. Exit successfully only after both report `healthy`.
5. Exit non-zero when a container is missing, becomes `unhealthy`, exits, or exceeds
   the configured timeout.

The timeout defaults to 120 seconds and may be overridden with
`DATASTORE_WAIT_TIMEOUT_SECONDS`. Polling uses one-second intervals and never waits
indefinitely.

### systemd startup ordering

Update the unit to execute, in order:

```text
podman-compose up -d postgres redis
wait_for_datastores.sh
podman-compose run --rm migrate
podman-compose up --remove-orphans
```

Remove the leading `-` from datastore startup so a real startup failure stops the unit.
`TimeoutStartSec=900` remains as an outer systemd safety limit.

Reload preserves the same ordering: bring datastores up, wait for health, run migration,
then reconcile application services. Shutdown behavior is unchanged.

## Error handling

- Missing service container IDs produce an explicit error naming the service.
- `unhealthy`, `exited`, or inspection failures stop startup immediately.
- A service that remains `starting` until the deadline reports a timeout and exits
  non-zero.
- Migration failure prevents bot and scheduler startup.
- The script does not print environment variables, database URLs, Redis passwords, or
  other secrets.

## Tests and validation

Extend infrastructure tests to assert:

- all four application services use `condition: service_healthy` for both datastores;
- the systemd unit no longer ignores datastore startup failure;
- readiness execution occurs before migration;
- migration occurs before application startup;
- the wait script has a bounded timeout and handles healthy, unhealthy, missing, and
  timeout states;
- Redis readiness remains compatible with authenticated healthchecks from H-02.

Run:

- focused wait-script and infrastructure tests;
- full pytest;
- Ruff;
- strict mypy;
- Compose configuration validation without printing resolved secret values.

Runtime validation will use isolated test containers or a controlled deployment:

1. Stop and recreate the datastore containers.
2. Confirm migration does not begin while PostgreSQL reports `starting`.
3. Confirm migration begins after both services report `healthy`.
4. Confirm bot, producer, and worker start successfully.
5. Force an unhealthy test service and confirm the wait script exits non-zero within
   the configured timeout.

## Interaction with other approved fixes

- H-02 supplies loopback bindings, Redis authentication, and the authenticated Redis
  healthcheck consumed here.
- H-03 makes migration and application containers run as `pwuser`; the wait script runs
  on the host and does not require container root.
- H-04 makes readiness, migration, and runtime failures visible in container and
  systemd logs.

## Remaining risk

- Compose-provider compatibility still varies, which is why systemd has an independent
  readiness barrier.
- Healthchecks prove service responsiveness, not that every application query will
  succeed.
- A datastore may become unhealthy after startup; runtime reconnect and retry behavior
  is outside this startup-order fix.

## Non-goals

- application-level database retry policies;
- zero-downtime database migration orchestration;
- distributed or multi-host deployment;
- changing scheduler job retries;
- replacing Podman Compose with Kubernetes, Quadlet, or another orchestrator.
