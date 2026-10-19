"""Merge host observations without overriding explicit session link choices."""

from __future__ import annotations

import time
from typing import Any

from omnigent.runner.session_prs import PullRequestRef, SessionPullRequest


def update_pull_requests(
    state: dict[str, Any],
    *,
    url: str | None = None,
    action: str = "import",
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a manual choice or import a host snapshot; removals survive replay."""
    entries = {entry["url"]: dict(entry) for entry in state.get("prs", [])}
    excluded = set(state.get("excluded", []))
    manual = set(state.get("manual", []))
    if action == "import":
        for value in (snapshot or {}).get("excluded", []):
            value = PullRequestRef.from_url(value).url
            if value not in manual:
                excluded.add(value)
                entries.pop(value, None)
        for raw in (snapshot or {}).get("prs", []):
            entry = SessionPullRequest.model_validate(raw)
            entry = entry.model_copy(update=PullRequestRef.from_url(entry.url).model_dump())
            key = entry.url
            if key in excluded:
                continue
            existing = entries.get(key, {})
            if existing:
                entry = SessionPullRequest.model_validate(existing).merge(entry)
            entries[key] = {**existing, **entry.model_dump()}
        pr = (snapshot or {}).get("pr")
        if isinstance(pr, dict) and pr.get("url") in entries:
            # Detail responses have no observation timestamp; don't overwrite a
            # newer cached title with a delayed provider response.
            if not entries[pr["url"]].get("title"):
                entries[pr["url"]]["title"] = pr.get("title")
            entries[pr["url"]]["state"] = pr.get("state")
    elif action in {"attach", "remove"} and url:
        reference = PullRequestRef.from_url(url)
        key = reference.url
        manual.add(key)
        if action == "remove":
            excluded.add(key)
            entries.pop(key, None)
        else:
            excluded.discard(key)
            entries[key] = {
                **SessionPullRequest(
                    **reference.model_dump(),
                    relationship="attached",
                    source="ui",
                    first_seen_at=time.time(),
                    last_seen_at=time.time(),
                ).model_dump(),
                **entries.get(key, {}),
            }
    else:
        raise ValueError("Expected attach or remove")
    return {"prs": list(entries.values()), "excluded": sorted(excluded), "manual": sorted(manual)}
