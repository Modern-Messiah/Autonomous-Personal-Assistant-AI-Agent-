FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md alembic.ini ./
COPY agent ./agent
COPY alembic ./alembic
COPY bot ./bot
COPY config ./config
COPY db ./db
COPY scheduler ./scheduler

RUN uv sync --no-dev

CMD ["python", "-m", "bot"]
