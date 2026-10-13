"""Keep this host's portable user settings in step with the server.

The host reads its own ``config.yaml`` and the ``config-sync.json`` sidecar
beside it, which records, per server, the revision and settings last exchanged
with it; daemons for different servers can share one config home. See
:mod:`omnigent.config_sync` for which keys travel and how the server reconciles
them.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from omnigent.config import (
    global_config_lock,
    global_config_path,
    load_global_config,
    write_global_config,
)
from omnigent.config_sync import (
    SyncedSettings,
    apply_synced_settings,
    synced_settings,
    validate_synced_settings,
)
from omnigent.host.frames import HostConfigSyncFrame, HostConfigUpdateFrame

_logger = logging.getLogger(__name__)

CONFIG_SYNC_STATE_FILENAME = "config-sync.json"


@dataclass(frozen=True)
class ConfigSyncState:
    """What this host last synced with one server.

    :param server_url: The server the revision belongs to.
    :param revision: Revision applied locally; ``0`` when the server held nothing.
    :param settings: The portable settings as of that revision.
    """

    server_url: str
    revision: int
    settings: SyncedSettings


def sync_state_path(config_path: Path | None = None) -> Path:
    """Return the sidecar beside ``config.yaml`` that records the last sync."""
    return (config_path or global_config_path()).with_name(CONFIG_SYNC_STATE_FILENAME)


def load_sync_state(server_url: str, config_path: Path | None = None) -> ConfigSyncState | None:
    """Read the last sync with *server_url*.

    :returns: ``None`` when the sidecar is absent, unreadable, or records
        another server.
    """
    path = sync_state_path(config_path)
    servers = _read_servers(path)
    entry = servers.get(server_url)
    if not isinstance(entry, dict):
        return None
    try:
        revision = entry.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("revision must be a non-negative int")
        state = ConfigSyncState(
            server_url, revision, validate_synced_settings(entry.get("settings"))
        )
    except ValueError as exc:
        _warn_unreadable(path, exc)
        return None
    _UNREADABLE_STATE_WARNED.pop(path, None)
    return state


def _read_servers(path: Path) -> dict[str, object]:
    """Return the sidecar's per-server entries; unreadable content reads as none."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _warn_unreadable(path, exc)
        return {}
    servers = raw.get("servers") if isinstance(raw, dict) else None
    return dict(servers) if isinstance(servers, dict) else {}


def _warn_unreadable(path: Path, exc: Exception) -> None:
    cause = f"{type(exc).__name__}: {exc}"
    if _UNREADABLE_STATE_WARNED.get(path) != cause:
        _UNREADABLE_STATE_WARNED[path] = cause
        _logger.warning("Ignoring unreadable %s: %s", path, exc)
        return None


def save_sync_state(state: ConfigSyncState, config_path: Path | None = None) -> None:
    """Record *state* for its server in the sidecar, keeping other servers' entries.

    Callers hold :func:`omnigent.config.global_config_lock`, which also covers
    this read-modify-write.
    """
    path = sync_state_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    servers = _read_servers(path)
    servers[state.server_url] = {"revision": state.revision, "settings": state.settings}
    payload = {"servers": servers}
    handle, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def build_sync_frame(server_url: str, config_path: Path | None = None) -> HostConfigSyncFrame:
    """Describe this host's portable settings relative to its last sync with *server_url*.

    A host that never synced reports revision 0 and counts any local settings
    as a change, so an empty server adopts them.
    """
    local = synced_settings(load_global_config(config_path))
    state = load_sync_state(server_url, config_path)
    if state is None:
        return HostConfigSyncFrame(revision=0, settings=local, changed=bool(local))
    return HostConfigSyncFrame(
        revision=state.revision, settings=local, changed=local != state.settings
    )


def apply_config_update(
    frame: HostConfigUpdateFrame, server_url: str, config_path: Path | None = None
) -> bool:
    """Write the server's settings into the local config and record the revision.

    A local edit made since the last sync that the server has not adopted yet is
    kept, so neither an acknowledgement of an earlier report nor a peer's update
    can erase it; the next report carries it instead. Callers serialize this
    with their own reads of ``config.yaml``; the file is read once and the
    merged result written back atomically.

    :returns: Whether ``config.yaml`` changed.
    :raises ValueError: If the frame's settings are malformed.
    """
    settings = validate_synced_settings(frame.settings)
    path = config_path or global_config_path()
    with global_config_lock(path):
        cfg = load_global_config(path)
        previous = load_sync_state(server_url, path)
        state = ConfigSyncState(server_url, frame.revision, settings)
        pending_local_edit = (
            previous is not None
            and synced_settings(cfg) != previous.settings
            and settings != synced_settings(cfg)
        )
        changed = False
        if not pending_local_edit:
            merged = apply_synced_settings(cfg, settings)
            changed = merged != cfg
            if changed:
                write_global_config(merged, path)
        if previous != state:
            save_sync_state(state, path)
    return changed


__all__ = [
    "CONFIG_SYNC_STATE_FILENAME",
    "ConfigSyncState",
    "apply_config_update",
    "build_sync_frame",
    "load_sync_state",
    "save_sync_state",
    "sync_state_path",
]
