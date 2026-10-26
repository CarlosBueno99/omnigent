"""Persistent store for the pull requests linked to each session.

Hosts report the PRs they observe and the states they fetch; this store
keeps them so the Pull Requests tab can list a session's PRs while its host is
offline. Re-reporting a host's registry converges on the same rows, and a poll
that changes nothing does not write. Unlinking deletes the PR and records an
unlink that ignores later reports until the host stops reporting the PR.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlSessionPullRequest,
    SqlSessionPullRequestUnlink,
    current_workspace_id,
    pull_request_url_cksum,
)
from omnigent.db.enum_codecs import (
    SESSION_PR_RELATIONSHIP,
    SESSION_PR_STATE,
    decode_session_pr_relationship,
    decode_session_pr_state,
    encode_session_pr_relationship,
    encode_session_pr_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)

Relationship = Literal["created", "worked_on", "attached", "inferred"]

MAX_PRS_PER_SESSION = 200
MAX_UNLINKS_PER_SESSION = 100

# A stronger relationship survives any later report; "created" is never downgraded.
_RELATIONSHIP_RANK: dict[str, int] = {"inferred": 0, "worked_on": 1, "attached": 2, "created": 3}
_INFERRED = SESSION_PR_RELATIONSHIP["inferred"]
_TITLE_MAX = 1024
_MINUTE_US = 60_000_000
# Hosts re-report the branch PR on every poll; coarser steps keep those polls read-only.
_SEEN_STEP_US = _MINUTE_US
_STATE_REFRESH_US = _MINUTE_US
# Longer than any proxied request, so a response begun before an unlink can't revive it.
_UNLINK_SETTLE_US = 10 * _MINUTE_US


class SessionPullRequestLimitError(Exception):
    """The session already links :data:`MAX_PRS_PER_SESSION` pull requests."""


@dataclass(frozen=True)
class PullRequestReport:
    """One PR as a host's registry describes it; times are epoch seconds.

    :ivar url: Canonical PR URL, as the host's git provider normalized it.
    :ivar provider: Git provider id, e.g. ``"github"``.
    :ivar host: Forge host, e.g. ``"github.com"``.
    :ivar repository: Base repository, e.g. ``"owner/repo"``.
    :ivar number: The PR number.
    :ivar relationship: How the PR joined the session.
    :ivar first_seen_at: When the PR was first observed.
    :ivar last_seen_at: When the PR was last observed.
    :ivar title: The host's latest title, or ``None`` when it has none.
    """

    url: str
    provider: str
    host: str
    repository: str
    number: int
    relationship: Relationship
    first_seen_at: float
    last_seen_at: float
    title: str | None = None


@dataclass(frozen=True)
class StoredPullRequest(PullRequestReport):
    """A linked PR with the last state its host fetched.

    :ivar state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``, or ``None`` until fetched.
    :ivar is_draft: Whether the PR was a draft at that fetch.
    :ivar state_checked_at: Epoch seconds of that fetch's request, or ``None``.
    """

    state: str | None = None
    is_draft: bool = False
    state_checked_at: float | None = None


@dataclass(frozen=True)
class PullRequestPage:
    """One page of a session's linked PRs in display order.

    :ivar entries: The PRs on this page.
    :ivar has_more: Whether PRs follow the last one.
    """

    entries: list[StoredPullRequest]
    has_more: bool


def _us(seconds: float) -> int:
    return int(seconds * 1_000_000)


def _seconds(micros: int | None) -> float | None:
    return None if micros is None else micros / 1_000_000


def _clean_title(title: str | None) -> str | None:
    return title.strip()[:_TITLE_MAX] if title and title.strip() else None


def _display_rank(relationship: int, first_seen_us: int, last_seen_us: int) -> int:
    """Explicit PRs newest first, then inferred PRs oldest first, in ascending order."""
    return first_seen_us if relationship == _INFERRED else -last_seen_us


def _to_entity(row: SqlSessionPullRequest) -> StoredPullRequest:
    return StoredPullRequest(
        url=row.url,
        provider=row.provider,
        host=row.host,
        repository=row.repository,
        number=row.number,
        relationship=cast(Relationship, decode_session_pr_relationship(row.relationship)),
        first_seen_at=row.first_seen_at / 1_000_000,
        last_seen_at=row.last_seen_at / 1_000_000,
        title=row.title,
        state=None if row.state is None else decode_session_pr_state(row.state),
        is_draft=row.is_draft,
        state_checked_at=_seconds(row.state_checked_at),
    )


def _changes(
    row: SqlSessionPullRequest, report: PullRequestReport, reported_us: int | None
) -> dict[str, Any]:
    """The column updates that fold ``report`` into ``row``; empty when nothing changes."""
    changes: dict[str, Any] = {}
    relationship = row.relationship
    if (
        _RELATIONSHIP_RANK[report.relationship]
        > _RELATIONSHIP_RANK[decode_session_pr_relationship(row.relationship)]
    ):
        relationship = changes["relationship"] = encode_session_pr_relationship(
            report.relationship
        )
    first_seen = min(row.first_seen_at, _us(report.first_seen_at))
    if first_seen != row.first_seen_at:
        changes["first_seen_at"] = first_seen
    last_seen = row.last_seen_at
    if _us(report.last_seen_at) >= row.last_seen_at + _SEEN_STEP_US:
        last_seen = changes["last_seen_at"] = _us(report.last_seen_at)
    rank = _display_rank(relationship, first_seen, last_seen)
    if rank != row.display_rank:
        changes["display_rank"] = rank
    title = _clean_title(report.title)
    # A response requested before the last full fetch carries an older title.
    fresh = (
        reported_us is None or row.state_checked_at is None or reported_us >= row.state_checked_at
    )
    if title is not None and title != row.title and fresh:
        changes["title"] = title
    return changes


def _new_row(conversation_id: str, report: PullRequestReport) -> SqlSessionPullRequest:
    relationship = encode_session_pr_relationship(report.relationship)
    first_seen, last_seen = _us(report.first_seen_at), _us(report.last_seen_at)
    return SqlSessionPullRequest(
        workspace_id=current_workspace_id(),
        conversation_id=conversation_id,
        url_cksum=pull_request_url_cksum(report.url),
        url=report.url,
        provider=report.provider,
        host=report.host,
        repository=report.repository,
        number=report.number,
        relationship=relationship,
        first_seen_at=first_seen,
        last_seen_at=last_seen,
        display_rank=_display_rank(relationship, first_seen, last_seen),
        title=_clean_title(report.title),
        is_draft=False,
    )


class SessionPullRequestStore:
    """SQLAlchemy-backed store of the pull requests linked to each session."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store", immediate=True
        )

    def _key(self, conversation_id: str, url: str) -> tuple[int, str, bytes]:
        return (current_workspace_id(), conversation_id, pull_request_url_cksum(url))

    def _session_links(self, conversation_id: str) -> Any:
        return select(SqlSessionPullRequest).where(
            SqlSessionPullRequest.workspace_id == current_workspace_id(),
            SqlSessionPullRequest.conversation_id == conversation_id,
        )

    def _count(self, session: Session, conversation_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(SqlSessionPullRequest)
            .where(
                SqlSessionPullRequest.workspace_id == current_workspace_id(),
                SqlSessionPullRequest.conversation_id == conversation_id,
            )
        )
        return int(session.execute(stmt).scalar_one())

    def list_page(
        self, conversation_id: str, *, limit: int, after_url: str | None = None
    ) -> PullRequestPage:
        """Return a page of the session's linked PRs in display order.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param limit: The most PRs to return.
        :param after_url: The last PR of the previous page, or ``None`` for the first page.
        :raises LookupError: ``after_url`` is not linked to the session.
        """
        stmt = self._session_links(conversation_id).order_by(
            SqlSessionPullRequest.display_rank, SqlSessionPullRequest.url_cksum
        )
        with self._session("list_session_pull_requests") as session:
            if after_url is not None:
                cursor = session.execute(
                    select(
                        SqlSessionPullRequest.display_rank, SqlSessionPullRequest.url_cksum
                    ).where(
                        SqlSessionPullRequest.workspace_id == current_workspace_id(),
                        SqlSessionPullRequest.conversation_id == conversation_id,
                        SqlSessionPullRequest.url_cksum == pull_request_url_cksum(after_url),
                    )
                ).one_or_none()
                if cursor is None:
                    raise LookupError(after_url)
                stmt = stmt.where(
                    tuple_(SqlSessionPullRequest.display_rank, SqlSessionPullRequest.url_cksum)
                    > tuple(cursor)
                )
            rows = session.execute(stmt.limit(limit + 1)).scalars().all()
            entries = [_to_entity(row) for row in rows[:limit]]
        return PullRequestPage(entries=entries, has_more=len(rows) > limit)

    def list_for_session(self, conversation_id: str) -> list[StoredPullRequest]:
        """Return all of the session's linked PRs in display order.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        return self.list_page(conversation_id, limit=MAX_PRS_PER_SESSION).entries

    def has_links(self, conversation_id: str) -> bool:
        """Return whether the session links any PR.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        stmt = self._session_links(conversation_id).with_only_columns(
            SqlSessionPullRequest.url_cksum
        )
        with self._session("check_session_pull_requests") as session:
            return session.execute(stmt.limit(1)).first() is not None

    def primary_for_sessions(
        self, conversation_ids: Sequence[str]
    ) -> dict[str, StoredPullRequest]:
        """Return each session's first linked PR in display order.

        :param conversation_ids: Sessions to summarize; ones without PRs are absent.
        """
        primaries: dict[str, StoredPullRequest] = {}
        if not conversation_ids:
            return primaries
        with self._session("list_primary_session_pull_requests") as session:
            for conversation_id in dict.fromkeys(conversation_ids):
                stmt = self._session_links(conversation_id).order_by(
                    SqlSessionPullRequest.display_rank, SqlSessionPullRequest.url_cksum
                )
                row = session.execute(stmt.limit(1)).scalars().first()
                if row is not None:
                    primaries[conversation_id] = _to_entity(row)
        return primaries

    def is_unlinked(self, conversation_id: str, url: str) -> bool:
        """Return whether the user unlinked ``url`` and the host may still report it.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: A PR URL.
        """
        with self._session("check_session_pull_request_unlink") as session:
            key = self._key(conversation_id, url)
            return session.get(SqlSessionPullRequestUnlink, key) is not None

    def pending_unlinks(self, conversation_id: str, *, limit: int) -> list[str]:
        """Return URLs the user unlinked that the host may still report.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param limit: The most URLs to return.
        """
        stmt = (
            select(SqlSessionPullRequestUnlink.url)
            .where(
                SqlSessionPullRequestUnlink.workspace_id == current_workspace_id(),
                SqlSessionPullRequestUnlink.conversation_id == conversation_id,
            )
            .order_by(SqlSessionPullRequestUnlink.url_cksum)
            .limit(limit)
        )
        with self._session("list_session_pull_request_unlinks") as session:
            return list(session.execute(stmt).scalars().all())

    def settle_unlinks(
        self,
        conversation_id: str,
        reported_urls: Sequence[str],
        *,
        requested_at: float,
        limit: int,
    ) -> int:
        """Forget pending unlinks the host no longer reports.

        Only unlinks made :data:`_UNLINK_SETTLE_US` before the request are
        forgotten, so a slower response begun before the unlink cannot revive it.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param reported_urls: Every PR URL in the host's response.
        :param requested_at: Epoch seconds the server sent that request.
        :param limit: How many pending unlinks the request carried, as in
            :meth:`pending_unlinks`.
        :returns: How many unlinks were forgotten.
        """
        reported = {pull_request_url_cksum(url) for url in reported_urls}
        cutoff = _us(requested_at) - _UNLINK_SETTLE_US
        stmt = (
            select(SqlSessionPullRequestUnlink.url_cksum, SqlSessionPullRequestUnlink.unlinked_at)
            .where(
                SqlSessionPullRequestUnlink.workspace_id == current_workspace_id(),
                SqlSessionPullRequestUnlink.conversation_id == conversation_id,
            )
            .order_by(SqlSessionPullRequestUnlink.url_cksum)
            .limit(limit)
        )
        with self._session("list_settled_session_pull_request_unlinks") as session:
            settled = [
                cksum
                for cksum, unlinked_at in session.execute(stmt).all()
                if cksum not in reported and unlinked_at <= cutoff
            ]
        if not settled:
            return 0

        def write(session: Session) -> int:
            result = session.execute(
                delete(SqlSessionPullRequestUnlink).where(
                    SqlSessionPullRequestUnlink.workspace_id == current_workspace_id(),
                    SqlSessionPullRequestUnlink.conversation_id == conversation_id,
                    SqlSessionPullRequestUnlink.url_cksum.in_(settled),
                    SqlSessionPullRequestUnlink.unlinked_at <= cutoff,
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

        return cast(
            int,
            run_write_transaction(
                self._session_immediate, "settle_session_pull_request_unlinks", write
            ),
        )

    def _write(self, operation: str, callback: Any) -> Any:
        """Run a write, replaying it once when a concurrent insert claimed the same row."""
        try:
            return run_write_transaction(self._session_immediate, operation, callback)
        except IntegrityError:
            return run_write_transaction(self._session_immediate, operation, callback)

    def record(
        self,
        conversation_id: str,
        reports: Sequence[PullRequestReport],
        *,
        requested_at: float | None = None,
    ) -> bool:
        """Merge reported PRs into the session; unlinked ones are ignored.

        A session already linking :data:`MAX_PRS_PER_SESSION` PRs gains no new ones.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param reports: PRs as the host's registry describes them.
        :param requested_at: Epoch seconds the server requested the report, or
            ``None`` when unknown. A report older than a PR's last full fetch
            keeps that fetch's title.
        :returns: Whether any row was added or changed.
        """
        unique = {pull_request_url_cksum(report.url): report for report in reports}
        if not unique:
            return False
        reported_us = None if requested_at is None else _us(requested_at)

        def merge(session: Session, *, lock: bool) -> bool:
            stmt = self._session_links(conversation_id).where(
                SqlSessionPullRequest.url_cksum.in_(unique)
            )
            rows = {
                row.url_cksum: row
                for row in session.execute(stmt.with_for_update() if lock else stmt).scalars()
            }
            unlinked = set(
                session.execute(
                    select(SqlSessionPullRequestUnlink.url_cksum).where(
                        SqlSessionPullRequestUnlink.workspace_id == current_workspace_id(),
                        SqlSessionPullRequestUnlink.conversation_id == conversation_id,
                        SqlSessionPullRequestUnlink.url_cksum.in_(unique),
                    )
                ).scalars()
            )
            new = [
                report
                for cksum, report in unique.items()
                if cksum not in rows and cksum not in unlinked
            ]
            if new:
                new = new[: max(0, MAX_PRS_PER_SESSION - self._count(session, conversation_id))]
            changed = bool(new)
            for cksum, row in rows.items():
                changes = _changes(row, unique[cksum], reported_us)
                changed = changed or bool(changes)
                if lock:
                    for column, value in changes.items():
                        setattr(row, column, value)
            if lock:
                session.add_all(_new_row(conversation_id, report) for report in new)
            return changed

        with self._session("check_session_pull_request_reports") as session:
            if not merge(session, lock=False):
                return False
        return cast(
            bool,
            self._write("record_session_pull_requests", lambda session: merge(session, lock=True)),
        )

    def attach(self, conversation_id: str, report: PullRequestReport) -> None:
        """Link a PR the user chose, clearing an earlier unlink.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param report: The PR as the host that verified it describes it.
        :raises SessionPullRequestLimitError: The session links too many PRs already.
        """
        attached = replace(report, relationship="attached")
        key = self._key(conversation_id, report.url)

        def write(session: Session) -> None:
            session.execute(
                delete(SqlSessionPullRequestUnlink).where(
                    SqlSessionPullRequestUnlink.workspace_id == key[0],
                    SqlSessionPullRequestUnlink.conversation_id == key[1],
                    SqlSessionPullRequestUnlink.url_cksum == key[2],
                )
            )
            row = session.get(SqlSessionPullRequest, key, with_for_update=True)
            if row is not None:
                for column, value in _changes(row, attached, None).items():
                    setattr(row, column, value)
                return
            if self._count(session, conversation_id) >= MAX_PRS_PER_SESSION:
                raise SessionPullRequestLimitError(
                    f"A session can link at most {MAX_PRS_PER_SESSION} pull requests"
                )
            session.add(_new_row(conversation_id, attached))

        self._write("attach_session_pull_request", write)

    def remove(self, conversation_id: str, url: str) -> bool:
        """Unlink a PR, remembering it so later reports cannot link it again.

        The oldest unlinks are forgotten beyond :data:`MAX_UNLINKS_PER_SESSION`.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :returns: Whether the PR was linked.
        """
        key = self._key(conversation_id, url)
        unlinked_at = now_epoch_us()

        def write(session: Session) -> bool:
            result = session.execute(
                delete(SqlSessionPullRequest).where(
                    SqlSessionPullRequest.workspace_id == key[0],
                    SqlSessionPullRequest.conversation_id == key[1],
                    SqlSessionPullRequest.url_cksum == key[2],
                )
            )
            unlink = session.get(SqlSessionPullRequestUnlink, key, with_for_update=True)
            if unlink is not None:
                unlink.unlinked_at = unlinked_at
            else:
                self._make_room_for_unlink(session, conversation_id)
                session.add(
                    SqlSessionPullRequestUnlink(
                        workspace_id=key[0],
                        conversation_id=conversation_id,
                        url_cksum=key[2],
                        url=url,
                        unlinked_at=unlinked_at,
                    )
                )
            return bool(getattr(result, "rowcount", 0))

        return cast(bool, self._write("unlink_session_pull_request", write))

    def _make_room_for_unlink(self, session: Session, conversation_id: str) -> None:
        oldest = (
            select(SqlSessionPullRequestUnlink.url_cksum)
            .where(
                SqlSessionPullRequestUnlink.workspace_id == current_workspace_id(),
                SqlSessionPullRequestUnlink.conversation_id == conversation_id,
            )
            .order_by(
                SqlSessionPullRequestUnlink.unlinked_at, SqlSessionPullRequestUnlink.url_cksum
            )
            .limit(MAX_UNLINKS_PER_SESSION + 1)
        )
        cksums = list(session.execute(oldest).scalars())
        excess = len(cksums) - MAX_UNLINKS_PER_SESSION + 1
        if excess > 0:
            session.execute(
                delete(SqlSessionPullRequestUnlink).where(
                    SqlSessionPullRequestUnlink.workspace_id == current_workspace_id(),
                    SqlSessionPullRequestUnlink.conversation_id == conversation_id,
                    SqlSessionPullRequestUnlink.url_cksum.in_(cksums[:excess]),
                )
            )

    def update_state(
        self,
        conversation_id: str,
        url: str,
        *,
        state: str,
        is_draft: bool,
        title: str | None,
        checked_at: float,
    ) -> bool:
        """Store the state a host fetched for a linked PR, and refresh its title.

        A fetch older than the stored one, an unknown state, or a PR that is
        not linked is ignored. An unchanged state is rewritten at most once a
        minute, just to advance the last sync time.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :param state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``.
        :param is_draft: Whether the PR is a draft.
        :param title: The fetched title, or ``None`` to keep the stored one.
        :param checked_at: Epoch seconds the server requested the fetch.
        :returns: Whether the row changed.
        """
        if state not in SESSION_PR_STATE:
            return False
        code, checked_us = encode_session_pr_state(state), _us(checked_at)
        fetched_title = _clean_title(title)
        key = self._key(conversation_id, url)

        def apply(row: SqlSessionPullRequest | None, *, write: bool) -> bool:
            if row is None:
                return False
            if row.state_checked_at is not None and row.state_checked_at > checked_us:
                return False
            changed = (row.state, row.is_draft) != (code, is_draft) or (
                fetched_title is not None and fetched_title != row.title
            )
            stale = row.state_checked_at is None or (
                checked_us - row.state_checked_at >= _STATE_REFRESH_US
            )
            if not (changed or stale):
                return False
            if write:
                row.state, row.is_draft, row.state_checked_at = code, is_draft, checked_us
                if fetched_title is not None:
                    row.title = fetched_title
            return True

        with self._session("check_session_pull_request_state") as session:
            if not apply(session.get(SqlSessionPullRequest, key), write=False):
                return False

        def write(session: Session) -> bool:
            row = session.get(SqlSessionPullRequest, key, with_for_update=True)
            return apply(row, write=True)

        return cast(
            bool,
            run_write_transaction(
                self._session_immediate, "update_session_pull_request_state", write
            ),
        )
