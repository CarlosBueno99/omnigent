"""Dragging through a long transcript while a reply streams must not drop more frames
than the same drags with nothing streaming. Phone stand-in: the web SPA at an iPhone
touch viewport under CDP CPU throttling."""

from __future__ import annotations

import json
import os
import uuid

from playwright.sync_api import Browser, CDPSession, Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, seed_committed_items

_VIEWPORT = {"width": 390, "height": 844}
_CPU_THROTTLE_RATE = 6
_HISTORY_TURNS = 300
_NEWEST_REPLY = f"Reply {_HISTORY_TURNS - 1}:"
_REPLY_WORDS = 1400
_REPACE_MS = 4
_STREAM_DRAG_START = 150
_STREAM_END = "STREAMENDMARKER"
_PROMPT = f"Scroll test stream {uuid.uuid4().hex[:8]}: tell me a long story."
# Frames are sampled only while the finger is down, after unmeasured warm-up drags,
# with a rest between drags so the transcript settles as between real flicks.
_WARMUP_DRAGS = 2
_DRAGS = 12
_DRAG_PAUSE_MS = 400

# The mock reply arrives as one consolidated text delta; re-pace it into token-sized
# deltas so the real stream/render path sees a streaming window. Everything else
# about the stream and turn lifecycle is the real path.
_REPACE_JS = """
(pace) => {
  const orig = window.fetch.bind(window);
  window.__repaced = 0;
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const resp = await orig(input, init);
    const ct = (resp.headers.get('content-type') || '').toLowerCase();
    if (!url.includes('/stream') || !resp.body || !ct.includes('text/event-stream')) return resp;
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    const enc = new TextEncoder();
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const stream = new ReadableStream({
      async start(controller) {
        let buf = '';
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let idx;
            while ((idx = buf.indexOf('\\n\\n')) >= 0) {
              const frame = buf.slice(0, idx + 2);
              buf = buf.slice(idx + 2);
              if (frame.includes('response.output_text.delta')) {
                const dline = frame.split('\\n').find((l) => l.startsWith('data:'));
                let text = '';
                try { text = JSON.parse(dline.slice(5).trim()).delta || ''; } catch (e) {}
                for (const tok of text.split(/(\\s+)/).filter(Boolean)) {
                  const payload = JSON.stringify({
                    type: 'response.output_text.delta', delta: tok,
                  });
                  controller.enqueue(enc.encode(
                    'event: response.output_text.delta\\ndata: ' + payload + '\\n\\n'
                  ));
                  window.__repaced++;
                  await sleep(pace);
                }
              } else {
                controller.enqueue(enc.encode(frame));
              }
            }
          }
          if (buf) controller.enqueue(enc.encode(buf));
        } finally { controller.close(); }
      },
    });
    return new Response(stream, {
      status: resp.status, statusText: resp.statusText, headers: resp.headers,
    });
  };
}"""

_SAMPLER_JS = """
() => {
  window.__frames = [];
  window.__long = [];
  let last = performance.now();
  const tick = (now) => {
    window.__frames.push([Math.round(now), Math.round((now - last) * 10) / 10]);
    last = now;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  try {
    const po = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        window.__long.push([Math.round(e.startTime), Math.round(e.duration)]);
      }
    });
    po.observe({ entryTypes: ['longtask'] });
  } catch (e) {}
  window.__scroller = (() => {
    const log = document.querySelector('[role="log"]');
    let best = null;
    (log ? log.querySelectorAll('*') : []).forEach((el) => {
      const scrolls = el.scrollHeight > el.clientHeight + 4;
      if (scrolls && (!best || el.scrollHeight > best.scrollHeight)) best = el;
    });
    return best || log;
  })();
}"""

# True once the re-paced stream has stopped delivering deltas for a few polls.
_STREAM_FINISHED_JS = """
() => {
  const n = window.__repaced;
  if (window.__rpLast === n) {
    window.__rpStable = (window.__rpStable || 0) + 1;
    return window.__rpStable > 3;
  }
  window.__rpLast = n;
  window.__rpStable = 0;
  return false;
}"""

# A mid-session user already has warm virtualized-row measurements; sweep the full
# history once so the idle control is not charged first-measurement cost.
_WARM_VIRTUALIZER_JS = """
() => new Promise((resolve) => {
  const s = window.__scroller;
  if (!s) return resolve();
  const step = Math.max(200, Math.floor(s.clientHeight * 0.8));
  let y = s.scrollHeight;
  const up = () => {
    y -= step;
    s.scrollTop = Math.max(0, y);
    if (y > 0) requestAnimationFrame(() => setTimeout(up, 16));
    else { s.scrollTop = s.scrollHeight; resolve(); }
  };
  up();
})"""


def _seed_history(session_id: str) -> None:
    from omnigent.entities import MessageData, NewConversationItem

    items: list[NewConversationItem] = []
    for turn in range(_HISTORY_TURNS):
        rid = f"resp_hist_{turn:03d}"
        items.append(
            NewConversationItem(
                type="message",
                response_id=rid,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"History prompt {turn}"}],
                ),
            )
        )
        paragraphs = "\n\n".join(
            f"paragraph {turn}.{p} " + " ".join(f"w{turn}x{p}x{i}" for i in range(14))
            for p in range(5)
        )
        items.append(
            NewConversationItem(
                type="message",
                response_id=rid,
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": f"Reply {turn}:\n\n{paragraphs}"}],
                    agent="hello_world",
                ),
            )
        )
    seed_committed_items(session_id, items)


def _elapsed(page: Page) -> float:
    return page.evaluate("() => performance.now()")


def _scroll_to_bottom(page: Page) -> None:
    page.evaluate("() => { const s = window.__scroller; if (s) s.scrollTop = s.scrollHeight; }")
    page.wait_for_timeout(300)


def _warm_virtualizer(page: Page) -> None:
    page.evaluate(_WARM_VIRTUALIZER_JS)
    page.wait_for_timeout(500)


def _drag_history_up(cdp: CDPSession, page: Page, reps: int) -> list[tuple[float, float]]:
    """Finger drags up the history; returns each drag's finger-down window."""
    windows: list[tuple[float, float]] = []
    x, y0, y1 = 195, 300, 760
    for _ in range(reps):
        t0 = _elapsed(page)
        cdp.send(
            "Input.dispatchTouchEvent",
            {"type": "touchStart", "touchPoints": [{"x": x, "y": y0}]},
        )
        for step in range(1, 7):
            cdp.send(
                "Input.dispatchTouchEvent",
                {"type": "touchMove", "touchPoints": [{"x": x, "y": y0 + (y1 - y0) * step / 6}]},
            )
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        windows.append((t0, _elapsed(page)))
        page.wait_for_timeout(_DRAG_PAUSE_MS)
    return windows


def _phase_metrics(page: Page, windows: list[tuple[float, float]]) -> dict:
    frames = page.evaluate("() => window.__frames")
    longs = page.evaluate("() => window.__long")

    def dragging(t: float) -> bool:
        return any(t0 <= t <= t1 for t0, t1 in windows)

    window = [d for (t, d) in frames if dragging(t)]
    long_in = [dur for (st, dur) in longs if dragging(st)]
    window_sorted = sorted(window)
    return {
        "drag_ms": round(sum(t1 - t0 for t0, t1 in windows)),
        "frames": len(window),
        "long_frames_gt50": sum(1 for d in window if d > 50),
        "long_frames_gt100": sum(1 for d in window if d > 100),
        "p95_ms": window_sorted[int(len(window_sorted) * 0.95)] if window_sorted else 0,
        "max_ms": max(window) if window else 0,
        "longtasks": len(long_in),
        "longtask_total_ms": sum(long_in),
    }


def _measured_drags(cdp: CDPSession, page: Page) -> dict:
    """Warm-up drags (unmeasured), then the measured drags of one phase."""
    _drag_history_up(cdp, page, _WARMUP_DRAGS)
    return _phase_metrics(page, _drag_history_up(cdp, page, _DRAGS))


def test_scrolling_long_history_while_streaming_stays_smooth(
    browser: Browser,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    _seed_history(session_id)
    reply = " ".join(f"streamword{n:04d}" for n in range(_REPLY_WORDS)) + f" {_STREAM_END}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply, "stream": True, "chunk_delay": 0.0}],
        key="scroll_stream",
        match=_PROMPT,
    )

    ctx_kwargs: dict = {
        "viewport": _VIEWPORT,
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 3,
    }
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**ctx_kwargs)
    context.add_init_script(f"({_REPACE_JS})({_REPACE_MS});")
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        expect(page.get_by_text(_NEWEST_REPLY).first).to_be_visible(timeout=30_000)
        page.wait_for_timeout(800)
        page.evaluate(_SAMPLER_JS)
        overflow = page.evaluate(
            "() => { const s = window.__scroller;"
            " return !!s && s.scrollHeight > s.clientHeight + 4; }"
        )
        assert overflow, "transcript did not overflow; seed more history"

        _warm_virtualizer(page)
        _scroll_to_bottom(page)

        cdp = context.new_cdp_session(page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": _CPU_THROTTLE_RATE})

        # Idle control: drag up with nothing streaming.
        before_top = page.evaluate("() => window.__scroller.scrollTop")
        idle = _measured_drags(cdp, page)
        assert page.evaluate("() => window.__scroller.scrollTop") != before_top, (
            "idle drags did not scroll the history"
        )

        # Streaming: send, wait for the re-paced stream to open, drag during it.
        _scroll_to_bottom(page)
        composer.fill(_PROMPT)
        page.get_by_role("button", name="Send", exact=True).click()
        page.wait_for_function(
            f"() => (window.__repaced || 0) > {_STREAM_DRAG_START}", timeout=60_000
        )
        _drag_history_up(cdp, page, _WARMUP_DRAGS)
        rp0 = page.evaluate("() => window.__repaced")
        streaming = _phase_metrics(page, _drag_history_up(cdp, page, _DRAGS))
        repaced_during = page.evaluate("() => window.__repaced") - rp0
        assert repaced_during > 10, (
            f"streaming window not open during the drags (repaced {repaced_during})"
        )

        # After: wait for the re-paced stream to finish, then run the same drags
        # once nothing is streaming. The transcript is virtualized, so wait on
        # the stream, not off-screen reply text.
        page.wait_for_function(_STREAM_FINISHED_JS, timeout=90_000, polling=300)
        total_repaced = page.evaluate("() => window.__repaced")
        assert total_repaced > 500, (
            f"re-paced stream did not deliver the full reply ({total_repaced})"
        )
        _scroll_to_bottom(page)
        after = _measured_drags(cdp, page)

        metrics = {
            "idle": idle,
            "streaming": streaming,
            "after": after,
            "repaced_during": repaced_during,
        }
        out = os.environ.get("SCROLL_WHILE_STREAMING_METRICS")
        if out:
            with open(out, "w") as fh:
                json.dump(metrics, fh)

        # The same drags with nothing streaming (idle before, after once the reply
        # finished) are the control; dragging while the reply streams must not
        # drop materially more frames, or the view visibly stutters.
        baseline_long = max(idle["long_frames_gt50"], after["long_frames_gt50"])
        assert streaming["long_frames_gt50"] <= baseline_long * 1.5 + 4, (
            "scrolling long history while streaming drops far more frames than the same "
            f"scroll with nothing streaming, so the view stutters: {metrics}"
        )
    finally:
        context.close()
