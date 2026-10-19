"""The additive PR column preserves rows and tolerates a retried migration."""

from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_session_pull_requests_column_round_trip_and_retry(db_uri: str) -> None:
    migration = import_module(
        "omnigent.db.migrations.versions.nn1a2b3c4d5e_add_session_pull_requests"
    )
    store = SqlAlchemyConversationStore(db_uri)
    session = store.create_conversation(title="Keep this session")
    engine = get_or_create_engine(db_uri)
    table_name = "omnigent_conversation_metadata"
    # Fresh CockroachDB bootstraps from the ORM and stamps head without Alembic.
    assert "pull_requests" in {
        column["name"] for column in sa.inspect(engine).get_columns(table_name)
    }
    table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.update().values(task_summary="Preserve metadata"))

    def assert_byte_limit() -> None:
        for value in (None, b"x" * 16_383):
            with engine.begin() as connection:
                connection.execute(table.update().values(pull_requests=value))
                assert connection.scalar(sa.select(table.c.pull_requests)) == value
        with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
            connection.execute(table.update().values(pull_requests=b"x" * 16_384))

    assert_byte_limit()

    def run(direction: str) -> None:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            with Operations.context(MigrationContext.configure(connection)):
                getattr(migration, direction)()
            connection.commit()

    run("downgrade")
    run("downgrade")
    assert "pull_requests" not in {
        column["name"] for column in sa.inspect(engine).get_columns(table_name)
    }
    run("upgrade")
    columns = {column["name"]: column for column in sa.inspect(engine).get_columns(table_name)}
    assert columns["pull_requests"]["nullable"]
    assert_byte_limit()
    with engine.begin() as connection:
        connection.execute(table.update().values(pull_requests=b"preserve on retry"))
    run("upgrade")
    with engine.connect() as connection:
        assert connection.scalar(sa.select(table.c.pull_requests)) == b"preserve on retry"
    run("downgrade")
    run("upgrade")
    with engine.connect() as connection:
        assert connection.execute(
            sa.select(table.c.task_summary, table.c.pull_requests)
        ).one() == ("Preserve metadata", None)
    restored = store.get_conversation(session.id)
    assert restored is not None and restored.title == "Keep this session"
