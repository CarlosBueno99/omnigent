"""Per-user portable settings shared by the user's connected hosts.

One ``preferences`` row per user holds the settings described in
:mod:`omnigent.config_sync` together with a revision that increases on every
change, so hosts can tell whether the copy they applied is current.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import LargeBinary, select, type_coerce
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Insert

from omnigent.config_sync import SyncedSettings, validate_synced_settings
from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.server.auth import RESERVED_USER_LOCAL

_logger = logging.getLogger(__name__)

_SYNCED_CONFIG_KEY = "synced_config"
_MAX_DECODED_BYTES = 64 * 1024


@dataclass(frozen=True)
class SyncedConfig:
    """The user's portable settings and the revision that produced them.

    :param settings: Validated settings, e.g. ``{"model": "gpt-5"}``.
    :param revision: ``0`` until the first save; increases on every change.
    """

    settings: SyncedSettings = field(default_factory=dict)
    revision: int = 0


def _decode_synced_config(raw: bytes | str | memoryview | None) -> SyncedConfig:
    """Read a stored row; an absent or unreadable row reads as revision 0."""
    if raw is None:
        return SyncedConfig()
    try:
        decoded = json.loads(decode(raw, max_decoded_bytes=_MAX_DECODED_BYTES) or "null")
        if not isinstance(decoded, dict):
            raise ValueError("synced config row must be a JSON object")
        revision = decoded.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("synced config revision must be a non-negative int")
        return SyncedConfig(validate_synced_settings(decoded.get("settings")), revision)
    except Exception as exc:  # noqa: BLE001 — a corrupt row must not break host connects
        _logger.warning("Ignoring unreadable synced config row: %s", exc)
        return SyncedConfig()


def _encode_synced_config(config: SyncedConfig) -> str:
    return json.dumps(
        {"revision": config.revision, "settings": config.settings}, separators=(",", ":")
    )


def _row_query(user_id: str, workspace_id: int) -> Select[tuple[bytes]]:
    # Decode raw bytes in Python so a malformed value cannot fail in the ORM result processor.
    return select(type_coerce(SqlPreference.value, LargeBinary)).where(
        SqlPreference.workspace_id == workspace_id,
        SqlPreference.user_id == user_id,
        SqlPreference.key == _SYNCED_CONFIG_KEY,
    )


class UserConfigStore:
    """Read and replace a user's synced settings."""

    def __init__(self, storage_location: str) -> None:
        """
        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///chat.db"``.
        """
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.user_config_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.user_config_store", immediate=True
        )

    def get(self, user_id: str | None) -> SyncedConfig:
        """Return the user's synced settings in the current workspace.

        :param user_id: Owner of the settings, or ``None`` on a single-user server.
        :returns: The stored settings, or revision 0 with no settings.
        """
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id
        with self._session("read_synced_config") as session:
            raw = session.scalar(_row_query(preference_user_id, current_workspace_id()))
        return _decode_synced_config(raw)

    def save(self, user_id: str | None, settings: SyncedSettings) -> SyncedConfig:
        """Store *settings* as the next revision; identical settings keep the current one.

        :param user_id: Owner of the settings, or ``None`` on a single-user server.
        :param settings: Validated settings (see
            :func:`omnigent.config_sync.validate_synced_settings`).
        :returns: The stored settings and their revision.
        """
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> SyncedConfig:
            require_active_account(session, user_id)
            workspace_id = current_workspace_id()
            raw = session.scalar(_row_query(preference_user_id, workspace_id).with_for_update())
            current = _decode_synced_config(raw)
            if current.settings == settings:
                return current
            updated = SyncedConfig(dict(settings), current.revision + 1)
            encoded = _encode_synced_config(updated)
            values = {
                "workspace_id": workspace_id,
                "user_id": preference_user_id,
                "key": _SYNCED_CONFIG_KEY,
                "value": encoded,
            }
            dialect = self._engine.dialect.name
            stmt: Insert
            if dialect == "mysql":
                stmt = (
                    mysql_insert(SqlPreference)
                    .values(**values)
                    .on_duplicate_key_update(value=encoded)
                )
            elif dialect == "sqlite":
                stmt = (
                    sqlite_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": encoded},
                    )
                )
            else:
                stmt = (
                    pg_insert(SqlPreference)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["workspace_id", "user_id", "key"],
                        set_={"value": encoded},
                    )
                )
            session.execute(stmt)
            return updated

        return run_write_transaction(self._session_immediate, "save_synced_config", write)


__all__ = ["SyncedConfig", "UserConfigStore"]
