/*
 * ViewViewView dual-pane renderer — ARCHITECTURE.md §4.3 / §4.4.
 * Pure single-file ESM, zero npm dependencies, no imports at all.
 *
 * Injection guard (§4.4): payload strings are untrusted user data, so the DOM
 * is built exclusively with createElement/textContent — innerHTML is banned.
 */

/**
 * Default anywidget entry point: render the current payload snapshot and keep
 * the DOM in sync with subsequent `change:payload` / `change:theme` pushes.
 * All rendering is a bounded full redraw (<=200 rows) via DocumentFragment.
 *
 * @param {{
 *   get: (key: string) => unknown,
 *   on: (name: string, callback: () => void) => void,
 *   off?: (name: string, callback: () => void) => void,
 * }} model
 * @param {HTMLElement} el Shadow-DOM element owned by anywidget.
 * @returns {() => void} Teardown function, called by anywidget on view removal.
 */
export default function render({ model, el }) {
  const root = document.createElement("div");
  root.className = "vvv-root";
  applyTheme(root, model.get("theme"));
  el.appendChild(root);
  renderPayload(root, model.get("payload"));

  const onPayloadChange = () => renderPayload(root, model.get("payload"));
  const onThemeChange = () => applyTheme(root, model.get("theme"));
  model.on("change:payload", onPayloadChange);
  model.on("change:theme", onThemeChange);

  return () => {
    if (typeof model.off === "function") {
      model.off("change:payload", onPayloadChange);
      model.off("change:theme", onThemeChange);
    }
  };
}

/**
 * Rebuild the two panes from one §4.3 payload snapshot. The payload may be
 * absent/partial while the widget is being wired, so every access is defensive.
 * @param {HTMLElement} root
 * @param {unknown} payload
 * @returns {void}
 */
function renderPayload(root, payload) {
  clearChildren(root);
  const columns = readColumns(payload);
  const counts = readCounts(payload);
  const rows = readRows(payload);
  const hasCounts = Object.keys(counts).length > 0;
  if (columns.length === 0 && rows.length === 0 && !hasCounts) {
    root.appendChild(buildNote("No data recorded."));
    return;
  }

  const stateKey = readStateKey(payload);
  root.appendChild(buildCountsPane(stateKey, counts));

  // §3.2 limit=0 → counts-only payload (no detail pane); rows arrive
  // newest-first already (§4.3), so render them in array order as-is.
  if (columns.length > 0 && rows.length > 0) {
    root.appendChild(buildDetailPane(columns, rows));
  }

  root.appendChild(buildFooter(payload, rows));
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
 * @returns {HTMLElement}
 */
function buildCountsPane(stateKey, counts) {
  const pane = document.createElement("section");
  pane.className = "vvv-pane vvv-pane--counts";
  if (stateKey === null) {
    pane.appendChild(buildNote("No state column"));
  } else {
    pane.appendChild(buildCountsTable(stateKey, counts));
  }
  return pane;
}

/**
 * Top pane table: one row per state bucket, header `[state_key | count]`.
 * Bucket order = payload dict order (no re-sorting; see OPEN_QUESTIONS B2).
 */
function buildCountsTable(stateKey, counts) {
  const table = document.createElement("table");
  table.className = "vvv-table vvv-table--counts";
  table.appendChild(buildTableHead([stateKey, "count"], [false, true]));
  const tbody = document.createElement("tbody");
  for (const [bucket, count] of Object.entries(counts)) {
    const tr = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.className = "vvv-td";
    const countCell = document.createElement("td");
    countCell.className = "vvv-td vvv-td--num";
    nameCell.textContent = toDisplayText(bucket);
    countCell.textContent = toDisplayText(count);
    tr.appendChild(nameCell);
    tr.appendChild(countCell);
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

/**
 * Bottom pane table: schema columns in payload order, rows newest-first.
 * @returns {HTMLElement}
 */
function buildDetailPane(columns, rows) {
  const pane = document.createElement("section");
  pane.className = "vvv-pane vvv-pane--detail";
  pane.appendChild(buildDetailTable(columns, rows));
  return pane;
}

/** Header cells `int|float` → right-aligned, `str` → left-aligned. */
function buildDetailTable(columns, rows) {
  const table = document.createElement("table");
  table.className = "vvv-table vvv-table--detail";
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
      td.className = "vvv-td" + (isNumericType(column.type) ? " vvv-td--num" : "");
      td.textContent = toDisplayText(values[index]);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
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
 * Theme lives on a data attribute for future palettes; CSS tokens (§4.4) are
 * dark-only for now, so any non-"dark" value still renders dark (B1).
 */
function applyTheme(root, theme) {
  const value = typeof theme === "string" && theme !== "" ? theme : "dark";
  root.setAttribute("data-vvv-theme", value);
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
