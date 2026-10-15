"""Real-Codex check: a budget refusal after a model switch names the previous model.

Codex compacts a resumed thread on the model of its last recorded turn before the
first turn on a newly selected model (``gpt-5.5`` and ``gpt-5.6-sol`` carry
different ``comp_hash`` values), and records the new turn's context only once
that compaction succeeds. When the previous model's budget is exhausted the
compaction request is refused, Codex never reaches the selected model, and every
later turn repeats the failure. This drives the installed ``codex`` through
Omnigent's app-server client against a loopback gateway that refuses one model,
feeds the real notifications through the codex-native forwarder, and checks the
surfaced failure explains the stuck compaction instead of a bare budget 403.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.app_server import (
    CodexNativeAppServer,
    preload_codex_thread_for_resume,
)
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState, write_bridge_state
from omnigent.harnesses.codex_native.rollout import find_thread_rollout, last_turn_context_model
from omnigent.runner.launch_failure import codex_previous_model_budget_hint
from tests.e2e._codex_loopback_resume import clean_codex_env, sse_text_response
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.harnesses.codex_native.forwarder._support import _RecordingClient

# Two built-in codex models whose ``comp_hash`` differ, so a switch between
# them makes Codex compact on the previous model first.
_OVER_BUDGET_MODEL = "gpt-5.5"
_SELECTED_MODEL = "gpt-5.6-sol"
_BUDGET_REFUSAL = json.dumps(
    {
        "error_code": "PERMISSION_DENIED",
        "message": (
            'Budget "frontier-budget" (00000000-0000-0000-0000-000000000001) has reached its '
            "limit of $2000. To continue, contact an admin to increase the budget or use a "
            "different budget."
        ),
    }
)


class _BudgetGateway(http.server.ThreadingHTTPServer):
    """Loopback Responses provider that refuses one model with the gateway's budget 403."""

    def __init__(self) -> None:
        self.models_requested: list[str | None] = []
        super().__init__(("127.0.0.1", 0), _BudgetGatewayHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _BudgetGatewayHandler(http.server.BaseHTTPRequestHandler):
    server: _BudgetGateway

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, "application/json", b'{"models":[]}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model")
        self.server.models_requested.append(model)
        if model == _OVER_BUDGET_MODEL:
            self._send(403, "application/json", _BUDGET_REFUSAL.encode())
            return
        self._send(200, "text/event-stream", sse_text_response(f"ok from {model}"))

    def log_message(self, *args: object) -> None:
        pass


def _provider_config_overrides(base_url: str) -> list[str]:
    return [
        'model_provider="loopback"',
        (
            "model_providers.loopback="
            f'{{name="Loopback",base_url={json.dumps(base_url)},wire_api="responses",'
            "requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}"
        ),
        "analytics.enabled=false",
        "feedback.enabled=false",
        "features.plugins=false",
        'otel.metrics_exporter="none"',
        "check_for_update_on_startup=false",
    ]


def _record_turn_on_over_budget_model(
    *, codex_home: Path, workspace: Path, child_home: Path, base_url: str
) -> subprocess.CompletedProcess[str]:
    """Run one ``codex exec`` turn on the over-budget model so its rollout records that model."""
    env = clean_codex_env(child_home)
    env["CODEX_HOME"] = str(codex_home)
    args = ["codex", "exec", "-m", _OVER_BUDGET_MODEL, "--skip-git-repo-check"]
    for override in _provider_config_overrides(base_url):
        args.extend(["-c", override])
    args.append("Work on the frontier model.")
    return subprocess.run(
        args,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


async def _forward_turn_on_selected_model(
    *,
    codex_home: Path,
    bridge_dir: Path,
    workspace: Path,
    child_home: Path,
    thread_id: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """Resume the thread on the selected model and forward Codex's notifications.

    :returns: Every ``external_session_status`` post the forwarder made.
    """
    session_id = "conv_budget_switch"
    app_server = CodexNativeAppServer(
        codex_path=shutil.which("codex"),
        socket_path=bridge_dir / "app-server.sock",
        codex_home=codex_home,
        env=clean_codex_env(child_home),
        config_overrides=_provider_config_overrides(base_url),
        cwd=workspace,
        bridge_dir=bridge_dir,
        pinned_model=_SELECTED_MODEL,
        reconcile_process_registry=False,
        session_id=session_id,
    )
    recording = _RecordingClient()
    forwarder_state = fwd._CodexForwarderState()
    usage_coalescer = fwd._SessionUsageCoalescer(recording, session_id)  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    client = None
    try:
        await app_server.start()
        client = await preload_codex_thread_for_resume(
            str(app_server.socket_path), thread_id, retain_client=True, cwd=workspace
        )
        assert client is not None
        # The forwarder reads the thread's rollout and config through this state.
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=session_id,
                socket_path=str(app_server.socket_path),
                thread_id=thread_id,
                codex_home=str(codex_home),
                cwd=str(workspace),
            ),
        )
        await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Please continue on the cheaper model."}],
            },
        )
        async with asyncio.timeout(120):
            async for event in client.iter_events():
                await fwd._handle_event(
                    recording,  # type: ignore[arg-type]
                    session_id=session_id,
                    bridge_dir=bridge_dir,
                    event=event,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                    expected_thread_id=thread_id,
                    forwarder_state=forwarder_state,
                )
                if event.get("method") in {"turn/completed", "turn/failed"}:
                    break
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()
        await app_server.close()
    return [
        body["data"] for _url, body in recording.posts if body["type"] == "external_session_status"
    ]


@pytest.mark.posix_only
@pytest.mark.timeout(300)
async def test_budget_refusal_after_model_switch_explains_previous_model_compaction(
    tmp_path: Path,
) -> None:
    """Switching models off an over-budget one surfaces the stuck compaction, not a bare 403."""
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    # The app-server socket lives under the bridge dir; pytest's tmp path is
    # too deep for the AF_UNIX path limit, so the bridge gets a short root.
    short_root = Path(tempfile.mkdtemp(prefix="cx-switch-"))
    bridge_dir = short_root / "b"
    codex_home = bridge_dir / "codex-home"
    workspace = tmp_path / "workspace"
    child_home = tmp_path / "home"
    for path in (codex_home, workspace, child_home):
        path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)

    gateway = _BudgetGateway()
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        frontier_turn = _record_turn_on_over_budget_model(
            codex_home=codex_home,
            workspace=workspace,
            child_home=child_home,
            base_url=gateway.base_url,
        )
        assert "has reached its limit" in frontier_turn.stdout + frontier_turn.stderr, (
            "the over-budget turn did not reach the gateway's refusal: "
            f"exit={frontier_turn.returncode} stdout={frontier_turn.stdout[-1500:]!r} "
            f"stderr={frontier_turn.stderr[-1500:]!r}"
        )
        rollouts = sorted((codex_home / "sessions").glob("**/rollout-*.jsonl"))
        assert len(rollouts) == 1, f"expected one rollout, found {rollouts}"
        thread_match = re.search(
            r"-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", rollouts[0].stem
        )
        assert thread_match is not None, rollouts[0].name
        thread_id = thread_match.group(1)
        assert find_thread_rollout(codex_home, thread_id) == rollouts[0]
        assert last_turn_context_model(rollouts[0]) == _OVER_BUDGET_MODEL, (
            "codex no longer records the refused turn's model; the previous-model "
            "detection needs revisiting"
        )
        gateway.models_requested.clear()

        statuses = await _forward_turn_on_selected_model(
            codex_home=codex_home,
            bridge_dir=bridge_dir,
            workspace=workspace,
            child_home=child_home,
            thread_id=thread_id,
            base_url=gateway.base_url,
        )
    finally:
        gateway.shutdown()
        gateway.server_close()
        shutil.rmtree(short_root, ignore_errors=True)

    if _SELECTED_MODEL in gateway.models_requested:
        pytest.skip(
            "the installed codex reached the selected model after a refused previous-model "
            f"compaction (models requested: {gateway.models_requested}); the stuck state this "
            "hint explains no longer occurs"
        )
    assert gateway.models_requested and set(gateway.models_requested) == {_OVER_BUDGET_MODEL}, (
        f"codex contacted {gateway.models_requested}, not only the previous model"
    )

    failed = [status for status in statuses if status.get("status") == "failed"]
    assert len(failed) == 1, f"expected one failed status, got {statuses}"
    output = failed[0]["output"]
    assert "has reached its limit" in output
    assert output.endswith(
        codex_previous_model_budget_hint(_OVER_BUDGET_MODEL, _SELECTED_MODEL)
    ), f"the forwarded failure did not explain the previous-model compaction: {output!r}"
    assert "reauth_required" not in failed[0]
