# Non-Root Container Design

## Goal

Run every application process as the unprivileged `pwuser` account while preserving
Python 3.12, Alembic, Playwright Chromium, bot, producer, and worker functionality.

## Validated finding

The current image has no `USER` instruction, and the running bot reports:

```text
uid=0(root) gid=0(root)
```

The application renders remote Krisha HTML in Chromium. A browser or parser
vulnerability would therefore initially execute with root privileges inside the
container.

The Playwright base image already contains `pwuser` with UID/GID 1000. However, adding
only `USER pwuser` does not work with the current build. A direct runtime check as
`pwuser` fails before importing the application:

```text
Fatal Python error: init_fs_encoding
ModuleNotFoundError: No module named 'encodings'
```

`uv sync` installs the required Python 3.12 interpreter under
`/root/.local/share/uv/python`, and the virtual environment points there. The
unprivileged user cannot traverse `/root`.

## Security invariant

- Bot, migration, scheduler producer, and ARQ worker must not run as UID 0.
- The runtime user must not need access to `/root`.
- Application source and the virtual environment should remain root-owned and
  non-writable by the runtime process.
- Python 3.12 and bundled Playwright browsers must remain executable.
- Runtime-writable data must stay in dedicated locations such as `/tmp` and
  `/home/pwuser`.

## Design

### Accessible uv-managed Python

Configure uv before dependency installation:

```dockerfile
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
```

`uv sync --no-dev` will install Python 3.12 under `/opt/uv-python` and create the
virtual environment with an interpreter link that `pwuser` can traverse.

The build will ensure `/opt/uv-python`, `/app`, and `/app/.venv` are readable and
executable by the runtime user without granting write ownership.

### Runtime user

After all root-only image installation steps:

```dockerfile
USER pwuser
```

The existing `CMD ["python", "-m", "bot"]` remains unchanged. Compose command
overrides for Alembic, scheduler producer, and ARQ worker inherit the same non-root
user.

The image will not run `chown -R pwuser:pwuser /app`. Application code and installed
dependencies do not require runtime writes, and keeping them root-owned prevents a
compromised process from modifying its own executable code inside the container.

The Playwright base image already provides:

- `/home/pwuser` owned by `pwuser`;
- world-readable/executable browser binaries under `/ms-playwright`;
- writable `/tmp`.

These paths cover Chromium profiles, temporary downloads, sockets, and other runtime
temporary files.

## Tests and validation

Extend infrastructure tests to assert:

- `Containerfile` sets `UV_PYTHON_INSTALL_DIR=/opt/uv-python`;
- the final runtime instruction is `USER pwuser`;
- the image does not grant `pwuser` ownership of all `/app`.

Build a fresh image and verify:

```text
id -u                                      -> 1000
python -c "import encodings, agent, bot"   -> exit 0
alembic current                            -> exit 0 with configured database
Playwright Chromium launch                 -> exit 0
python -m bot                              -> starts without permission errors
python -m scheduler                        -> starts without permission errors
ARQ worker                                 -> starts without permission errors
```

The original issue is considered fixed only when `docker inspect` reports
`Config.User=pwuser` and `docker exec ... id` no longer reports UID 0.

Run the full pytest, Ruff, and strict mypy gates in addition to the image-level checks.

## Rollout

All services share one application image, so the image must be rebuilt and bot,
migration, producer, and worker containers must be recreated together.

No persistent volume ownership migration is expected: PostgreSQL and Redis use their
own images and users, while the application containers do not mount writable
application volumes.

## Remaining risk

- Running as `pwuser` limits privileges but does not prove Chromium's process sandbox is
  enabled. Playwright defaults and container security options require a separate
  sandbox-hardening review.
- A container escape vulnerability may still cross the container boundary.
- The runtime process can write to `/tmp` and its own home as required by Chromium.
- Secrets supplied through environment variables remain readable by the application
  process.

## Non-goals

- Enabling Chromium's sandbox or supplying a custom seccomp profile;
- a read-only root filesystem;
- dropping every Linux capability in Compose;
- redesigning the image as a multi-stage build;
- changing business logic or parser behavior.
