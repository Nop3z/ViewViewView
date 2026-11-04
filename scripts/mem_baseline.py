"""Front-end memory regression harness (docs/VERIFICATION.md baseline).

Drives the real ``index.mjs`` renderer inside a headless browser and asserts
the three S1 acceptance criteria (incremental rendering, see
``docs/ARCHITECTURE.md`` change log):

1. shadow-DOM node count stays constant across the whole run (no per-frame
   rebuild churn, no leak);
2. mean per-frame render time stays under ``MAX_FRAME_MS``;
3. after a forced GC the used JS heap returns to within ``MAX_HEAP_DRIFT_MB``
   of the baseline sample (no unbounded growth).

Usage::

    python scripts/mem_baseline.py [frames] [interval_ms] [depth] [columns]

Defaults: 3000 frames, 50 ms interval (Syncer pace), depth 1000, 5 columns.
Requires ``playwright`` with a chromium-family browser installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_JS = (ROOT / "src" / "viewviewview" / "static" / "index.mjs").read_text(encoding="utf-8")
CSS = (ROOT / "src" / "viewviewview" / "static" / "styles.css").read_text(encoding="utf-8")

MAX_FRAME_MS = 13.0
MAX_HEAP_DRIFT_MB = 5.0
MAX_NODE_DRIFT = 5

COLUMNS = [
    {"name": "state", "type": "str"},
    {"name": "delay", "type": "int"},
    {"name": "pulse", "type": "float"},
    {"name": "reply", "type": "str"},
    {"name": "errcode", "type": "int"},
]

PAGE = """<!doctype html>
<html>
<head><meta charset="utf-8"><style>html,body{{margin:0;padding:8px;}}</style></head>
<body>
<div id="host" style="height:480px;"></div>
<script>
window.__css = {css!r};
window.__columns = {columns!r};
window.__depth = {depth};
</script>
<script type="module">
{js}
// harness bridge: expose the anywidget default export for the driver below
window.__vvvRender = {{ render }};
</script>
</body>
</html>
"""


def build_page(depth: int, columns: list) -> str:
    js = INDEX_JS + "\nwindow.__vvvRender = { render };\n"
    return PAGE.format(js=js, css=CSS, columns=columns, depth=depth)





def main() -> int:
    frames = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    interval_ms = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    depth = int(sys.argv[3]) if len(sys.argv) > 3 else 1000
    columns = COLUMNS[: int(sys.argv[4])] if len(sys.argv) > 4 else COLUMNS

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is required: pip install playwright && playwright install chromium")
        return 2

    html_path = ROOT / "dev" / "mem_harness.html"
    html_path.write_text(build_page(depth, columns), encoding="utf-8")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                channel="msedge",
                headless=True,
                args=["--js-flags=--expose-gc", "--enable-precise-memory-info"],
            )
        except Exception:
            browser = pw.chromium.launch(
                args=["--js-flags=--expose-gc", "--enable-precise-memory-info"]
            )
        page = browser.new_page()
        page.goto(html_path.as_uri())

        # The module script exports { render }; grab it via an addEventListener
        # bridge the page sets up (see driver).
        page.evaluate(
            """
            ([frames, interval]) => {
              window.__args = [frames, interval];
              return true;
            }
            """,
            [frames, interval_ms],
        )

        result = page.evaluate(
            """
            async ([frames, interval, depth]) => {
              const render = window.__vvvRender.render;

              const host = document.getElementById("host");
              const shadow = host.attachShadow({ mode: "open" });
              const style = document.createElement("style");
              style.textContent = window.__css;
              shadow.appendChild(style);
              const el = document.createElement("div");
              el.style.height = "100%";
              shadow.appendChild(el);

              const columns = window.__columns;
              let payload = null;
              const listeners = [];
              const model = {
                get: (key) => (key === "payload" ? payload : key === "theme" ? "dark" : undefined),
                on: (event, cb) => { if (event === "change:payload") listeners.push(cb); },
                off: (event, cb) => {
                  const i = listeners.indexOf(cb);
                  if (i !== -1) listeners.splice(i, 1);
                },
              };

              const seedRow = (n) => [
                ["PASS", "FAIL", "TIMEOUT"][n % 3],
                100 + (n % 1900),
                10 + (n % 40) + 0.5,
                ["ok", "short", "open"][n % 3],
                n % 9,
              ];
              const makePayload = (n) => {
                const rows = [];
                for (let i = 0; i < Math.min(depth, n); i += 1) rows.push(seedRow(n - i));
                return {
                  columns,
                  state_key: "state",
                  counts: {
                    PASS: Math.floor(n / 3) + 1,
                    FAIL: Math.floor(n / 3),
                    TIMEOUT: Math.floor(n / 3),
                  },
                  rows,
                  total: n,
                  window: rows.length,
                  version: n,
                };
              };

              const countNodes = () => {
                let total = 0;
                const walk = (node) => {
                  total += 1;
                  if (node.shadowRoot) walk(node.shadowRoot);
                  for (const child of node.children || []) walk(child);
                };
                for (const child of shadow.children) walk(child);
                return total;
              };
              const heapUsed = () =>
                performance.memory ? performance.memory.usedJSHeapSize : 0;

              // ---- initial render ----
              payload = makePayload(depth);
              const teardown = render({ model, el });

              gc(); gc(); gc();
              const baseNodes = countNodes();
              const baseHeap = heapUsed();

              const frameTimes = [];
              let nodesMin = baseNodes;
              let nodesMax = baseNodes;
              const t0 = performance.now();

              for (let n = depth + 1; n <= depth + frames; n += 1) {
                const next = makePayload(n);
                const tFrame = performance.now();
                payload = next;
                listeners.forEach((cb) => cb());
                frameTimes.push(performance.now() - tFrame);
                const nodes = countNodes();
                if (nodes < nodesMin) nodesMin = nodes;
                if (nodes > nodesMax) nodesMax = nodes;
                await new Promise((r) => setTimeout(r, interval));
              }

              gc(); gc(); gc();
              const endHeap = heapUsed();

              // Correctness: the patched DOM must equal the last payload.
              const body = shadow.querySelector("table.vvv-table--detail tbody");
              let domMismatches = 0;
              const samples = [];
              for (let i = 0; i < payload.rows.length; i += 1) {
                const tr = body.children[i];
                for (let j = 0; j < columns.length; j += 1) {
                  const raw = payload.rows[i][j];
                  const want = raw === null || raw === undefined ? "" : String(raw);
                  const got = tr && tr.children[j] ? tr.children[j].textContent : null;
                  if (got !== want) {
                    domMismatches += 1;
                    if (samples.length < 5) samples.push([i, j, got, want]);
                  }
                }
              }
              return {
                frames,
                baseNodes,
                nodesMin,
                nodesMax,
                meanFrameMs: frameTimes.reduce((a, b) => a + b, 0) / frameTimes.length,
                p95FrameMs: frameTimes.slice().sort((a, b) => a - b)[
                  Math.floor(frameTimes.length * 0.95)
                ],
                baseHeapMB: baseHeap / 1048576,
                endHeapMB: endHeap / 1048576,
                heapDriftMB: (endHeap - baseHeap) / 1048576,
                wallSeconds: (performance.now() - t0) / 1000,
                domMismatches,
                domSamples: samples,
              };
            }
            """,
            [frames, interval_ms, depth],
        )

        browser.close()

    mean = result["meanFrameMs"]
    p95 = result["p95FrameMs"]
    drift = result["heapDriftMB"]
    node_drift = result["nodesMax"] - result["nodesMin"]

    print(f"frames={result['frames']} depth={depth} cols={len(columns)}")
    print(
        "shadow nodes: "
        f"base={result['baseNodes']} min={result['nodesMin']} "
        f"max={result['nodesMax']} (drift {node_drift})"
    )
    print(f"frame time: mean={mean:.2f}ms p95={p95:.2f}ms (budget {MAX_FRAME_MS}ms)")
    print(
        f"heap: base={result['baseHeapMB']:.1f}MB end={result['endHeapMB']:.1f}MB "
        f"drift={drift:+.1f}MB (budget {MAX_HEAP_DRIFT_MB}MB)"
    )
    print(f"dom mismatches: {result['domMismatches']}")

    failures = []
    if mean >= MAX_FRAME_MS:
        failures.append(f"mean frame time {mean:.2f}ms >= {MAX_FRAME_MS}ms")
    if drift >= MAX_HEAP_DRIFT_MB:
        failures.append(f"forced-GC heap drift {drift:.1f}MB >= {MAX_HEAP_DRIFT_MB}MB")
    if node_drift > MAX_NODE_DRIFT:
        failures.append(f"node count drift {node_drift} > {MAX_NODE_DRIFT}")
    if result["domMismatches"]:
        failures.append(
            f"DOM does not match payload: {result['domMismatches']} cells, "
            f"samples={result['domSamples']}"
        )

    if failures:
        print("FAIL: " + "; ".join(failures))
        return 1
    print("PASS: memory baseline holds (constant DOM, bounded heap)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
