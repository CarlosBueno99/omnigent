"""GitHub writes using one pinned credential and an explicitly confirmed actor."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.runner import github_resource as github
from omnigent.runner.pr_resource import _selected_pr
from omnigent.runner.session_prs import PullRequestRef


class PullRequestEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_url: str = Field(max_length=4096)
    expected_actor: str = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    body: str | None = Field(default=None, max_length=65536)
    reviewer: str | None = Field(default=None, min_length=1, max_length=39)

    @model_validator(mode="after")
    def validate_changes(self) -> PullRequestEdit:
        if self.title is not None and not self.title.strip():
            raise ValueError("A PR title cannot be blank")
        if self.reviewer is not None:
            if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", self.reviewer):
                raise ValueError("Enter an exact GitHub username")
            if self.title is not None or self.body is not None:
                raise ValueError("Request reviewers separately from metadata edits")
        elif self.title is None and self.body is None:
            raise ValueError("No PR changes supplied")
        return self


def _api(root: str, ref: PullRequestRef, token: str, endpoint: str) -> dict[str, Any]:
    rc, out, _ = github._gh(
        ["api", *github._host_args(root, ref), endpoint], cwd=root, token=token
    )
    if rc != 0:
        raise ValueError(
            "Could not check GitHub access. "
            "Check host sign-in and repository permissions, then retry."
        )
    try:
        result = json.loads(out)
    except ValueError:
        raise ValueError("GitHub returned an invalid access response") from None
    if not isinstance(result, dict):
        raise ValueError("GitHub returned an invalid access response")
    return result


def _access(root: str, session_id: str, pr_url: str) -> tuple[PullRequestRef, str, dict[str, Any]]:
    ref = _selected_pr(session_id, pr_url)
    if ref.provider != "github":
        raise ValueError("PR editing is currently supported for GitHub only")
    github._host_args(root, ref)
    token = github._pr_token(root, ref)
    if (
        not token
        and not github._in_sandbox()
        and github._config.github_account_preference(github._repo_argument(ref))
    ):
        raise ValueError(
            "The selected GitHub account is unavailable. "
            "Refresh the account selection before editing."
        )
    if not token:
        rc, token, _ = github._gh(["auth", "token", "--hostname", ref.host], cwd=root)
        if rc != 0 or not token.strip():
            raise ValueError("Sign in with gh on the host before editing PRs")
        token = token.strip()
    actor = _api(root, ref, token, "user").get("login")
    if not isinstance(actor, str) or not actor:
        raise ValueError("GitHub did not identify the acting account")
    repo = _api(root, ref, token, f"repos/{ref.repository}")
    pr = _api(root, ref, token, f"repos/{ref.repository}/pulls/{ref.number}")
    permissions = repo.get("permissions") or {}
    author = pr.get("user") or {}
    if not isinstance(permissions, dict) or not isinstance(author, dict):
        raise ValueError("GitHub returned an invalid access response")
    allowed = (
        any(permissions.get(key) is True for key in ("push", "maintain", "admin"))
        or author.get("login") == actor
    )
    return (
        ref,
        token,
        {
            "actor": actor,
            "can_edit": allowed,
            "can_request_reviewers": allowed and pr.get("state") == "open",
        },
    )


def pr_write_access(root: str, *, session_id: str, pr_url: str) -> dict[str, Any]:
    """Report the host identity and conservative provider write capabilities."""
    return _access(root, session_id, pr_url)[2]


def edit_pr(root: str, *, session_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply exactly the supplied fields; never retry an ambiguous provider write."""
    edit = PullRequestEdit.model_validate(changes)
    ref, token, access = _access(root, session_id, edit.pr_url)
    if edit.expected_actor != access["actor"]:
        raise ValueError("The GitHub account changed. Refresh access before saving.")
    capability = "can_request_reviewers" if edit.reviewer else "can_edit"
    if not access[capability]:
        raise ValueError("This GitHub account cannot perform that action on the PR")
    endpoint = f"repos/{ref.repository}/pulls/{ref.number}"
    args = ["api", *github._host_args(root, ref)]
    if edit.reviewer:
        args += [
            f"{endpoint}/requested_reviewers",
            "--method",
            "POST",
            "-f",
            f"reviewers[]={edit.reviewer}",
        ]
    else:
        args += [endpoint, "--method", "PATCH"]
        for field in ("title", "body"):
            value = getattr(edit, field)
            if value is not None:
                args += ["-f", f"{field}={value}"]
    rc, _, _ = github._gh(args, cwd=root, token=token)
    if rc != 0:
        raise ValueError(
            "GitHub could not confirm the change. Refresh the PR before retrying; "
            "check permissions and the reviewer username."
        )
    return {"updated": True, "actor": access["actor"]}
