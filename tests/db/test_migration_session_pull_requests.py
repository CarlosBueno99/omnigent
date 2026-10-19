"""The additive PR column preserves rows and tolerates a retried migration."""

from importlib import import_module

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
    table = sa.Table(table_name, sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        assert connection.scalar(sa.select(table.c.pull_requests)) is None
        connection.execute(table.update().values(pull_requests=b"preserve on retry"))
    run("upgrade")
    with engine.connect() as connection:
        assert connection.scalar(sa.select(table.c.pull_requests)) == b"preserve on retry"
    run("downgrade")
    run("upgrade")
    restored = store.get_conversation(session.id)
    assert restored is not None and restored.title == "Keep this session"
