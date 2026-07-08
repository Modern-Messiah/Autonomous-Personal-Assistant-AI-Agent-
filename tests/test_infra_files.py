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
    assert "migrate:" in text
    assert "bot:" in text
    assert "scheduler-producer:" in text
    assert "scheduler-worker:" in text
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
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "container.yml"
    )
    text = workflow.read_text(encoding="utf-8")

    # version-agnostic: the action must be used, but bumps must not break this
    assert re.search(r"docker/build-push-action@v\d+", text)
    assert "ghcr.io/${{ github.repository_owner }}/krisha-agent" in text
    assert "file: ./Containerfile" in text
    assert "docker run --rm" in text


def test_systemd_deploy_files_exist_with_expected_commands() -> None:
    project_root = Path(__file__).resolve().parents[1]
    unit_template = (
        project_root
        / "deploy"
        / "systemd"
        / "krisha-agent-compose.service.template"
    )
    install_script = project_root / "deploy" / "systemd" / "install_user_service.sh"
    bootstrap_script = project_root / "deploy" / "vps" / "bootstrap_ubuntu_24.sh"

    unit_text = unit_template.read_text(encoding="utf-8")
    install_text = install_script.read_text(encoding="utf-8")
    bootstrap_text = bootstrap_script.read_text(encoding="utf-8")
    wait_script = (
        project_root / "deploy" / "systemd" / "wait_for_datastores.sh"
    ).read_text(encoding="utf-8")

    assert "ExecStart=/usr/bin/env podman-compose" in unit_text
    assert "ExecStartPre=/usr/bin/env podman-compose" in unit_text
    assert "ExecReload=/usr/bin/env podman-compose" in unit_text
    assert "__PROJECT_ROOT__" in unit_text

    assert 'SERVICE_NAME="krisha-agent-compose.service"' in install_text
    assert 'systemctl --user enable "${SERVICE_NAME}"' in install_text
    assert 'sed "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g"' in install_text

    assert (
        "apt-get install -y podman podman-compose uidmap slirp4netns fuse-overlayfs"
        in bootstrap_text
    )
    assert 'loginctl enable-linger "${TARGET_USER}"' in bootstrap_text
    assert "./deploy/systemd/install_user_service.sh" in bootstrap_text
    assert "ufw default deny incoming" in bootstrap_text
    assert "wait_for_datastores.sh" in unit_text
    assert '"$POSTGRES_USER"' in wait_script
    assert '"$REDIS_PASSWORD"' in wait_script
