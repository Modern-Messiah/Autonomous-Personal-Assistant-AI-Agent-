# Krisha Agent

Autonomous multi-agent system for apartment discovery in Kazakhstan.  
Current scope is **Phase 0 + Phase 6 baseline**: foundation, parser, LangGraph search pipeline, enrichment, Gemini-backed scoring, checkpoint memory, Telegram bot, persistent monitor settings, scheduler runtime baseline, tests, and CI.

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
uv run mypy agent bot config db scheduler

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
- `SCHEDULER__POLL_INTERVAL_SECONDS`, `SCHEDULER__BATCH_SIZE`

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
- Supervisor-style dialog agent for free-text turns, refinement routing, and natural-language fallback without explicit commands.
  - Dialog refinement baseline with `/refine`, `/cancel`, FSM-based follow-up text, and inline "Сохранить" / "Отклонить" / "Уточнить критерии" actions after search results.
  - Search result persistence in `apartments` / `seen_apartments` and `/list` for the latest saved apartments.
  - Persistent monitor settings with `/monitor`, `/monitor on|off`, and `/monitor interval 6h`.
  - Scheduler runtime baseline that polls enabled monitor targets, respects `interval_minutes`, and sends only newly discovered apartments.
  - HTML fixture-based parser tests and CI checks.
- Not implemented yet: richer multi-step approval memory, ARQ-based production scheduler, Notion sync.

## Telegram Bot Baseline

Run the bot locally after filling `.env`:

```bash
uv run python -m bot
```

Available commands:

- `/start` registers the Telegram user and shows a short usage guide.
- `/search <query>` parses text into `SearchCriteria`, stores it as active criteria, and runs the LangGraph search pipeline.
- plain free-text messages are routed through the dialog agent and can trigger search, refinement, saved-list, criteria, or monitor actions.
- `/refine <query>` merges a free-text refinement into the active criteria and reruns the search.
- `/cancel` exits refinement mode after an inline or manual refine prompt.
- `/criteria` returns the last active criteria stored for the Telegram user.
- `/list` returns recently saved apartments linked to the Telegram user.
- `/monitor` shows current monitor settings.
- `/monitor on|off` enables or disables monitoring for the user.
- `/monitor interval 6h` updates the monitor interval in persistent settings.

Current dialog additions:

- after `/search`, the bot shows inline actions for criteria refinement and saved listings,
- after search results, the dialog enters follow-up mode and waits for feedback or a natural-language clarification,
- the "Уточнить критерии" action opens FSM-based follow-up mode,
- a plain-text clarification like `только 3 комнаты и до 35 млн` merges into active criteria instead of resetting the whole search,
- free-text messages like `покажи сохраненные квартиры` or `какой сейчас мониторинг` are routed through the dialog supervisor without slash commands.

## Scheduler Baseline

Run one long-lived scheduler loop after filling `.env`:

```bash
uv run python -m scheduler
```

Current scheduler behavior:

- loads enabled users with active criteria from PostgreSQL,
- skips users until `interval_minutes` has elapsed since `last_checked_at`,
- runs the LangGraph search pipeline,
- persists search results,
- sends Telegram notifications only for apartments not yet linked in `seen_apartments`.
