# Pull request management

Sessions can retain several PR links independently of their workspace or host.
The default-off `pull_request_management` flag adds server-owned associations,
patch navigation, viewed files, and GitHub metadata edits and reviewer requests.

## Sub-features

- `links`: paste URLs, select a linked PR, and unlink without changing the upstream PR.
- `recovery`: distinguish failed reads from empty results and retry with Refresh.
- `review`: filter paths, find text in the fetched patch, copy paths, open the
  displayed revision, and retain viewed state per user, PR and head revision.
- `writes`: edit title or description with local draft recovery and request a
  reviewer by exact username, showing the GitHub account that will act.

## How to get to it (user POV)

- `desktop-rail`: open Workspace, then Pull Requests, including workspace-free sessions.
- `desktop-composer`: click the PR number/count beside the composer.
- `mobile-menu`: choose Pull Requests from the session's actions menu.
- `mobile-composer`: tap the PR number/count to open the full-screen drawer.
- `sidebar`: hover a session to inspect its linked PR count.
- `canvas`: enable Canvas and inspect the PR link/count on the session card.

## Driving it with the repro environment

Preconditions: use the [Verify Omnigent skill](skills/verify-omnigent/SKILL.md).
Browser tests enable flags on their isolated server or stubbed capability response.

- `desktop-rail`, `mobile-menu`, composer counts, `sidebar`, and `canvas`:
  `tests/e2e_ui/github/test_pr_management.py::test_link_prs_without_workspace`
  uses its own real server, no host and no workspace. It links two PRs, checks
  counts, reloads and unlinks, while another session retains its membership.
- `desktop-composer` and `mobile-composer`:
  `tests/e2e_ui/github/test_github_tab.py::test_flagged_pr_review_and_actions`
  searches a collapsed diff, checks revision links and persisted viewed state,
  restores an edit after reload, retries a failed save and requests a reviewer.
  GitHub responses are mocked; no upstream write occurs.
- Recovery across the desktop rail and both composer surfaces:
  `tests/e2e_ui/github/test_github_tab.py::test_pr_diff_retry_keeps_associations`.
- Authorization and provider transport:
  `tests/server/routes/test_shell_permission_gate.py::test_pr_writes_require_flag_session_owner_and_host_owner`
  and `tests/runner/test_session_pr_resources.py::test_pr_writes_pin_actor_and_only_send_requested_fields`.

For human verification, add `pull_request_management` to the server's
`OMNIGENT_FEATURES` and restart it. Open a saved session, link two PR URLs,
reload, and unlink one. Link the same URL to a second session and confirm that
unlinking in the first does not affect it. In Changes, search a collapsed file,
mark it viewed and reload. In Summary, edit a description, reload before saving,
then save the recovered draft. In a permitted test repository, request a reviewer
and confirm the result on GitHub under the account displayed by Omnigent.

## Gotchas

- The flag is off by default; association mutations require session edit access.
- GitHub writes require the session owner and execution-host owner to be the
  current user. Admin visibility alone does not delegate host credentials.
- Offline sessions retain links; uncached provider details and writes require
  the execution host. Existing hosts should be upgraded for association sync.
- Drafts and viewed state are local to this browser/device, user and server.
  Viewed state resets at a new PR head. Search covers only the fetched patch.
- GitHub is the first supported write provider. GitLab and Azure DevOps retain
  their shared reads and association workflows; see their maps for coverage.
- Browser fixtures prove UI behavior, and mocked provider tests prove request
  construction. Live GitHub permissions and writes need a permitted test PR.
