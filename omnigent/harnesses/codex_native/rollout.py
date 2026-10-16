"""Read-only access to Codex rollout JSONL files."""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path

# A rollout stem is a single path component; anything else cannot name a thread.
_THREAD_ID_STEM_RE = re.compile(r"[A-Za-z0-9._-]+")


def find_thread_rollout(codex_home: Path, thread_id: str) -> Path | None:
    """
    Find a Codex thread's rollout file within a ``CODEX_HOME``.

    Codex persists each thread's history as a single append-only JSONL
    rollout at ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ISO-ts>-<thread_id>.jsonl``,
    where the trailing ``<thread_id>`` matches the thread's ``session_meta.id``.

    :param codex_home: A per-session private ``CODEX_HOME``, e.g.
        ``Path("~/.omnigent/codex-native/<hash>/codex-home")``.
    :param thread_id: Codex thread id / rollout stem, e.g.
        ``"019e96aa-0be2-7343-8d3b-6f914d60936b"``. Matched literally; an id
        that is not a single path component matches nothing.
    :returns: Path to the most recently modified matching rollout, or
        ``None`` when none exists on this host.
    :raises OSError: If a candidate rollout cannot be inspected.
    """
    sessions = codex_home / "sessions"
    if not _THREAD_ID_STEM_RE.fullmatch(thread_id) or not sessions.is_dir():
        return None
    pattern = f"**/rollout-*-{glob.escape(thread_id)}.jsonl"
    matches = [p for p in sessions.glob(pattern) if p.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def last_turn_context_model(rollout_path: Path) -> str | None:
    """
    Return the ``model`` of the newest ``turn_context`` record in a rollout.

    Codex records a turn's context after any pre-sampling compaction has
    succeeded, so this is the model of the last turn that reached sampling:
    the model Codex compacts on before the first turn on a different one, and
    the ``previous`` model it reports when the thread is resumed.

    :param rollout_path: Path to the rollout JSONL.
    :returns: The newest recorded turn model, e.g. ``"gpt-5.5"``, or ``None``
        when no ``turn_context`` names one.
    :raises OSError: If the rollout cannot be read.
    """
    model: str | None = None
    with rollout_path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if '"turn_context"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "turn_context":
                continue
            payload = record.get("payload")
            candidate = payload.get("model") if isinstance(payload, dict) else None
            if isinstance(candidate, str) and candidate:
                model = candidate
    return model
