/*
 * ViewViewView dual-pane renderer — ARCHITECTURE.md §4.3 / §4.4.
 * Pure single-file ESM, zero npm dependencies, no imports at all.
 *
 * Injection guard (§4.4): payload strings are untrusted user data, so the DOM
 * is built exclusively with createElement/textContent — innerHTML is banned.
 *
 * Column widths: drag the divider on a header cell. Neighbouring columns share
 * the delta (Excel style); dragging the last column widens the table and the
 * pane scrolls horizontally. Widths survive live payload re-renders (kept in
 * the per-view layout state) and are stored per table ("counts" / "detail").
 */

const MIN_COLUMN_WIDTH = 56;

/** API color key -> CSS custom property (see contract §4.4). */
const COLOR_VARS = Object.freeze({
  text: "--vvv-fg",
  count: "--vvv-count",
  number: "--vvv-number",
  muted: "--vvv-fg-dim",
  background: "--vvv-bg",
  panel: "--vvv-panel",
  header: "--vvv-header",
  row_alt: "--vvv-row-alt",
  accent: "--vvv-accent",
  border: "--vvv-border",
  live: "--vvv-live",
});

/**
 * Default anywidget entry point: render the current payload snapshot and keep
 * the DOM in sync with subsequent `change:payload` / `change:theme` pushes.
 * @param {{
 *   get: (key: string) => unknown,
 *   on: (name: string, callback: () => void) => void,
 *   off?: (name: string, callback: () => void) => void,
 * }} model
 * @param {HTMLElement} el Shadow-DOM element owned by anywidget.
 * @returns {() => void} Teardown function, called by anywidget on view removal.
 */
function render({ model, el }) {
  const root = document.createElement("div");
  root.className = "vvv-root";
  applyTheme(root, model.get("theme"));
  applyColors(root, model.get("colors"));
  el.appendChild(root);

  const layout = {
    counts: new Map(), // column-width array for the counts table
    detail: new Map(), // column-width array for the detail table
    dragging: false,
    pending: false,
  };
  /** @type {{ key: string, countsBody: HTMLElement | null, detailBody: HTMLElement | null, footerLabel: HTMLElement | null } | null} */
  let mounted = null;
  const readNumberColumns = (value) =>
    new Set(Array.isArray(value) ? value : ["delay", "pulse"]);
  const ctx = {
    layout,
    mounted,
    numberColumns: readNumberColumns(model.get("number_columns")),
    redraw: () => draw(model.get("payload")),
  };
  const draw = (payload) => renderPayload(root, payload, ctx);

  draw(model.get("payload"));

  const onPayloadChange = () => {
    // A live frame must not destroy the DOM under an active drag; replay the
    // latest snapshot as soon as the pointer is released.
    if (layout.dragging) {
      layout.pending = true;
      return;
    }
    draw(model.get("payload"));
  };
  const onThemeChange = () => applyTheme(root, model.get("theme"));
  const onColorsChange = () => applyColors(root, model.get("colors"));
  const onNumberColumnsChange = () => {
    ctx.numberColumns = readNumberColumns(model.get("number_columns"));
    draw(model.get("payload"));
  };
  model.on("change:payload", onPayloadChange);
  model.on("change:theme", onThemeChange);
  model.on("change:colors", onColorsChange);
  model.on("change:number_columns", onNumberColumnsChange);

  return () => {
    ctx.mounted = null; // drop DOM references so the removed view can be GC'd
    if (typeof model.off === "function") {
      model.off("change:payload", onPayloadChange);
      model.off("change:theme", onThemeChange);
      model.off("change:colors", onColorsChange);
      model.off("change:number_columns", onNumberColumnsChange);
    }
  };
}

/**
 * Rebuild the two panes from one §4.3 payload snapshot. The payload may be
 * absent/partial while the widget is being wired, so every access is defensive.
 * @param {HTMLElement} root
 * @param {unknown} payload
 * @param {{ layout: object, redraw: () => void }} ctx
 * @returns {void}
 */
function renderPayload(root, payload, ctx) {
  const columns = readColumns(payload);
  const counts = readCounts(payload);
  const rows = readRows(payload);
  const hasCounts = Object.keys(counts).length > 0;
  const schemaKey = layoutKey(columns, readStateKey(payload), ctx.numberColumns);
  const stateKey = readStateKey(payload);

  // Fast path: same schema/state/number-columns as the currently mounted
  // panes -> update the mounted tables in place instead of rebuilding ~6000
  // nodes per frame. The DOM contract is unchanged: cell classes and text
  // content end up exactly what a full rebuild would produce.
  const mounted = ctx.mounted;
  const canPatch =
    mounted !== null &&
    mounted.key === schemaKey &&
    root.contains(mounted.countsBody) &&
    root.contains(mounted.detailBody) &&
    root.contains(mounted.footerLabel);
  if (canPatch) {
    patchCountsTable(mounted.countsBody, stateKey, counts);
    if (mounted.detailBody !== null) {
      patchDetailRows(mounted.detailBody, columns, rows, ctx, mounted, payload);
    }
    patchFooter(mounted.footerLabel, payload, rows);
    mounted.lastRows = rows;
    mounted.lastTotal = isPayloadObject(payload) ? payload.total : undefined;
    return;
  }

  clearChildren(root);
  if (columns.length === 0 && rows.length === 0 && !hasCounts) {
    ctx.mounted = null;
    root.appendChild(buildNote("No data recorded."));
    return;
  }

  const countsPane = buildCountsPane(stateKey, counts, ctx);
  root.appendChild(countsPane);

  // §3.2 limit=0 → counts-only payload (no detail pane); rows arrive
  // newest-first already (§4.3), so render them in array order as-is.
  // Declared columns render even before the first record (empty tbody).
  let detailBody = null;
  if (columns.length > 0) {
    const detailPane = buildDetailPane(columns, rows, ctx);
    root.appendChild(detailPane);
    detailBody = detailPane.querySelector("tbody");
  }

  const footer = buildFooter(payload, rows);
  root.appendChild(footer);

  ctx.mounted = {
    key: schemaKey,
    countsBody: countsPane.querySelector("tbody"),
    detailBody,
    footerLabel: footer.firstChild,
    lastRows: rows,
    lastTotal: isPayloadObject(payload) ? payload.total : undefined,
  };
}

/**
 * Stable identity for the mounted pane skeleton: column names+types, the
 * state column and the number-highlight set. Any change forces one full
 * rebuild (the old path), after which in-place updates resume.
 * @returns {string}
 */
function layoutKey(columns, stateKey, numberColumns) {
  const cols = columns
    .map((column) => `${column.name}:${column.type ?? "?"}`)
    .join("|");
  const numbers = Array.from(numberColumns).join(",");
  return `${stateKey ?? ""};${cols};${numbers}`;
}

/**
 * Rebuild only the counts tbody (usually a handful of rows) in place.
 * Column widths live on the table skeleton, which is untouched here.
 * @returns {void}
 */
function patchCountsTable(tbody, stateKey, counts) {
  if (tbody === null) {
    return;
  }
  const entries = Object.entries(counts);
  while (tbody.children.length > entries.length) {
    tbody.removeChild(tbody.lastChild);
  }
  while (tbody.children.length < entries.length) {
    const tr = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.className = "vvv-td";
    const countCell = document.createElement("td");
    countCell.className = "vvv-td vvv-td--num vvv-count";
    tr.appendChild(nameCell);
    tr.appendChild(countCell);
    tbody.appendChild(tr);
  }
  entries.forEach(([bucket, count], index) => {
    const tr = tbody.children[index];
    const nameCell = tr.children[0];
    const countCell = tr.children[1];
    const name = toDisplayText(bucket);
    const value = toDisplayText(count);
    if (nameCell.textContent !== name) {
      nameCell.textContent = name;
    }
    if (countCell.textContent !== value) {
      countCell.textContent = value;
    }
  });
}

/**
 * Fast path for the ring-window update pattern: K new rows arrive at the top
 * (K = total_now - total_previous) and, once the window is full, the same
 * number of oldest rows drop off the bottom. The overlap is verified cell by
 * cell against the previous payload, then realised with one fragment insert
 * and K removals — instead of rewriting every cell, which invalidates the
 * layout of the whole table on every frame.
 * @returns {boolean} True when the shift was applied.
 */
function applyShiftedRows(tbody, columns, rows, ctx, mounted, payload) {
  const previous = mounted.lastRows;
  const previousTotal = mounted.lastTotal;
  const total = isPayloadObject(payload) ? payload.total : undefined;
  if (!previous || previous.length === 0) {
    return false;
  }
  if (typeof previousTotal !== "number" || typeof total !== "number") {
    return false;
  }
  const fresh = total - previousTotal;
  if (!Number.isInteger(fresh) || fresh <= 0 || fresh > rows.length) {
    return false;
  }
  // Same window size → dropped as many as arrived; grown by exactly K → no drop.
  const grew = rows.length === previous.length + fresh;
  if (!grew && rows.length !== previous.length) {
    return false;
  }
  const compared = Math.min(previous.length, rows.length - fresh);
  if (compared <= 0) {
    return false;
  }
  for (let rowIndex = 0; rowIndex < compared; rowIndex += 1) {
    const next = rows[rowIndex + fresh];
    const prev = previous[rowIndex];
    if (!Array.isArray(next) || !Array.isArray(prev) || next.length !== prev.length) {
      return false;
    }
    for (let columnIndex = 0; columnIndex < prev.length; columnIndex += 1) {
      if (next[columnIndex] !== prev[columnIndex]) {
        return false;
      }
    }
  }
  const fragment = document.createDocumentFragment();
  for (let rowIndex = 0; rowIndex < fresh; rowIndex += 1) {
    const values = rows[rowIndex];
    if (!Array.isArray(values)) {
      return false;
    }
    const tr = document.createElement("tr");
    columns.forEach((column, index) => {
      const td = document.createElement("td");
      td.className =
        "vvv-td" +
        (isNumericType(column.type) ? " vvv-td--num" : "") +
        (ctx.numberColumns.has(column.name) ? " vvv-num" : "");
      td.textContent = toDisplayText(values[index]);
      tr.appendChild(td);
    });
    fragment.appendChild(tr);
  }
  tbody.insertBefore(fragment, tbody.firstChild);
  if (!grew) {
    for (let rowIndex = 0; rowIndex < fresh; rowIndex += 1) {
      tbody.removeChild(tbody.lastChild);
    }
  }
  return true;
}

/**
 * Align the detail tbody to ``rows`` with the minimum number of DOM writes:
 * reuse existing <tr> when the row count matches, add/remove only the tail
 * difference, and set cell text only when it actually changed.
 * @returns {void}
 */
function patchDetailRows(tbody, columns, rows, ctx, mounted, payload) {
  if (applyShiftedRows(tbody, columns, rows, ctx, mounted, payload)) {
    return;
  }
  // Trim or extend the row count first.
  while (tbody.children.length > rows.length) {
    tbody.removeChild(tbody.lastChild);
  }
  const template = columns.map((column) =>
    isNumericType(column.type) ? "vvv-td vvv-td--num" : "vvv-td",
  );
  while (tbody.children.length < rows.length) {
    const tr = document.createElement("tr");
    columns.forEach((column, index) => {
      const td = document.createElement("td");
      td.className =
        template[index] + (ctx.numberColumns.has(column.name) ? " vvv-num" : "");
      td.textContent = "";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }

  rows.forEach((row, rowIndex) => {
    const values = Array.isArray(row) ? row : [];
    const tr = tbody.children[rowIndex];
    columns.forEach((column, index) => {
      const td = tr.children[index];
      const text = toDisplayText(values[index]);
      if (td.textContent !== text) {
        td.textContent = text;
      }
    });
  });
}

/** Update the footer caption text in place (nodes are reused). @returns {void} */
function patchFooter(label, payload, rows) {
  const safe = isPayloadObject(payload) ? payload : {};
  const total = toCount(safe.total, "0");
  const windowSize = toCount(safe.window, String(rows.length));
  const text = `已记录 ${total} 行 / 当前窗口 ${windowSize} 行`;
  if (label.textContent !== text) {
    label.textContent = text;
  }
}

/** @returns {{ name: string, type: string }[]} User columns of the payload. */
function readColumns(payload) {
  if (!isPayloadObject(payload) || !Array.isArray(payload.columns)) {
    return [];
  }
  return payload.columns.filter(
    (column) =>
      column !== null &&
      typeof column === "object" &&
      typeof column.name === "string",
  );
}

/** @returns {Record<string, unknown>} The counts dict of the payload. */
function readCounts(payload) {
  return isPayloadObject(payload) && isPayloadObject(payload.counts)
    ? payload.counts
    : {};
}

/** @returns {unknown[][]} The rows array of the payload. */
function readRows(payload) {
  return isPayloadObject(payload) && Array.isArray(payload.rows)
    ? payload.rows
    : [];
}

/** @returns {string | null} The state column name, or null when absent. */
function readStateKey(payload) {
  return isPayloadObject(payload) && typeof payload.state_key === "string"
    ? payload.state_key
    : null;
}

/**
 * Top pane: category statistics. No state column → graceful empty state.
 * @param {string | null} stateKey
 * @param {Record<string, unknown>} counts
 * @param {{ layout: object, redraw: () => void }} ctx
 * @returns {HTMLElement}
 */
function buildCountsPane(stateKey, counts, ctx) {
  const pane = document.createElement("section");
  pane.className = "vvv-pane vvv-pane--counts";
  if (stateKey === null) {
    pane.appendChild(buildNote("No state column"));
  } else {
    pane.appendChild(buildCountsTable(stateKey, counts, ctx));
  }
  return pane;
}

/**
 * Top pane table: one row per state bucket, header `[state_key | count]`.
 * Bucket order = payload dict order (no re-sorting; see OPEN_QUESTIONS B2).
 * @returns {HTMLElement}
 */
function buildCountsTable(stateKey, counts, ctx) {
  const table = document.createElement("table");
  table.className = "vvv-table vvv-table--counts";
  table.appendChild(createColgroup(2));
  table.appendChild(buildTableHead([stateKey, "count"], [false, true]));
  const tbody = document.createElement("tbody");
  for (const [bucket, count] of Object.entries(counts)) {
    const tr = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.className = "vvv-td";
    const countCell = document.createElement("td");
    countCell.className = "vvv-td vvv-td--num vvv-count";
    nameCell.textContent = toDisplayText(bucket);
    countCell.textContent = toDisplayText(count);
    tr.appendChild(nameCell);
    tr.appendChild(countCell);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  enableColumnResize(table, ctx, "counts");
  return table;
}

/**
 * Bottom pane table: schema columns in payload order, rows newest-first.
 * @returns {HTMLElement}
 */
function buildDetailPane(columns, rows, ctx) {
  const pane = document.createElement("section");
  pane.className = "vvv-pane vvv-pane--detail";
  pane.appendChild(buildDetailTable(columns, rows, ctx));
  return pane;
}

/** Header cells `int|float` → right-aligned, `str` → left-aligned. */
function buildDetailTable(columns, rows, ctx) {
  const table = document.createElement("table");
  table.className = "vvv-table vvv-table--detail";
  table.appendChild(createColgroup(columns.length));
  table.appendChild(
    buildTableHead(
      columns.map((column) => column.name),
      columns.map((column) => isNumericType(column.type)),
    ),
  );
  const tbody = document.createElement("tbody");
  for (const row of rows) {
    const values = Array.isArray(row) ? row : [];
    const tr = document.createElement("tr");
    columns.forEach((column, index) => {
      const td = document.createElement("td");
      const numeric = isNumericType(column.type);
      const highlighted = ctx.numberColumns.has(column.name);
      td.className =
        "vvv-td" +
        (numeric ? " vvv-td--num" : "") +
        (highlighted ? " vvv-num" : "");
      td.textContent = toDisplayText(values[index]);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  enableColumnResize(table, ctx, "detail");
  return table;
}

/** @returns {HTMLTableColElement} Empty colgroup used for explicit widths. */
function createColgroup(count) {
  const colgroup = document.createElement("colgroup");
  for (let index = 0; index < count; index += 1) {
    colgroup.appendChild(document.createElement("col"));
  }
  return colgroup;
}

/** @returns {HTMLElement} Sticky table head for `labels`. */
function buildTableHead(labels, numeric) {
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  labels.forEach((label, index) => {
    const th = document.createElement("th");
    th.className = "vvv-th" + (numeric[index] ? " vvv-th--num" : "");
    th.textContent = toDisplayText(label);
    th.setAttribute("scope", "col");
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  return thead;
}

/**
 * Restore previously dragged widths and attach one resize handle per header.
 * @param {HTMLTableElement} table
 * @param {{ layout: object, redraw: () => void }} ctx
 * @param {"counts" | "detail"} storeKey
 */
function enableColumnResize(table, ctx, storeKey) {
  const colgroup = table.querySelector("colgroup");
  const widths = ctx.layout[storeKey].get(storeKey);
  if (widths && widths.length === colgroup.children.length) {
    applyWidths(table, colgroup, widths);
  }
  const headers = Array.from(table.querySelectorAll("thead th"));
  headers.forEach((header, index) => {
    header.appendChild(buildResizeHandle(table, colgroup, index, ctx, storeKey));
  });
}

/** @returns {HTMLSpanElement} Draggable divider on a header cell. */
function buildResizeHandle(table, colgroup, index, ctx, storeKey) {
  const handle = document.createElement("span");
  handle.className = "vvv-resize";
  handle.title = "拖动调整列宽";
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute("aria-label", "调整列宽");
  handle.addEventListener("pointerdown", (event) => {
    startColumnResize(event, table, colgroup, index, ctx, storeKey, handle);
  });
  return handle;
}

/** Apply an explicit width array to a table and freeze its layout. */
function applyWidths(table, colgroup, widths) {
  table.classList.add("vvv-table--fixed");
  let total = 0;
  Array.from(colgroup.children).forEach((col, index) => {
    col.style.width = `${widths[index]}px`;
    total += widths[index];
  });
  table.style.width = `${total}px`;
}

/** Toggle the root-level resize flag (shadow-DOM scoped cursor/selection lock). */
function setRootResizing(handle, active) {
  const root = handle.closest(".vvv-root");
  if (root) {
    root.classList.toggle("vvv-root--resizing", active);
  }
}

/**
 * Excel-style resize: the dragged divider widens one column and narrows the
 * next; the last column expands the table (horizontal scroll in the pane).
 */
function startColumnResize(event, table, colgroup, index, ctx, storeKey, handle) {  if (event.button !== 0) {
    return;
  }
  event.preventDefault();
  event.stopPropagation();

  const store = ctx.layout[storeKey];
  const headers = Array.from(table.querySelectorAll("thead th"));
  if (headers.length === 0) {
    return;
  }
  const widths = headers.map((header) => header.getBoundingClientRect().width);
  const total = widths.reduce((sum, width) => sum + width, 0);
  applyWidths(table, colgroup, widths);

  ctx.layout.dragging = true;
  setRootResizing(handle, true);
  handle.classList.add("vvv-resize--active");
  if (typeof handle.setPointerCapture === "function") {
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      /* stale pointer id: drag still works through the document listeners */
    }
  }

  const startX = event.clientX;
  const startLeft = widths[index];
  const hasNeighbour = index < widths.length - 1;
  const startRight = hasNeighbour ? widths[index + 1] : 0;
  let lastWidths = widths.slice();

  const onMove = (moveEvent) => {
    const delta = moveEvent.clientX - startX;
    if (hasNeighbour) {
      const left = startLeft + delta;
      const right = startRight - delta;
      if (left < MIN_COLUMN_WIDTH || right < MIN_COLUMN_WIDTH) {
        return;
      }
      widths[index] = left;
      widths[index + 1] = right;
      table.style.width = `${total}px`;
    } else {
      const next = Math.max(MIN_COLUMN_WIDTH, startLeft + delta);
      widths[index] = next;
      table.style.width = `${total + (next - startLeft)}px`;
    }
    Array.from(colgroup.children).forEach((col, i) => {
      col.style.width = `${widths[i]}px`;
    });
    lastWidths = widths.slice();
  };

  const finish = () => {
    handle.removeEventListener("pointermove", onMove);
    handle.removeEventListener("pointerup", finish);
    handle.removeEventListener("pointercancel", finish);
    handle.classList.remove("vvv-resize--active");
    setRootResizing(handle, false);
    store.set(storeKey, lastWidths);
    ctx.layout.dragging = false;
    if (ctx.layout.pending) {
      ctx.layout.pending = false;
      ctx.redraw();
    }
  };

  handle.addEventListener("pointermove", onMove);
  handle.addEventListener("pointerup", finish);
  handle.addEventListener("pointercancel", finish);
}

/** Footer caption, bottom-right small dim text with a green "live" mark. */
function buildFooter(payload, rows) {
  const footer = document.createElement("div");
  footer.className = "vvv-footer";
  const safe = isPayloadObject(payload) ? payload : {};
  const total = toCount(safe.total, "0");
  const windowSize = toCount(safe.window, String(rows.length));
  const label = document.createElement("span");
  label.textContent = `已记录 ${total} 行 / 当前窗口 ${windowSize} 行`;
  const live = document.createElement("span");
  live.className = "vvv-live";
  live.textContent = "live";
  footer.appendChild(label);
  footer.appendChild(live);
  return footer;
}

/** @returns {HTMLElement} Dim text note (graceful empty states). */
function buildNote(text) {
  const note = document.createElement("div");
  note.className = "vvv-note";
  note.textContent = text;
  return note;
}

/**
 * Theme lives on a data attribute for future palettes; the bundled tokens are
 * dark by default (D5), and user overrides are applied as inline CSS vars.
 */
function applyTheme(root, theme) {
  const value = typeof theme === "string" && theme !== "" ? theme : "dark";
  root.setAttribute("data-vvv-theme", value);
}

/**
 * Apply user palette overrides (contract §3.2 ``colors=``). Only the keys
 * present in the mapping are overridden; everything else keeps the bundled
 * defaults. Values were validated as hex strings on the Python side, and are
 * applied with ``setProperty`` (never concatenated into stylesheet text).
 */
function applyColors(root, colors) {
  const overrides = colors !== null && typeof colors === "object" ? colors : {};
  for (const [key, cssVar] of Object.entries(COLOR_VARS)) {
    const value = overrides[key];
    if (typeof value === "string" && value !== "") {
      root.style.setProperty(cssVar, value);
    } else {
      root.style.removeProperty(cssVar);
    }
  }
}

/** @returns {boolean} True when the column type is numeric (right-aligned). */
function isNumericType(type) {
  return type === "int" || type === "float";
}

/** @returns {string} Text for a cell; null/undefined must show as "". */
function toDisplayText(value) {
  return value === null || value === undefined ? "" : String(value);
}

/** @returns {string} Number as text with a fallback for missing fields. */
function toCount(value, fallback) {
  return typeof value === "number" && Number.isFinite(value)
    ? String(value)
    : fallback;
}

/** @returns {boolean} True for non-array JSON objects. */
function isPayloadObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** @returns {void} */
function clearChildren(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

// anywidget contract: the default export is a view definition object whose
// ``render`` method receives ``{ model, el }``. Exporting a bare function here
// makes anywidget invoke it without arguments and breaks model initialisation.
export default { render };
