"""Upgrade keeps a ``scheduled_tasks.project_id`` column that predates its migration.

Revision ``0638f6e46137`` adds the column only when it is missing, so an upgrade
replayed over a schema that already has it (a rewound ``alembic_version``, or a
column patched in outside Alembic) reaches head without a duplicate-column
failure and without losing rows. The fresh-upgrade path is covered by
``test_migration_scheduled_tasks.py::test_scheduled_tasks_columns``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import (
    _build_alembic_config,
    _create_engine,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    clear_engine_cache,
)

# Revision one step before ``0638f6e46137`` (which adds ``project_id``).
_REVISION_BEFORE_PROJECT_ID = "nf12ab34cd56"


@pytest.fixture(autouse=True)
def _clean_engine_cache() -> Iterator[None]:
    try:
        yield
    finally:
        clear_engine_cache()


def test_upgrade_keeps_a_preexisting_project_id_column(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'chat.db'}"
    engine = _create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, _REVISION_BEFORE_PROJECT_ID)

    project_id = b"\x11" * 16
    with engine.connect() as conn:
        conn.execute(sa.text("ALTER TABLE scheduled_tasks ADD COLUMN project_id BLOB"))
        conn.execute(
            sa.text(
                "INSERT INTO scheduled_tasks "
                "(workspace_id, id, name, prompt, rrule, agent_id, created_at, project_id) "
                "VALUES (0, :id, 'nightly', :prompt, 'FREQ=DAILY', :agent, 1, :project)"
            ),
            {"id": b"\x22" * 16, "prompt": b"p", "agent": b"\x33" * 16, "project": project_id},
        )
        conn.commit()

    _initialize_or_verify_schema(engine, uri)

    assert _get_current_db_revision(engine) == _get_head_db_revision(uri)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT project_id FROM scheduled_tasks")).scalars().all() == [
            project_id
        ]
