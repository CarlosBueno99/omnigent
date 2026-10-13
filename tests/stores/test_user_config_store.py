"""Per-user synced settings in the preferences table."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlPreference, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.user_config_store import SyncedConfig, UserConfigStore

_SETTINGS = {"model": "gpt-5", "harness": {"codex-native": {"args": ["--profile", "work"]}}}


@pytest.fixture()
def store(db_uri: str) -> UserConfigStore:
    return UserConfigStore(db_uri)


def test_save_bumps_the_revision_only_when_settings_change(store: UserConfigStore) -> None:
    assert store.get("alice@example.com") == SyncedConfig({}, 0)
    assert store.save("alice@example.com", {}) == SyncedConfig({}, 0)
    first = store.save("alice@example.com", _SETTINGS)
    assert first == SyncedConfig(_SETTINGS, 1)
    assert store.save("alice@example.com", dict(_SETTINGS)) == first
    second = store.save("alice@example.com", {"model": "claude-opus"})
    assert second == SyncedConfig({"model": "claude-opus"}, 2)
    assert store.get("alice@example.com") == second
    assert store.save("alice@example.com", {}) == SyncedConfig({}, 3)


def test_settings_are_scoped_per_user_and_workspace(store: UserConfigStore) -> None:
    store.save("alice@example.com", _SETTINGS)
    store.save(None, {"model": "local-only"})
    assert store.get("bob@example.com") == SyncedConfig({}, 0)
    assert store.get(None) == SyncedConfig({"model": "local-only"}, 1)
    with workspace_scope(7):
        assert store.get("alice@example.com") == SyncedConfig({}, 0)
        assert store.save("alice@example.com", {"model": "ws7"}) == SyncedConfig(
            {"model": "ws7"}, 1
        )
    assert store.get("alice@example.com") == SyncedConfig(_SETTINGS, 1)
    # An explicit workspace wins over the request-bound one.
    assert store.get("alice@example.com", workspace_id=7) == SyncedConfig({"model": "ws7"}, 1)
    assert store.save("alice@example.com", {"model": "ws7-next"}, workspace_id=7).revision == 2
    assert store.get("alice@example.com") == SyncedConfig(_SETTINGS, 1)


def test_unreadable_row_reads_as_revision_zero(
    store: UserConfigStore, db_uri: str, caplog: pytest.LogCaptureFixture
) -> None:
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(
            SqlPreference(
                workspace_id=0,
                user_id="alice@example.com",
                key="synced_config",
                value='{"revision": 3, "settings": {"providers": {}}}',
            )
        )
        session.commit()
    with caplog.at_level(logging.WARNING, logger="omnigent.stores.user_config_store"):
        assert store.get("alice@example.com") == SyncedConfig({}, 0)
    assert "Ignoring unreadable synced config row" in caplog.text
    assert store.save("alice@example.com", _SETTINGS) == SyncedConfig(_SETTINGS, 1)


def test_save_preserves_other_preferences(store: UserConfigStore, db_uri: str) -> None:
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.add(SqlPreference(workspace_id=0, user_id="local", key="theme", value='"dark"'))
        session.commit()
    store.save(None, _SETTINGS)
    with Session(engine) as session:
        theme = session.get(SqlPreference, (0, "local", "theme"))
        assert theme is not None and theme.value == '"dark"'
