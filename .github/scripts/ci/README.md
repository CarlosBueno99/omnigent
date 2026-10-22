# Dependency CI selection

The existing CI, E2E, E2E UI and Integration workflows use
`dependency_scope.py` to select consumers of isolated dependency changes.
This applies to both Dependabot PRs and maintainer bundles; author and labels
do not determine test coverage.

| Modified files | Tests retained in these workflows |
| --- | --- |
| `dev/omnidev/Cargo.lock` | Locked `omnidev` build and Rust tests |
| `tests/codex_parity/sidecar/Cargo.lock` | Locked sidecar build, Codex parity, two UI shards containing every sidecar fixture consumer |
| `pnpm-lock.yaml`, `web/package.json` | Full server E2E (including lockfile pin guards), browser-contract and UI E2E suites; web tests and other existing workflows still run |
| `.pre-commit-config.yaml` | Existing full pre-commit checks; no runtime tests |
| Any other file, added/deleted/renamed file, or unknown checkout | Full existing suites plus `omnidev` |

Bundles run the union of their consumers. Python dependencies, harness CLI
pins, workflow changes, and workspace configuration changes retain full CI.
Security scans, dependency cooldown, review eligibility and lint remain in
their existing workflows. This selects tests; it does not grant automerge.

Selection reads a depth-two checkout of the exact GitHub PR merge revision,
verifies its head parent against the event, and diffs its tree against the base
parent. It needs no API inventory or additional token permissions. Push,
scheduled and manually dispatched runs retain full coverage. Existing workflow
path filters still apply.

The UI filter uses pytest's resolved fixture closure, so tests depending on
`mocked_native_codex_session` indirectly are included. Filtering happens before
sharding. An empty selection or a skipped selected test fails. Keep new sidecar
consumers behind this fixture, or update the selector when adding another one.

`CI result` and `E2E UI result` always run after selection and the test jobs.
They require success from every selected consumer; an unexpected skip, missing
job, failure or cancellation fails the result. Intentional skips remain valid.
Merge Ready requires these results once their selection jobs exist, and never
accepts an explicitly skipped result. Older workflows without selection jobs
can finish their original checks. Check-run IDs order retries, so a queued
retry cannot be hidden by an older success with a completion timestamp.

For automatic Dependabot updates, Maintainer Approval checks policy eligibility
without waiting for CI. Merge Ready validates the security scan, advisory
inventory, successful consumer jobs and completed workflows for the current
head and main. The scheduled worker repeats those evidence checks before merging.
Human approval retains the existing manual path; required CI still applies.

Granting an automatic exemption first resets Merge Ready to pending so results
from the older gate logic cannot authorize a merge. Concurrent evaluations use
the dependency evidence validator. Approval completion refreshes that
status, as do scan/test completions and hold/waiver label changes. The worker
can refresh approval and dispatch Merge Ready after cooldown expiry or registry
recovery, with at most three dispatches per PR head in 24 hours and no duplicate
dispatch while a refresh is running. Neither gate waits for the other's success.
In report mode, only the separate approval check requires a human review;
Merge Ready retains its ordinary CI behavior. Human approval can also pass
when the dependency parser is unavailable.

To inspect the current selected browser cases without starting a server:

```sh
uv run --no-sync pytest tests/e2e_ui --collect-only \
  --ui-codex-sidecar-only -m 'not visual and not nightly' -q
```

To verify the selector and merge policy:

```sh
uv run --no-sync pytest tests/scripts/test_dependency_ci.py \
  tests/scripts/test_ci_results.py \
  tests/scripts/test_dependency_ui_selection.py \
  tests/scripts/test_dependabot_automerge.py --reruns 0 -q
```

## Regular E2E shard balancing

`e2e.yml` passes `tests/e2e/durations.json` to the shared E2E action. The
duration plugin assigns expensive tests first to the least-loaded shard, with
stable node-ID tie breaking. It partitions pytest's final filtered collection
and preserves its order within each shard. New tests receive a ten-second
estimate; removed tests in the profile never enter the collection. The eight
shards still use two `loadscope` workers each. Backcompat callers omit the
profile and retain `pytest-shard` assignment.

The initial profile uses median worker completion intervals from five
successful hosted runs linked in its `source` metadata. These are approximate
costs, including fixture overhead attributed between result messages. They
predict scheduling costs, not precise per-test phase durations.

Each regular E2E shard retains its JUnit report and worker progress logs in an
`e2e-timings-<run>-<attempt>-shard<N>` artifact for seven days, including on
failure. To refresh the profile manually, download **all eight** artifacts from
a successful full run (optionally several successful runs into one directory):

```sh
ghx run download RUN_ID --repo omnigent-ai/omnigent \
  --pattern 'e2e-timings-*' --dir /tmp/e2e-timings-RUN_ID
uv run --no-sync python scripts/update_e2e_durations.py /tmp/e2e-timings-RUN_ID \
  --source-run https://github.com/omnigent-ai/omnigent/actions/runs/RUN_ID
uv run --no-sync pytest tests/github/test_e2e_shards.py --reruns 0 -q
```

The refresh uses worker START-to-END elapsed time, including setup and
teardown, and rejects failed or incomplete shards. Review and commit the
updated profile. CI never downloads a profile or updates it automatically;
refresh when runtimes change enough to create a persistent shard imbalance.
