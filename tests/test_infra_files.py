"""Smoke checks for container and Podman infrastructure files."""

from __future__ import annotations

from pathlib import Path


def test_containerfile_contains_runtime_basics() -> None:
    containerfile = Path(__file__).resolve().parents[1] / "Containerfile"
    text = containerfile.read_text(encoding="utf-8")

    assert "mcr.microsoft.com/playwright/python:v1.47.0-jammy" in text
    assert "pip install --no-cache-dir uv" in text
    assert "uv sync --no-dev" in text
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


def test_container_workflow_builds_containerfile_to_ghcr() -> None:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "container.yml"
    )
    text = workflow.read_text(encoding="utf-8")

    assert "docker/build-push-action@v6" in text
    assert "ghcr.io/${{ github.repository_owner }}/krisha-agent" in text
    assert "file: ./Containerfile" in text
