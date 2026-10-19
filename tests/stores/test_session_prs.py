import pytest

from omnigent.stores.conversation_store.session_prs import validate_session_prs

PR = {
    "provider": "github",
    "host": "github.com",
    "repository": "example/project",
    "number": 42,
    "url": "https://github.com/example/project/pull/42",
    "relationship": "created",
    "source": "native_hook",
    "first_seen_at": 1.0,
    "last_seen_at": 2.0,
    "title": "Saved change",
}


def test_pr_metadata_preserves_other_state_and_rejects_stale_reports(conversation_store):
    store = conversation_store
    conversation = store.create_conversation()
    assert conversation.session_pull_requests is None
    todos = [{"content": "Keep", "status": "pending", "activeForm": "Keeping"}]
    assert store.set_session_prs(conversation.id, [PR], updated_at_ns=10)
    store.set_session_todos(conversation.id, todos)
    store.set_session_state(conversation.id, {"count": 2, "_omnigent_session_prs_v1": []})
    saved = store.get_conversation(conversation.id)
    assert saved.session_pull_requests == [PR]
    assert saved.session_pull_requests_updated_at_ns == 10
    assert saved.session_state == {"count": 2}
    assert saved.session_todos == todos
    assert store.fork_conversation(conversation.id).session_pull_requests is None
    assert store.set_session_prs(conversation.id, [], updated_at_ns=11)
    assert not store.set_session_prs(conversation.id, [PR], updated_at_ns=10)
    assert not store.set_session_prs(conversation.id, [PR], updated_at_ns=11)
    assert not store.set_session_prs(conversation.id, [PR])
    assert store.get_conversation(conversation.id).session_pull_requests == []
    assert store.get_conversation(conversation.id).session_todos == todos
    assert store.set_session_prs(conversation.id, [PR], user_update=True)
    assert not store.set_session_prs(conversation.id, [], updated_at_ns=10)
    assert store.get_conversation(conversation.id).session_pull_requests == [PR]
    assert store.get_conversation(conversation.id).session_pull_requests_updated_at_ns == 11
    with pytest.raises(ValueError):
        store.set_session_prs(conversation.id, [PR], updated_at_ns=True)


def test_legacy_pr_report_only_backfills_missing_metadata(conversation_store):
    conversation = conversation_store.create_conversation()
    assert conversation_store.set_session_prs(conversation.id, [PR])
    assert not conversation_store.set_session_prs(conversation.id, [])
    assert conversation_store.set_session_prs(conversation.id, [], updated_at_ns=1)
    assert conversation_store.get_conversation(conversation.id).session_pull_requests == []


def test_pr_snapshot_validation_strips_private_fields_and_supports_private_hosts():
    private = {
        **PR,
        "host": "git.internal.test",
        "url": "https://git.internal.test/group/project/-/merge_requests/42",
    }
    assert validate_session_prs([{**private, "auth": {"token": "private"}}, private]) == [private]
    for changes in (
        {"url": "javascript:alert(1)"},
        {"url": "https://attacker.test/pull/42"},
        {"url": "https://user:secret@github.com/example/project/pull/42"},
        {"url": PR["url"] + "?token=secret"},
        {"url": PR["url"] + "\n"},
        {"number": True},
        {"last_seen_at": float("inf")},
        {"title": "x" * 4097},
    ):
        with pytest.raises(ValueError):
            validate_session_prs([{**PR, **changes}])
    for value in (None, {}, [PR] * 101, [{**PR, "title": "字" * 4096}] * 20):
        with pytest.raises(ValueError):
            validate_session_prs(value)
