"""Default node factories accept injected settings (no env/global lookup)."""

from __future__ import annotations

from pydantic import SecretStr

from agent.nodes.enrich_node import EnrichNode, create_default_enrich_node
from agent.nodes.intent_node import create_default_llm_intent_parser
from agent.nodes.scoring_node import ScoringNode, create_default_scoring_node
from agent.nodes.search_node import SearchNode, create_default_search_node
from config.settings import (
    APISettings,
    DatabaseSettings,
    RedisSettings,
    Settings,
    TelegramSettings,
)


def build_settings() -> Settings:
    """A fully explicit Settings object — no .env file, no environment."""
    return Settings(
        _env_file=None,
        db=DatabaseSettings(
            host="db.test", name="krisha", user="krisha", password=SecretStr("pw")
        ),
        redis=RedisSettings(host="redis.test"),
        telegram=TelegramSettings(bot_token=SecretStr("42:token")),
        api=APISettings(
            two_gis_api_key=SecretStr("2gis-key"),
            deepseek_api_key=SecretStr("deepseek-key"),
        ),
    )


def test_factories_build_from_injected_settings() -> None:
    settings = build_settings()

    # None of these may touch get_settings()/env when settings are injected —
    # constructing them proves the composition seam works without a .env.
    assert isinstance(create_default_search_node(settings=settings), SearchNode)
    assert isinstance(create_default_enrich_node(settings=settings), EnrichNode)
    assert isinstance(create_default_scoring_node(settings=settings), ScoringNode)

    parser = create_default_llm_intent_parser(settings=settings)
    assert parser is not None  # a non-empty key yields a real LLM parser


def test_llm_parser_factory_disables_on_blank_key() -> None:
    settings = build_settings()
    settings.api.deepseek_api_key = SecretStr("")

    assert create_default_llm_intent_parser(settings=settings) is None
