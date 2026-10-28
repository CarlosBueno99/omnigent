"""Display text for a Codex ``fileChange`` item mirrored as an ``apply_patch`` call."""

from __future__ import annotations

from collections.abc import Mapping

_UNAPPLIED_STATUSES = frozenset({"failed", "declined"})
# Must match the web transcript's check for an applied Codex patch.
_APPLIED_LINE = "File change applied."


def file_change_failure(item: Mapping[str, object]) -> str | None:
    """Describe a file change Codex did not apply.

    :param item: Codex ``fileChange`` item, e.g. ``{"status": "declined", ...}``.
    :returns: ``"File change ended with status declined."`` for a failed or
        declined change, otherwise ``None``.
    """
    status = item.get("status")
    if status in _UNAPPLIED_STATUSES:
        return f"File change ended with status {status}."
    return None


def file_change_output(item: Mapping[str, object]) -> str:
    """One ``<kind> <path>`` line per change, led by the change's outcome.

    The lead line lets the transcript tell an applied patch, which shows line
    counts, from a failed one; output without it is ambiguous.

    :param item: Codex ``fileChange`` item, e.g. ``{"changes": [{"path":
        "/repo/x.py", "kind": {"type": "update"}, "diff": "..."}],
        "status": "completed"}``.
    :returns: e.g. ``"File change applied.\\nupdate /repo/x.py"``, or
        ``"Error: File change ended with status declined.\\nupdate /repo/x.py"``.
    """
    lines: list[str] = []
    failure = file_change_failure(item)
    if failure is not None:
        lines.append(f"Error: {failure}")
    elif item.get("status") == "completed":
        lines.append(_APPLIED_LINE)
    changes = item.get("changes")
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        if not isinstance(path, str):
            continue
        kind = change.get("kind")
        kind_type = kind.get("type") if isinstance(kind, dict) else None
        label = kind_type if isinstance(kind_type, str) and kind_type else "change"
        lines.append(f"{label} {path}")
    return "\n".join(lines)
