"""Session search finds a session by a pasted Omnigent or harness session id."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_ALICE = "alice@example.com"
_BOB = "bob@example.com"
_LEVEL_OWNER = 4


@pytest.fixture(params=["single_db", "split_db"])
def stores(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    """A conversation store and the permission store sharing its Omnigent DB."""
    omnigent_uri = f"sqlite:///{tmp_path}/omnigent.db"
    if request.param == "single_db":
        conversation_store = SqlAlchemyConversationStore(omnigent_uri)
    else:
        conversation_store = SqlAlchemyConversationStore(
            omnigent_uri, f"sqlite:///{tmp_path}/conversations.db"
        )
    permission_store = SqlAlchemyPermissionStore(omnigent_uri)
    for user in (_ALICE, _BOB):
        permission_store.ensure_user(user)
    return conversation_store, permission_store


def _archived_ids(store: SqlAlchemyConversationStore, query: str) -> set[str]:
    """Ids the Archived settings view lists for *query* as Alice."""
    page = store.list_conversations(
        search_query=query,
        accessible_by=_ALICE,
        include_archived=True,
        archived_only=True,
    )
    return {conversation.id for conversation in page.data}


def test_search_finds_archived_session_by_any_spelling_of_its_id(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> None:
    """A pasted Omnigent id matches exactly, whatever spelling the user copied."""
    store, permissions = stores
    target = store.create_conversation(title="Quarterly planning")
    other = store.create_conversation(title="Unrelated")
    for conversation in (target, other):
        store.update_conversation(conversation.id, archived=True)
        permissions.grant(_ALICE, conversation.id, _LEVEL_OWNER)

    dashed = str(uuid.UUID(target.id))
    for spelling in (target.id, dashed, f"conv_{target.id}", f"  {target.id}\n"):
        assert _archived_ids(store, spelling) == {target.id}, spelling

    # Exact ids only: a fragment or an id with inner whitespace finds nothing.
    assert _archived_ids(store, target.id[:12]) == set()
    assert _archived_ids(store, f"{target.id[:16]} {target.id[16:]}") == set()


def test_search_finds_archived_import_by_its_harness_session_id(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> None:
    """The harness id a session was imported from finds it, scoped to the caller."""
    store, permissions = stores
    harness_session_id = str(uuid.uuid4())
    alice_import = store.create_conversation(title="Imported from Claude")
    store.set_external_session_id(alice_import.id, harness_session_id)
    store.update_conversation(alice_import.id, archived=True)
    permissions.grant(_ALICE, alice_import.id, _LEVEL_OWNER)

    # Another user's session wrapping the same harness id stays invisible.
    bob_import = store.create_conversation(title="Bob's copy")
    store.set_external_session_id(bob_import.id, harness_session_id)
    store.update_conversation(bob_import.id, archived=True)
    permissions.grant(_BOB, bob_import.id, _LEVEL_OWNER)

    assert _archived_ids(store, harness_session_id) == {alice_import.id}
    assert _archived_ids(store, f" {harness_session_id} ") == {alice_import.id}
    # Non-uuid harness ids (e.g. OpenCode's) match as exact text too.
    opencode_id = "ses_7f3a9c2e1b"
    opencode_import = store.create_conversation(title="Imported from OpenCode")
    store.set_external_session_id(opencode_import.id, opencode_id)
    store.update_conversation(opencode_import.id, archived=True)
    permissions.grant(_ALICE, opencode_import.id, _LEVEL_OWNER)
    assert _archived_ids(store, opencode_id) == {opencode_import.id}

    # Restoring it moves it from the archived search to the active one.
    store.update_conversation(alice_import.id, archived=False)
    assert _archived_ids(store, harness_session_id) == set()
    active = store.list_conversations(search_query=harness_session_id, accessible_by=_ALICE)
    assert [conversation.id for conversation in active.data] == [alice_import.id]
