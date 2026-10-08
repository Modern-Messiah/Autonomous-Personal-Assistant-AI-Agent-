"""Smoke checks for container and Podman infrastructure files."""

from __future__ import annotations

import re
from pathlib import Path


def test_containerfile_contains_runtime_basics() -> None:
    root = Path(__file__).resolve().parents[1]
    containerfile = root / "Containerfile"
    text = containerfile.read_text(encoding="utf-8")

    # The pip playwright pin and the base-image tag ship matched browser
    # binaries — they must always move in lockstep.
    pin = re.search(r'"playwright==([\d.]+)"', (root / "pyproject.toml").read_text("utf-8"))
    assert pin is not None, "pyproject must pin playwright to an exact version"
    assert f"mcr.microsoft.com/playwright/python:v{pin.group(1)}-jammy" in text
    assert "pip install --no-cache-dir uv" in text
    assert "COPY pyproject.toml uv.lock README.md alembic.ini ./" in text
    assert "uv sync --no-dev --locked --no-install-project" in text
    assert "uv sync --no-dev --locked" in text
    assert "AS builder" in text
    assert "AS runtime" in text
    assert "UV_PYTHON_INSTALL_DIR=/opt/uv-python" in text
    assert "USER pwuser" in text
    assert 'CMD ["python", "-m", "bot"]' in text


def test_podman_compose_contains_core_services() -> None:
    compose_file = Path(__file__).resolve().parents[1] / "podman-compose.yml"
    text = compose_file.read_text(encoding="utf-8")

    assert "postgres:" in text
    assert "redis:" in text
    assert "postgres-backup:" in text
    assert "migrate:" in text
    assert "bot:" in text
    assert "scheduler-producer:" in text
    assert "scheduler-worker:" in text
    # the DB holds user feedback/taste data that cannot be re-scraped: the
    # backup service must keep dumping on schedule and expire old dumps
    assert "pg_dump --format=custom" in text
    assert "-mtime +$${BACKUP_RETENTION_DAYS} -delete" in text
    assert "postgres_backups:/backups" in text
    assert 'command: ["python", "-m", "bot"]' in text
    assert 'command: ["python", "-m", "scheduler"]' in text
    assert 'command: ["arq", "scheduler.arq_worker.WorkerSettings"]' in text
    # Postgres/Redis must not be published to the host at all — the app talks to
    # them over the compose network, and host bindings caused port conflicts on
    # the server (bindings removed in d6ef9f2; stricter than the old loopback).
    assert "ports:" not in text
    assert "--requirepass" in text
    assert "condition: service_healthy" in text
    assert 'max-size: "10mb"' in text


def test_container_workflow_builds_containerfile_to_ghcr() -> None:
    workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "container.yml"
    text = workflow.read_text(encoding="utf-8")

    # SHA-pinned (bumps must not silently regress to floating tags)
    assert re.search(r"docker/build-push-action@[0-9a-f]{40}", text)
    assert "ghcr.io/${{ github.repository_owner }}/krisha-agent" in text
    assert "file: ./Containerfile" in text
    assert "docker run --rm" in text


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    # These workflows hold production secrets (SSH deploy key, GHCR push); a
    # retargeted floating tag on any action would run attacker code with them.
    # Every external `uses:` must therefore be pinned to a full commit SHA
    # (local workflow calls like ./.github/workflows/ci.yml are fine).
    workflows_dir = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for workflow in workflows_dir.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for match in re.finditer(r"uses:\s*(\S+)", text):
            ref = match.group(1)
            if ref.startswith("./"):
                continue
            action = ref.split("@", 1)[0]
            assert re.fullmatch(r"[0-9a-f]{40}", ref.split("@", 1)[1]), (
                f"{workflow.name}: {action} must be pinned to a commit SHA, not a tag"
            )


def test_ci_defines_the_full_check_matrix() -> None:
    ci = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")

    # The PR check matrix: dropping one of these silently shrinks review
    # coverage back to "two green checks".
    for job in (
        "lint:",
        "format:",
        "typecheck:",
        "lockfile:",
        "migrations:",
        "tests:",
        "security-audit:",
        "secrets-scan:",
        "infra-lint:",
    ):
        assert f"  {job}" in text, f"ci.yml must keep the {job.rstrip(':')} job"
    # migrations really cycle and the lockfile is really verified
    assert "alembic downgrade -1" in text
    assert "uv run alembic check" in text  # model drift guard
    assert "uv lock --check" in text
    assert "ruff format --check" in text
    assert "pip-audit" in text
    assert "gitleaks" in text  # secrets scan
    assert "actionlint" in text
    assert "shellcheck" in text


def test_prod_compose_pins_image_per_deploy() -> None:
    prod = Path(__file__).resolve().parents[1] / "podman-compose.prod.yml"
    text = prod.read_text(encoding="utf-8")

    # The production overlay must consume the per-deploy sha-<commit> tag the
    # CD workflow exports as IMAGE_TAG, not a floating :latest — a rollout runs
    # the exact tested artifact and a bad release has a rollback target.
    assert text.count("${IMAGE_TAG:-latest}") == 4
    assert "ghcr.io/modern-messiah/krisha-agent:${IMAGE_TAG:-latest}" in text
    # ...and the CD workflow must forward the commit sha to the deploy script.
    cd = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "cd.yml"
    cd_text = cd.read_text(encoding="utf-8")
    assert "IMAGE_TAG: ${{ github.sha }}" in cd_text
    assert "envs: IMAGE_TAG,DEPLOY_PATH" in cd_text


def test_systemd_deploy_files_exist_with_expected_commands() -> None:
    project_root = Path(__file__).resolve().parents[1]
    unit_template = project_root / "deploy" / "systemd" / "krisha-agent-compose.service.template"
    install_script = project_root / "deploy" / "systemd" / "install_user_service.sh"
    bootstrap_script = project_root / "deploy" / "vps" / "bootstrap_ubuntu_24.sh"

    unit_text = unit_template.read_text(encoding="utf-8")
    install_text = install_script.read_text(encoding="utf-8")
    bootstrap_text = bootstrap_script.read_text(encoding="utf-8")
    wait_script = (project_root / "deploy" / "systemd" / "wait_for_datastores.sh").read_text(
        encoding="utf-8"
    )

    # One compose tooling everywhere: the unit must drive `docker compose`,
    # the same binary the CD pipeline uses over SSH.
    assert "ExecStart=/usr/bin/env docker compose" in unit_text
    assert "ExecStartPre=/usr/bin/env docker compose" in unit_text
    assert "ExecReload=/usr/bin/env docker compose" in unit_text
    assert "Environment=DOCKER_HOST=unix:///run/user/%U/docker.sock" in unit_text
    assert "__PROJECT_ROOT__" in unit_text

    assert 'SERVICE_NAME="krisha-agent-compose.service"' in install_text
    assert 'systemctl --user enable "${SERVICE_NAME}"' in install_text
    assert 'sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g"' in install_text
    assert "docker compose version" in install_text

    # Bootstrap installs the same Docker Engine + compose plugin (rootless for
    # the deploy user) instead of a parallel podman-compose stack.
    assert "https://get.docker.com" in bootstrap_text
    assert "docker-ce-rootless-extras" in bootstrap_text
    assert "dockerd-rootless-setuptool.sh install" in bootstrap_text
    assert 'loginctl enable-linger "${TARGET_USER}"' in bootstrap_text
    assert "./deploy/systemd/install_user_service.sh" in bootstrap_text
    assert "ufw default deny incoming" in bootstrap_text
    assert "wait_for_datastores.sh" in unit_text
    assert "docker compose -f" in wait_script
    assert '"$POSTGRES_USER"' in wait_script
    assert '"$REDIS_PASSWORD"' in wait_script


def test_example_env_and_real_env_never_committed() -> None:
    """Guards the no-leaked-secrets invariant that gitleaks backs up in CI."""
    import subprocess

    root = Path(__file__).resolve().parents[1]
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "\n.env\n" in gitignore or gitignore.startswith(".env\n")

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert ".env" not in tracked, ".env must never be tracked"
    assert ".env.example" in tracked, ".env.example must stay tracked"
