"""Schema-level smoke tests for ORM metadata and migration file."""

import re
from pathlib import Path

from sqlalchemy import UniqueConstraint

import db.models  # noqa: F401
from db.base import Base


def test_metadata_contains_expected_tables() -> None:
    expected_tables = {"users", "search_criteria", "apartments", "seen_apartments"}
    assert expected_tables.issubset(Base.metadata.tables.keys())


def test_apartments_constraints_and_indexes_present() -> None:
    apartments = Base.metadata.tables["apartments"]
    unique_constraints = {
        constraint.name
        for constraint in apartments.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_apartments_source_external_id" in unique_constraints
    assert "uq_apartments_url" in unique_constraints

    apartment_index_names = {index.name for index in apartments.indexes}
    assert "idx_apartments_created_at" in apartment_index_names


def test_other_indexes_present() -> None:
    search_criteria = Base.metadata.tables["search_criteria"]
    seen_apartments = Base.metadata.tables["seen_apartments"]

    search_index_names = {index.name for index in search_criteria.indexes}
    seen_index_names = {index.name for index in seen_apartments.indexes}

    assert "idx_search_criteria_user_active" in search_index_names
    assert "idx_seen_apartments_first_seen_at" in seen_index_names


def test_init_migration_contains_required_operations() -> None:
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    init_migrations = sorted(versions_dir.glob("*_init_schema.py"))

    assert len(init_migrations) == 1
    migration_text = init_migrations[0].read_text(encoding="utf-8")

    assert re.search(r'op\.create_table\(\s*"users"', migration_text) is not None
    assert re.search(r'op\.create_table\(\s*"search_criteria"', migration_text) is not None
    assert re.search(r'op\.create_table\(\s*"apartments"', migration_text) is not None
    assert re.search(r'op\.create_table\(\s*"seen_apartments"', migration_text) is not None
    assert "idx_search_criteria_user_active" in migration_text
    assert "idx_apartments_created_at" in migration_text
    assert "idx_seen_apartments_first_seen_at" in migration_text
