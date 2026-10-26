"""Profile snapshots stay workspace scoped and bounded in SQL and serialized bytes."""

import json
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSandboxProfileBinding, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.sandbox_profile_store import SandboxProfileStore, encode_snapshot


def test_get_host_snapshot_is_workspace_scoped_and_bounded(db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    store = SandboxProfileStore(engine)
    host_id, other_host_id = "a" * 32, "b" * 32
    for workspace_id, session_number, bound_host, profile_id in (
        (42, 0, host_id, "other-workspace"),
        (41, 1, other_host_id, "other-host"),
        (41, 3, host_id, "later-session"),
        (41, 2, host_id, "first-session"),
    ):
        with workspace_scope(workspace_id):
            session_id = f"{session_number:032x}"
            store.bind_session(session_id, {"id": profile_id})
            store.bind_host(session_id, bound_host)
    queries = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        assert statement.strip().upper() != "BEGIN IMMEDIATE"
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with workspace_scope(41):
            assert store.get_host_snapshot(host_id) == {"id": "first-session"}
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(queries) == 1
    statement, parameters = queries[0]
    assert "LIMIT" in statement.upper()
    with engine.connect() as connection:
        # Multiple matches must be bounded in SQL, before scalar() consumes a row.
        assert len(connection.exec_driver_sql(statement, parameters).all()) == 1
        if engine.dialect.name == "sqlite":
            plan = str(
                connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
            )
            assert "ix_sandbox_profile_bindings_host" in plan, plan
            assert "workspace_id=? AND host_id=?" in plan, plan
            assert "SCAN" not in plan, plan
            assert "TEMP B-TREE" not in plan, plan

    with workspace_scope(41):
        assert store.get_host_snapshot(other_host_id) == {"id": "other-host"}
        assert store.get_host_snapshot("c" * 32) is None
    with workspace_scope(42):
        assert store.get_host_snapshot(host_id) == {"id": "other-workspace"}
        assert store.get_host_snapshot(other_host_id) is None
    with workspace_scope(43):
        assert store.get_host_snapshot(host_id) is None


@pytest.mark.parametrize(
    "prefix", ["plain", "模型🚀", 'a"b\\c\n'], ids=["ascii", "non-ascii", "json-escaping"]
)
def test_encode_snapshot_bounds_serialized_bytes(prefix: str) -> None:
    snapshot = {"provider": "company", "id": "build", "revision": prefix}
    overhead = len(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode())
    snapshot["revision"] += "a" * (16383 - overhead)

    encoded = encode_snapshot(snapshot)
    assert isinstance(encoded, bytes)
    assert len(encoded) == 16383
    assert json.loads(encoded) == snapshot

    snapshot["revision"] += "a"
    assert len(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()) == 16384
    with pytest.raises(ValueError, match="smaller than 16 KB"):
        encode_snapshot(snapshot)


def test_clear_host_binding_preserves_snapshot_and_other_affinities(db_uri: str) -> None:
    store = SandboxProfileStore(get_or_create_engine(db_uri))
    session_id = uuid.uuid4().hex
    host_id, replacement_id = uuid.uuid4(), uuid.uuid4().hex
    snapshot = {"provider": "company", "id": "build", "revision": "immutable"}
    for workspace_id in (41, 42):
        with workspace_scope(workspace_id):
            store.bind_session(session_id, snapshot)
            store.bind_host(session_id, host_id.hex)

    with workspace_scope(41):
        store.clear_host_binding(session_id, replacement_id)
        store.clear_host_binding(uuid.uuid4().hex, host_id.hex)
        assert store.get_session_host_id(session_id) == host_id.hex
        store.clear_host_binding(session_id, str(host_id))
        assert store.get_session_host_id(session_id) is None
        assert store.get_session_snapshot(session_id) == snapshot
        assert store.get_host_snapshot(host_id.hex) is None
        store.clear_host_binding(session_id, host_id.hex)
        store.bind_host(session_id, replacement_id)
        store.clear_host_binding(session_id, host_id.hex)
        assert store.get_session_host_id(session_id) == replacement_id
        assert store.get_session_snapshot(session_id) == snapshot
        assert store.get_host_snapshot(replacement_id) == snapshot

    with workspace_scope(42):
        assert store.get_session_host_id(session_id) == host_id.hex
        assert store.get_session_snapshot(session_id) == snapshot
        assert store.get_host_snapshot(host_id.hex) == snapshot


@pytest.mark.parametrize("same_selection", [True, False])
def test_concurrent_first_bind_is_idempotent_or_conflicts(db_uri, monkeypatch, same_selection):
    store = SandboxProfileStore(get_or_create_engine(db_uri))
    session_id = uuid.uuid4().hex
    with workspace_scope(41):
        winner = {"id": "build", "revision": "v1", "description": "Build"}
        store.bind_session(session_id, winner)
        candidate = winner if same_selection else {**winner, "description": "Different selection"}
        original_get = Session.get
        missed = False

        def miss_competing_row(session, entity, *args, **kwargs):
            nonlocal missed
            if entity is SqlSandboxProfileBinding and not missed:
                missed = True
                return None
            return original_get(session, entity, *args, **kwargs)

        # Model the stale first read; the real primary key enforces the insert race.
        monkeypatch.setattr(Session, "get", miss_competing_row)
        if same_selection:
            store.bind_session(session_id, candidate)
        else:
            with pytest.raises(ValueError, match="cannot be changed"):
                store.bind_session(session_id, candidate)
        assert missed
        assert store.get_session_snapshot(session_id) == winner
