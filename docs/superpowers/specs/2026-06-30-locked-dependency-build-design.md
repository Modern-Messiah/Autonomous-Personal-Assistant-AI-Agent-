# Locked Dependency Build Design

## Goal

Make CI and container dependency installation fail instead of silently resolving or
rewriting versions that differ from the committed `uv.lock`.

## Validated finding

The repository commits `uv.lock`, but:

- CI runs `uv sync --dev` without a lock-enforcement flag;
- the Containerfile runs `uv sync --no-dev` without a lock-enforcement flag;
- the Containerfile does not copy `uv.lock` into the image before syncing.

By default, uv re-locks a project before syncing. A dependency declaration change can
therefore update resolution during CI or image construction instead of proving that the
committed lockfile describes the build.

## Reproducibility invariant

- CI and image builds must never modify dependency resolution.
- Missing or stale `uv.lock` must fail immediately.
- The runtime environment must be installed from the exact committed lockfile.
- Development commands may still update the lockfile intentionally outside CI.

## Design

### Container build

Copy the lockfile with the project metadata before dependency installation:

```dockerfile
COPY pyproject.toml uv.lock README.md alembic.ini ./
```

Install locked third-party runtime dependencies before copying frequently-changing
application source:

```dockerfile
RUN uv sync --no-dev --locked --no-install-project
```

`--locked` is preferred over `--frozen` here. Both prevent lockfile updates, but
`--locked` additionally verifies that `uv.lock` is current for `pyproject.toml` and
fails on drift. This repository is a single project and copies all relevant project
metadata before sync, so the check is available.

After copying `agent`, `bot`, `config`, `db`, `scheduler`, and Alembic sources, run:

```dockerfile
RUN uv sync --no-dev --locked
```

The first sync uses `--no-install-project` because the local package source is not
present yet. The second locked sync installs the project itself without resolving new
third-party versions. This keeps dependency installation in a reusable container layer
without trying to build an incomplete local package.

### CI

Install development dependencies with:

```yaml
run: uv sync --dev --locked
```

CI therefore fails before linting or tests when a dependency edit was committed without
the matching lockfile update.

### Developer workflow

Intentional dependency changes continue to use commands that update the lockfile, such
as:

```text
uv add <package>
uv remove <package>
uv lock
```

The updated `pyproject.toml` and `uv.lock` must be committed together.

## Tests and validation

Extend infrastructure tests to assert:

- `Containerfile` copies `uv.lock`;
- the metadata-only sync uses `--no-dev --locked --no-install-project`;
- the final project sync uses `--no-dev --locked`;
- CI dependency sync uses `--dev --locked`;
- no CI or container sync command omits lock enforcement.

Validation:

1. Run `uv lock --check`.
2. Run `uv sync --dev --locked`.
3. Build the application image.
4. In an isolated temporary copy, modify dependency metadata without updating
   `uv.lock` and confirm locked sync exits non-zero.
5. Run normal lint, type, and test gates.

## Remaining risk

This change locks Python dependency resolution but does not make the complete image
byte-for-byte reproducible. The base image tag, `pip install uv`, operating-system
packages, registry state, and build timestamps remain external inputs. Pinning the uv
installer and container image digest is a separate hardening task.

## Non-goals

- pinning the Playwright base image by digest;
- pinning the uv installer version;
- offline or vendored dependency installation;
- generating software bills of materials or provenance attestations;
- changing application dependencies.
