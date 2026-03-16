# Krisha Agent

Autonomous multi-agent system for apartment discovery in Kazakhstan.  
Current scope is **Phase 0 + Phase 4 baseline**: foundation, parser, LangGraph search pipeline, enrichment, Gemini-backed scoring, checkpoint memory, Telegram bot skeleton, tests, and CI.

## Tech Stack

- Python 3.12
- Pydantic + pydantic-settings
- SQLAlchemy 2 (async) + Alembic
- LangGraph (search graph baseline implemented)
- aiogram
- Redis, PostgreSQL
- Playwright + BeautifulSoup (Krisha parser)
- uv, ruff, mypy, pytest, pre-commit

## Repository Structure

```text
.
├── agent/
│   ├── graph.py
│   ├── models/
│   ├── nodes/
│   └── tools/
├── bot/
├── config/
├── db/
├── scheduler/
├── alembic/
│   └── versions/
├── tests/
└── .github/workflows/ci.yml
```

## Quick Start

1. Install dependencies for local development:

```bash
# uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# pre-commit
python3 -m pip install --user pre-commit

# podman (Ubuntu)
sudo apt-get update && sudo apt-get install -y podman podman-compose
```

2. Clone and enter project:

```bash
git clone https://github.com/Modern-Messiah/Autonomous-Personal-Assistant-AI-Agent-.git
cd Autonomous-Personal-Assistant-AI-Agent-
```

3. Prepare environment:

```bash
cp .env.example .env
```

4. Install Python dependencies:

```bash
uv sync --dev
```

5. Enable pre-commit:

```bash
pre-commit install
```

## Development Commands

```bash
# lint
uv run ruff check .

# format
uv run ruff format .

# type check
uv run mypy agent bot config db

# tests
uv run pytest
```

## Database Migrations

```bash
# apply migrations
uv run alembic upgrade head

# create new migration
uv run alembic revision -m "describe_change"
```

Alembic reads connection settings from:
- `DATABASE_URL` (if provided), or
- `DB__*` variables from `.env` / environment.

## Environment Variables

The project uses nested settings via `pydantic-settings` and `env_nested_delimiter="__"`:

- `APP__ENV`, `APP__LOG_LEVEL`
- `DB__HOST`, `DB__PORT`, `DB__NAME`, `DB__USER`, `DB__PASSWORD`
- `REDIS__HOST`, `REDIS__PORT`, `REDIS__DB`, `REDIS__PASSWORD`
- `TELEGRAM__BOT_TOKEN`
- `API__TWO_GIS_API_KEY`, `API__GEMINI_API_KEY`
- `API__LANGSMITH_API_KEY`, `API__LANGSMITH_PROJECT`
- `API__SENTRY_DSN`

See `.env.example` for the full contract.

## Current Status

- Implemented:
  - Project foundation and tooling.
  - Pydantic models (`SearchCriteria`, `Apartment`, `ApartmentScore`, `EnrichedApartment`).
  - SQLAlchemy async schema + Alembic init migration.
  - `KrishaParser` (Playwright-first), anti-bot fallback, randomized UA support, Redis-based dedup.
  - `IntentNode` (rule-based text -> `SearchCriteria`) and `run_search_graph_from_text`.
  - `SearchNode` + `run_search_graph` pipeline on LangGraph.
  - `EnrichNode` with mortgage annuity calculation and 2GIS nearby summary client.
  - `ScoringNode` with Gemini structured JSON scoring and graceful fallback on scorer errors.
  - Optional Postgres-backed LangGraph checkpointing via `thread_id` and official saver integration.
  - Telegram bot baseline on `aiogram` with `/start`, `/search`, `/criteria`, user registration, and active criteria persistence.
  - Search result persistence in `apartments` / `seen_apartments` and `/list` for the latest saved apartments.
  - HTML fixture-based parser tests and CI checks.
- Not implemented yet: conversational bot flow, `/monitor`, Notion sync, scheduler runtime.

## Telegram Bot Baseline

Run the bot locally after filling `.env`:

```bash
uv run python -m bot
```

Available commands:

- `/start` registers the Telegram user and shows a short usage guide.
- `/search <query>` parses text into `SearchCriteria`, stores it as active criteria, and runs the LangGraph search pipeline.
- `/criteria` returns the last active criteria stored for the Telegram user.
- `/list` returns recently saved apartments linked to the Telegram user.
