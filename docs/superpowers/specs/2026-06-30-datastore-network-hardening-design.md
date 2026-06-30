# Datastore Network Hardening Design

## Goal

Prevent public access to PostgreSQL and Redis, require Redis authentication, and
establish a host firewall baseline without breaking local development or
container-to-container communication.

## Validated finding

The current Compose stack publishes PostgreSQL and Redis with an empty host IP:

```text
5432/tcp -> HostIp=""
6379/tcp -> HostIp=""
```

This binds both services to all host interfaces. Redis currently responds to
`redis-cli ping` without authentication. On a public VPS, anyone who can reach those
ports can attempt to access application data, cache entries, deduplication state, and
the ARQ job queue.

## Security invariant

- PostgreSQL and Redis must not be reachable through a public host interface.
- Redis must reject unauthenticated commands.
- Production must not start with empty or documented placeholder datastore passwords.
- Enabling host firewall rules must preserve administrator SSH access.
- Bot, migration, scheduler producer, worker, and healthchecks must continue to connect
  after authentication is enabled.

## Design

### Compose network exposure

Keep host access for local debugging but bind both published ports explicitly to the
loopback interface:

```yaml
ports:
  - "127.0.0.1:5432:5432"
```

and:

```yaml
ports:
  - "127.0.0.1:6379:6379"
```

Application containers continue to use Compose DNS names (`postgres` and `redis`) on
the internal Compose network, so loopback host bindings do not affect service-to-service
traffic.

### Redis authentication

Start Redis with append-only persistence and `requirepass`, sourcing the password from
`REDIS__PASSWORD`. Compose expansion must fail when the variable is missing or empty.

The Redis container will expose the same password through `REDISCLI_AUTH` for its
healthcheck. The healthcheck can therefore keep using `redis-cli ping` without placing
the password directly in the healthcheck command.

Existing application paths already support authenticated Redis:

- `RedisSettings.redis_url` adds the password to the Redis URL;
- ARQ worker and producer settings pass the configured Redis password;
- parser deduplication, 2GIS cache, and scheduler queue use those shared settings.

All runtime services must be recreated together after the password changes. Restarting
only Redis would leave old bot or scheduler containers unable to reconnect.

### Production password validation

Keep development flexible while preventing unsafe production startup.

When `APP__ENV` is `prod` or `production`, root settings validation will reject:

- an empty Redis password;
- `DB__PASSWORD=change_me`;
- `REDIS__PASSWORD=change_me`;
- the documented replacement placeholder used in `.env.example`.

Development and test settings may continue using local placeholder credentials.

`.env.example` will document non-empty replacement placeholders and show a command such
as `openssl rand -base64 32` for generating production values.

### Host firewall

`deploy/vps/bootstrap_ubuntu_24.sh` will install UFW and configure:

```text
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"
ufw --force enable
```

`SSH_PORT` defaults to `22` and may be overridden when invoking the bootstrap script.
The SSH allow rule is installed before UFW is enabled to avoid locking out the deploy
operator.

No PostgreSQL or Redis firewall rules will be added. Any future public HTTP service must
open its port explicitly in a separate change.

## Tests and validation

Add repeatable tests that assert:

- Compose binds PostgreSQL and Redis only to `127.0.0.1`;
- Redis starts with `requirepass`;
- the Redis healthcheck uses authenticated CLI configuration;
- Compose requires a non-empty Redis password;
- production settings reject empty and placeholder passwords;
- development settings still accept local credentials;
- bootstrap installs UFW, sets deny-incoming/allow-outgoing defaults, allows the
  configured SSH port, and enables UFW non-interactively.

Run:

- focused infrastructure and settings tests;
- full pytest;
- Ruff;
- strict mypy;
- `docker compose config` with a non-empty test Redis password.

Runtime verification after deployment must show:

- `docker inspect` reports `HostIp=127.0.0.1` for ports 5432 and 6379;
- unauthenticated `redis-cli ping` returns `NOAUTH`;
- authenticated `redis-cli ping` returns `PONG`;
- bot, producer, worker, PostgreSQL, and Redis remain healthy.

## Rollout

1. Generate strong, unique PostgreSQL and Redis passwords.
2. Update the production `.env`.
3. Rebuild the shared application image.
4. Recreate PostgreSQL, Redis, migration, bot, producer, and worker services together.
5. Run the runtime verification above.
6. Apply the updated bootstrap firewall rules on the VPS while preserving the active
   SSH port.

Changing an existing PostgreSQL container's `POSTGRES_PASSWORD` environment variable
does not alter the password already stored in its initialized data volume. Production
operators must rotate the database role password explicitly before changing
`DB__PASSWORD`; automatic database password rotation is outside this patch.

## Remaining risk

- Loopback binding protects host-published ports but does not replace host firewalling.
- Redis authentication is password-based and does not add TLS inside the Compose
  network.
- Anyone with access to container inspection or the production environment file can
  obtain service credentials; secret-manager integration is outside this change.
- UFW cannot protect against a compromised container or deploy user.

## Non-goals

- TLS for PostgreSQL or Redis;
- external secret-manager integration;
- database role redesign;
- automatic rotation of an existing PostgreSQL role password;
- exposing a public web endpoint;
- changing application business logic.
