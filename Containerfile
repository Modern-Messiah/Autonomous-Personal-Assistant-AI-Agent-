FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON=3.12 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md alembic.ini ./
RUN uv sync --no-dev --locked --no-install-project

COPY agent ./agent
COPY alembic ./alembic
COPY bot ./bot
COPY config ./config
COPY db ./db
COPY scheduler ./scheduler

RUN uv sync --no-dev --locked && rm -rf /root/.cache/uv

FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/uv-python /opt/uv-python
COPY --from=builder /app /app

USER pwuser

CMD ["python", "-m", "bot"]
