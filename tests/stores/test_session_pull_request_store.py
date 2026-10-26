"""Tests for :class:`SessionPullRequestStore` against a migrated SQLite database."""

from __future__ import annotations

import pytest

from omnigent.stores import session_pull_request_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_pull_request_store import (
    PullRequestReport,
    SessionPullRequestLimitError,
    SessionPullRequestStore,
)

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER = "5d29bee4350489d66feafecfebd94a97"
A = "https://github.com/example/one/pull/42"
B = "https://github.com/example/two/pull/7"
MINUTE = 60.0


def _report(url: str, **overrides: object) -> PullRequestReport:
    repository, number = url.removeprefix("https://github.com/").split("/pull/")
    fields: dict[str, object] = {
        "url": url,
        "provider": "github",
        "host": "github.com",
        "repository": repository,
        "number": int(number),
        "relationship": "worked_on",
        "first_seen_at": 100.0,
        "last_seen_at": 100.0,
    }
    return PullRequestReport(**{**fields, **overrides})  # type: ignore[arg-type]


def _urls(store: SessionPullRequestStore, session: str = SESSION) -> list[str]:
    return [entry.url for entry in store.list_for_session(session)]


@pytest.fixture()
def store(db_uri: str) -> SessionPullRequestStore:
    return SessionPullRequestStore(db_uri)


def _forbid_writes(store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def write_session(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("opened a write transaction")

    monkeypatch.setattr(store, "_session_immediate", write_session)


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_reports_and_fetches_converge_in_either_order(
    store: SessionPullRequestStore, reverse: bool
) -> None:
    """Strongest relationship, widest seen window, and the newest fetch's state and title."""
    reports = [
        _report(A, relationship="created", first_seen_at=50.0, last_seen_at=60.0),
        _report(A, relationship="inferred", first_seen_at=70.0, last_seen_at=60.0 + 2 * MINUTE),
    ]
    fetches = [
        {"state": "OPEN", "is_draft": True, "title": "Draft", "checked_at": 1_000.0},
        {"state": "MERGED", "is_draft": False, "title": "Shipped", "checked_at": 2_000.0},
    ]
    for report, fetch in zip(
        reversed(reports) if reverse else reports,
        reversed(fetches) if reverse else fetches,
        strict=True,
    ):
        store.record(SESSION, [report])
        store.update_state(SESSION, A, **fetch)  # type: ignore[arg-type]

    [entry] = store.list_for_session(SESSION)
    assert entry.relationship == "created"
    assert (entry.first_seen_at, entry.last_seen_at) == (50.0, 60.0 + 2 * MINUTE)
    assert (entry.state, entry.is_draft, entry.title) == ("MERGED", False, "Shipped")
    assert entry.state_checked_at == 2_000.0
    # A registry title from a request made before that fetch is older than it.
    store.record(SESSION, [_report(A, title="Old")], requested_at=1_500.0)
    assert store.list_for_session(SESSION)[0].title == "Shipped"


def test_repeated_reports_and_fetches_do_not_write(
    store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.record(SESSION, [_report(A, title="Title")])
    store.update_state(SESSION, A, state="OPEN", is_draft=False, title="Title", checked_at=1_000.0)
    _forbid_writes(store, monkeypatch)

    # Seen again within a minute, and fetched again unchanged within a minute.
    assert not store.record(SESSION, [_report(A, title="Title", last_seen_at=130.0)])
    assert not store.update_state(
        SESSION, A, state="OPEN", is_draft=False, title="Title", checked_at=1_030.0
    )


def test_titles_are_trimmed_capped_and_kept_when_a_lookup_fails(
    store: SessionPullRequestStore,
) -> None:
    store.record(SESSION, [_report(A, title="  First  ")])
    assert store.list_for_session(SESSION)[0].title == "First"
    store.record(SESSION, [_report(A, title="x" * 2000)])
    assert store.list_for_session(SESSION)[0].title == "x" * 1024
    # A host whose title lookup failed reports no title.
    assert not store.record(SESSION, [_report(A, title=None)])
    assert store.list_for_session(SESSION)[0].title == "x" * 1024


def test_unlink_ignores_reports_until_the_user_links_it_again(
    store: SessionPullRequestStore,
) -> None:
    store.record(SESSION, [_report(A), _report(B)])
    assert store.remove(SESSION, A)
    assert not store.remove(SESSION, A)
    assert _urls(store) == [B]
    assert store.is_unlinked(SESSION, A) and not store.is_unlinked(SESSION, B)
    assert store.pending_unlinks(SESSION, limit=20) == [A]

    # A replayed observation or fetch must not undo the user's unlink.
    assert not store.record(SESSION, [_report(A, relationship="created", last_seen_at=500.0)])
    assert not store.update_state(
        SESSION, A, state="OPEN", is_draft=False, title=None, checked_at=1.0
    )
    assert _urls(store) == [B]

    store.attach(SESSION, _report(A, last_seen_at=600.0))
    assert store.pending_unlinks(SESSION, limit=20) == []
    assert [(entry.url, entry.relationship) for entry in store.list_for_session(SESSION)] == [
        (A, "attached"),
        (B, "worked_on"),
    ]


def test_unlinks_settle_once_the_host_stops_reporting_them(
    store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_pull_request_store, "now_epoch_us", lambda: 1_000_000_000)
    store.remove(SESSION, A)
    store.remove(SESSION, B)
    settled_after = 1_000.0 + 10 * MINUTE

    # A request sent soon after the unlink may have started before it.
    assert store.settle_unlinks(SESSION, [], requested_at=settled_after - 1, limit=20) == 0
    # The host still reports B, so only A is settled.
    assert store.settle_unlinks(SESSION, [B], requested_at=settled_after, limit=20) == 1
    assert store.pending_unlinks(SESSION, limit=20) == [B]


def test_sessions_cap_links_and_forget_their_oldest_unlinks(
    store: SessionPullRequestStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_pull_request_store, "MAX_PRS_PER_SESSION", 2)
    monkeypatch.setattr(session_pull_request_store, "MAX_UNLINKS_PER_SESSION", 2)
    third = "https://github.com/example/three/pull/1"
    store.record(SESSION, [_report(A), _report(B), _report(third)])
    assert len(_urls(store)) == 2
    with pytest.raises(SessionPullRequestLimitError):
        store.attach(SESSION, _report("https://github.com/example/four/pull/1"))

    clock = iter(range(1_000_000, 10_000_000, 1_000_000))
    monkeypatch.setattr(session_pull_request_store, "now_epoch_us", lambda: next(clock))
    for url in (A, B, third):
        store.remove(SESSION, url)
    assert sorted(store.pending_unlinks(SESSION, limit=20)) == sorted([B, third])


def test_display_order_pages_and_primary_pr(store: SessionPullRequestStore) -> None:
    """Explicit PRs come first, most recent first; inferred ones follow, oldest first."""
    inferred_old = "https://github.com/example/three/pull/1"
    inferred_new = "https://github.com/example/three/pull/2"
    store.record(
        SESSION,
        [
            _report(inferred_new, relationship="inferred", first_seen_at=300.0),
            _report(inferred_old, relationship="inferred", first_seen_at=200.0),
            _report(A, last_seen_at=100.0),
            _report(B, last_seen_at=400.0),
        ],
    )
    store.record(OTHER, [_report(A, relationship="inferred")])
    assert _urls(store) == [B, A, inferred_old, inferred_new]

    first = store.list_page(SESSION, limit=3)
    assert ([entry.url for entry in first.entries], first.has_more) == ([B, A, inferred_old], True)
    rest = store.list_page(SESSION, limit=3, after_url=inferred_old)
    assert ([entry.url for entry in rest.entries], rest.has_more) == ([inferred_new], False)
    with pytest.raises(LookupError):
        store.list_page(SESSION, limit=3, after_url="https://github.com/example/none/pull/1")

    primary = store.primary_for_sessions([SESSION, OTHER, "f" * 32])
    assert {session: entry.url for session, entry in primary.items()} == {SESSION: B, OTHER: A}
    assert store.has_links(SESSION) and not store.has_links("f" * 32)

    # A PR first inferred, then worked on, moves among the explicit PRs.
    store.record(SESSION, [_report(inferred_new, relationship="worked_on", last_seen_at=500.0)])
    assert _urls(store)[0] == inferred_new
    store.remove(SESSION, inferred_new)
    store.remove(SESSION, B)
    assert store.primary_for_sessions([SESSION])[SESSION].url == A


async def test_deleting_the_session_deletes_its_pull_requests(db_uri: str) -> None:
    conversation_store = SqlAlchemyConversationStore(db_uri)
    store = SessionPullRequestStore(db_uri)
    conv = conversation_store.create_conversation()
    store.record(conv.id, [_report(A)])
    store.remove(conv.id, B)
    store.record(OTHER, [_report(A)])

    assert await conversation_store.delete_conversation(conv.id)
    assert store.list_for_session(conv.id) == []
    assert store.pending_unlinks(conv.id, limit=20) == []
    assert _urls(store, OTHER) == [A]
