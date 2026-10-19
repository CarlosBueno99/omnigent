# Claude working-status manual smoke

Smoke branch: `omni-12244-manual-smoke`. Production code is identical to
PR [#10259](https://github.com/omnigent-ai/omnigent/pull/10259) at
`6dbbe5908`; this branch only adds this recipe.
Allow about 10–15 minutes. Use **Claude Code / claude-native**, with Claude
2.1.295 or newer, and your existing live provider credentials.

## Start the isolated development pod

On Pat's machine, the worktree and dependencies are already prepared:

```sh
cd /Users/pat.sukprasert/git/omnigent-worktrees/omni-12244-manual-smoke
claude --version
env -u OMNIGENT_FEATURES UV_FROZEN=1 omnidev
```

Run this from a fresh terminal. Open the exact **ui** URL printed by omnidev.
Keep the supervisor running; use the shutdown steps below before toggling flags.
The command leaves `OMNIGENT_FEATURES` unset to exercise the enabled default.
The pod has its own Omnigent config and database. It inherits your existing
provider credentials and Claude machine settings. `UV_FROZEN=1` preserves the
candidate's dependency lock when omnidev launches subprocesses.

For another machine, fetch the smoke branch, create a worktree, and install
dependencies before the command above:

```sh
uv sync --frozen --extra all --group dev
pnpm install --frozen-lockfile --filter web
```

Create an empty workspace with `mktemp -d /tmp/omni-12244-smoke.XXXXXX`, and
select its printed path in the new-session composer. Select the local host,
Claude Code, **Haiku**, and normal permissions. The automated live run used
Claude 2.1.299 and `system.ai.claude-haiku-4-5`; record the model your terminal
actually displays if it differs. Do not use the mock verification environment
for this live smoke.

## Desktop checks

1. **New-session composer.** Send this as the first message:

   ```text
   Run exactly this harmless Bash command once: python3 -c "import time; time.sleep(18); print('SMOKE_FIRST_DONE')". Keep it in the foreground, wait for it to finish, then reply SMOKE_FIRST_DONE. Do not inspect or modify files or use other tools.
   ```

   If approval is requested, leave it pending for at least three seconds.
   Chat must show the approval card, and it must be clear that input is needed.
   Approve only this command. If your configuration auto-approves it, record
   permission coverage as not exercised.

2. **Quiet work and reconnect.** While that command sleeps, stay in Chat for
   three seconds, open Terminal, return to Chat, and reload. Working must remain
   visible throughout active work, regardless of whether the viewer is open.
   After the reply, it must settle to idle, with one copy of the reply.

3. **Existing-session composer.** Repeat the same prompt with
   `SMOKE_SECOND_DONE`. Check the same working → completion behavior.

4. **Interruption and recovery.** Send:

   ```text
   Run python3 -c "import time; time.sleep(45)" in the foreground. Do not inspect or modify files or use other tools; I will interrupt this command.
   ```

   Approve if asked. Once the tool is running, open Terminal and press Escape.
   Return to Chat: working must settle. Send `Reply exactly SMOKE_RECOVERED;
   do not use tools.` The reply must appear once and settle to idle.

5. **Embedded-terminal input.** Type `Reply exactly SMOKE_TERMINAL; do not use
   tools.` directly into the agent terminal. Return to Chat. Both the user
   message and the reply must appear once, and working must settle.

6. **Queued follow-up.** Start another 18-second foreground sleep. While it is
   active, send `After the current turn, reply exactly SMOKE_QUEUED; do not use
   tools.` from Chat. It may steer or queue according to Claude's behavior;
   it must be delivered once and the final indicator must settle.

## Additional entry points

- **Phone-width UI:** use browser responsive mode at about 390×844. In a fresh
  session repeat the quiet-work prompt, open Terminal from the header menu,
  return to Chat, and reload while the tool runs. Check the same status and
  completion. Responsive desktop testing does not establish a physical phone.
- **Local CLI:** in a second terminal, from the smoke worktree, run:

  ```sh
  env -u OMNIGENT_FEATURES UV_FROZEN=1 omnidev omnigent claude --model haiku
  ```

  Open its linked session in the same pod's web UI. Send the harmless sleep
  prompt from the local terminal and inspect Chat while it runs. Interrupt a
  second turn, then complete a follow-up. Exit the Claude terminal afterward.
  If you use a custom `--pod-dir`, pass it to both omnidev commands.

## Kill-switch control and cleanup

Quit omnidev with `q` or Ctrl-C. Then stop any detached host for this pod before
restarting. In the command below, replace the URL with the exact **server** URL
printed by this pod (not the frontend URL):

```sh
UV_FROZEN=1 omnidev omnigent host stop --server http://localhost:6781 --daemon-only
```

The local supervisor left detached host/runner helpers during verification, so
this explicit, server-scoped stop matters: a reused daemon could retain the old
feature setting. Do not use `omnigent stop` or `host stop --all`; those affect
other running environments. Then restart from the same worktree:

```sh
UV_FROZEN=1 OMNIGENT_FEATURES=-claude_osc_status omnidev
```

Start a **fresh** Claude session and repeat a short reply and the quiet-work
prompt. Replies, approvals and final idle must still work. Record differences
in quiet status: the legacy status-file path can also be accurate, so a visible
difference is not required. The automated flag-off control additionally checks
that no OSC relay status file exists.

To re-enable, repeat the shutdown steps and rerun the default command, then
start another fresh session. With other feature entries, preserve them and
append `-claude_osc_status` to disable OSC; remove the negative entry to
re-enable it. An explicit disable wins even if the positive name is present. This is a startup flag, not a hot switch; existing terminals retain
their launch configuration. These commands change only this pod's launch
environment. No release default or global rollout setting changes.

Repeat the shutdown steps when finished. Keep the scratch workspace and logs until any
failure is understood. Record the branch/commit, CLI version, actual model,
flag, entry point, prompt, expected/observed status, and a short recording or
screenshot for failures. Compare the session's `GET /v1/sessions/<id>` status
with Chat using the **server** URL printed by omnidev. A pending permission may
keep session status `running` while Chat shows the approval card.

This recipe follows [native working status](../feature-map/native-harnesses.md)
and the [harness acceptance checklist](../feature-map/harness-acceptance.md).
It covers an OSS local pod with a live provider. Actual Claude in Linux bwrap,
older-CLI fallback, and a managed Omnigent deployment require separate checks.
