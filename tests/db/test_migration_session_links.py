"""The additive schema works on an existing database and survives app rollback."""

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import OmnigentBase
from omnigent.db.utils import _build_alembic_config


def test_session_links_upgrade_and_downgrade(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'links.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    tables = (
        "session_link_epochs",
        "session_links",
        "session_input_authorities",
        "linked_requests",
        "linked_replies",
    )
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "mm1a2b3c4d5e")
            command.upgrade(config, "nn1a2b3c4d5e")
        inspector = sa.inspect(engine)
        for name in tables:
            expected = OmnigentBase.metadata.tables[name]
            actual = inspector.get_columns(name)
            assert {column["name"] for column in actual} == set(expected.columns.keys())
            assert all(column["default"] is None for column in actual)
            assert not inspector.get_foreign_keys(name)
            assert inspector.get_pk_constraint(name)["constrained_columns"][0] == "workspace_id"
            assert {index["name"] for index in inspector.get_indexes(name)} == {
                index.name for index in expected.indexes
            }
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "mm1a2b3c4d5e")
        assert not set(tables) & set(sa.inspect(engine).get_table_names())
        assert "conversations" in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
