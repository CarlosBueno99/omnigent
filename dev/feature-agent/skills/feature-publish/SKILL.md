---
name: feature-publish
description: Commit the feature, write the PR body, publish in the selected mode, and drive the PR.
---

# Publish and drive the PR

## Commit

Commit the implementation and its tests on the working branch with the
repository's conventions, signed off (`git commit -s`). Stage paths explicitly
and check `git diff --cached --stat`: **never** commit `recordings/` or
`.omnigent/` files. Read the staged diff for redundant comments. If a commit
hook changed tested files, rerun their checks. If the checkout has
`.github/scripts/pr-template/hygiene.py`, run it with `--base origin/main`.

## Choose the publication mode

- **Local-only (`skip_push: true`):** stop after committing. Report the branch in
  `pushed_branch` and leave `pr_url` empty; do not prepare a PR or drive one.
  The outcome is `partially_implemented`, with publishing and the PR loop in
  `remaining_work`.
- **Workflow-owned (a CI publisher contract is appended to your instructions):**
  do not push or make any `gh` write. Write and validate `.omnigent/pr-body.md`
  (below), write the final handoff, and stop. The workflow publishes the PR and
  runs the PR loop.
- **Direct (no publisher contract):** push the branch, open the PR, then drive it
  (below).

When `.omnigent/pr-gate.json` exists, make every PR write (create, edit, comment,
review, ready) with the `pr.py` helper CI supplies, at the path given in the
publisher contract. Keep Git pushes on the supplied credential. Never decode,
search for, or export another token, and never change credential configuration.

The PR is always **ready for review, never a draft**, including when a
recording is blocked.

## PR body

Copy `.github/pull_request_template.md` to `.omnigent/pr-body.md` and fill it
in, keeping every heading and checkbox row:

- First lines: `Implements <feature_url>` and `<!-- omnigent-feature-managed -->`.
  Do not use a closing keyword against a Linear URL.
- **Summary:** a 1–2 sentence plain-language description of what users can now
  do, then subsections *Design*, *Alternatives considered*, *CUJs covered*, and
  *CUJs not covered (and why)*.
- **Test Plan:** each covered CUJ with the tests that exercise it, plus the
  impact-assessment checks, as short bullets.
- **Demo:** a `### CUJ-n` heading for each covered CUJ, saying its recordings
  are on the Linear ticket, or pending manual upload when nothing can post
  them (see "Post the demo" below). For a CUJ with a blocked recording, give
  the blocker and the manual test steps instead.
- Check **Feature**, plus **UI / frontend change** when the UI changes.

Aim for fewer than 800 visible words. If the checkout has
`.github/scripts/pr-template/validate.py`, run it with
`PR_BODY="$(cat .omnigent/pr-body.md)"` and fix every error. Then publish with
`gh pr create --body-file .omnigent/pr-body.md`, or the helper in checked mode.

## Post the demo

The demos go on the Linear ticket, never on the PR. List every clip in its
CUJ's `recordings` in the handoff, as a path under `recordings/`.

- **CI:** the workflow posts each covered CUJ's listed recordings to the ticket
  after your turn. Do not upload them yourself.
- **Local run:** when a Linear key is available, post one ticket comment with a
  `CUJ-n` label per covered CUJ and its clips (GraphQL `fileUpload`, then
  `commentCreate` with the returned asset URLs). Without a Linear key, tell the
  user the clip paths so they can post them, and say in each Demo entry that
  its recordings are pending manual upload.

## Drive the PR (direct publication)

Stay on the PR until CI is green and both reviews are settled:

1. Load the shared `otto-drive-pr` skill. Follow `ci.md` to keep the branch
   mergeable and CI green, and `preview.md` to label `ui-preview`. Skip its
   live-validation prompt and `validation_surface` steps; the Demo section and
   the CUJ steps serve that purpose.
2. Load the shared `otto-pr-review-loop` skill and run Polly and OCR until every
   finding is addressed or answered with a reason. Request both again after
   every push. Run its live gate before claiming `implemented`.
3. Refresh `otto-impact-assessment` after every change in this loop.
4. Request review from a maintainer: the ticket's assignee or the owners of the
   main changed files. Never approve or merge.

If a concrete blocker or deadline prevents finishing, leave the PR open and
report `partially_implemented` with the remaining work.
