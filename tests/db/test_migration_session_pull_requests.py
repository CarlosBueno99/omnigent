"""Tests for the session pull request tables migration (pr1a2b3c4d5e).

The migration and the ORM models both create the two tables keyed by
``(workspace_id, conversation_id, url_cksum)``, the display-order index ending
in that key, and no database defaults; downgrading drops them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlSessionPullRequest, SqlSessionPullRequestUnlink
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "c8e4f2a6b901"
_KEY = ["workspace_id", "conversation_id", "url_cksum"]
_TABLES = {"session_pull_requests", "session_pull_request_unlinks"}


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


@pytest.fixture(params=["migration", "metadata"])
def engine(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[sa.Engine]:
    """A database whose PR tables come from the migration chain or the ORM models."""
    uri = f"sqlite:///{tmp_path / 'session_prs.db'}"
    engine = sa.create_engine(uri)
    if request.param == "migration":
        _migrate(uri, engine, "upgrade", "head")
    else:
        SqlSessionPullRequest.__table__.create(engine)
        SqlSessionPullRequestUnlink.__table__.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_tables_are_keyed_by_session_and_url_without_defaults(engine: sa.Engine) -> None:
    inspector = sa.inspect(engine)
    links = {column["name"]: column for column in inspector.get_columns("session_pull_requests")}
    assert set(links) == {
        *_KEY,
        "url",
        "provider",
        "host",
        "repository",
        "number",
        "relationship",
        "first_seen_at",
        "last_seen_at",
        "display_rank",
        "title",
        "state",
        "is_draft",
        "state_checked_at",
    }
    assert {name for name, column in links.items() if column["nullable"]} == {
        "title",
        "state",
        "state_checked_at",
    }
    unlinks = {
        column["name"]: column for column in inspector.get_columns("session_pull_request_unlinks")
    }
    assert set(unlinks) == {*_KEY, "url", "unlinked_at"}
    assert not any(column["nullable"] for column in unlinks.values())
    for table, columns in (
        ("session_pull_requests", links),
        ("session_pull_request_unlinks", unlinks),
    ):
        assert inspector.get_pk_constraint(table)["constrained_columns"] == _KEY
        assert all(column["default"] is None for column in columns.values()), table
        assert inspector.get_foreign_keys(table) == []
    [index] = inspector.get_indexes("session_pull_requests")
    assert index["name"] == "ix_session_pull_requests_display"
    assert index["column_names"] == [
        "workspace_id",
        "conversation_id",
        "display_rank",
        "url_cksum",
    ]
    assert inspector.get_indexes("session_pull_request_unlinks") == []


def test_downgrade_drops_both_tables(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'downgrade.db'}"
    engine = sa.create_engine(uri)
    _migrate(uri, engine, "upgrade", "head")
    assert set(sa.inspect(engine).get_table_names()) >= _TABLES

    _migrate(uri, engine, "downgrade", _PREVIOUS)
    assert not _TABLES & set(sa.inspect(engine).get_table_names())
    engine.dispose()
    clear_engine_cache()
