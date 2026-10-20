"""Core recorder widget, tail scanner and sync machinery.

Implements the ``ViewWidget`` data recorder (architecture contract Section 3.2),
the ``_TailReader`` CSV tail-window scanner, and the ``Syncer`` frame-merging
push service (Section 4.1).  This module owns the authoritative CSV channel and
the in-memory channel (counts + ring buffer, Section 2); display work lives in
``viewviewview.panel`` (member B) and is only invoked lazily from here.
"""

from __future__ import annotations

import contextlib
import csv
import logging
import os
import re
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

__all__ = [
    "ViewWidget",
    "Syncer",
    "_TailReader",
    "render_payload",
]

_LOGGER = logging.getLogger("viewviewview")

_DEFAULT_RING_DEPTH = 2000   # default memory window cap when live_rows is None
_DEFAULT_DETAIL_ROWS = 200   # default payload detail depth (contract 4.3)
_MAX_WINDOW_ROWS = 2000      # payload rows hard cap
_FRAME_DELAY = 0.05          # Syncer frame-merge delay in seconds (4.1)

_TYPE_NAMES: Dict[type, str] = {int: "int", float: "float", str: "str"}

_DEFAULT_CSV_FORMAT = "%Y%m%d-%H%M%S"  # start-time based default file name

# User-customizable palette entries (API key -> CSS variable in the frontend).
_COLOR_KEYS = frozenset(
    {
        "text",
        "count",
        "number",
        "muted",
        "background",
        "panel",
        "header",
        "row_alt",
        "accent",
        "border",
        "live",
    }
)
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Columns that use the "number" color (#19ff25) by default.
_DEFAULT_NUMBER_COLUMNS = ("delay", "pulse")


def _normalize_number_columns(value: Any) -> List[str]:
    """Validate ``number_columns=`` (columns rendered with the number color)."""
    if value is None:
        return list(_DEFAULT_NUMBER_COLUMNS)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("number_columns must be a sequence of column names")
    names: List[str] = []
    for name in value:
        if not isinstance(name, str) or not name:
            raise ValueError("number_columns entries must be non-empty strings")
        names.append(name)
    return names


def _normalize_colors(colors: Any) -> Dict[str, str]:
    """Validate the optional ``colors=`` palette overrides (contract §3.2).

    Values are hex strings (``#rgb`` or ``#rrggbb``) applied as CSS variables by
    the frontend, so they cannot inject arbitrary stylesheet text.
    """
    if colors is None:
        return {}
    if not isinstance(colors, Mapping):
        raise ValueError("colors must be a mapping of color names to hex values")
    normalized: Dict[str, str] = {}
    for key, value in colors.items():
        if key not in _COLOR_KEYS:
            supported = ", ".join(sorted(_COLOR_KEYS))
            raise ValueError(f"unknown color key: {key!r}; supported: {supported}")
        if not isinstance(value, str) or not _HEX_COLOR.match(value):
            raise ValueError(
                f"color values must be hex strings like #19ff25 (got {value!r})"
            )
        normalized[key] = value
    return normalized


def _normalize_columns(columns: Any) -> Tuple[List[str], List[Optional[type]]]:
    """Validate a user supplied column definition (constructor ``columns=``).

    Accepts either a mapping ``{name: int|float|str}`` (explicit types) or a
    sequence of names (types are adopted from the first accepted record).
    Column order is preserved; duplicate/empty/non-str names are rejected.
    """
    if isinstance(columns, Mapping):
        names = list(columns.keys())
        types: List[Optional[type]] = []
        for name, value_type in columns.items():
            if not isinstance(name, str) or not name:
                raise ValueError("column names must be non-empty strings")
            if value_type not in _TYPE_NAMES:
                raise ValueError("column types must be exactly int, float or str")
            types.append(value_type)
        if not names:
            raise ValueError("columns must not be empty")
        return names, types

    if isinstance(columns, (str, bytes)):
        raise ValueError(
            "columns must be a sequence of names or a mapping name -> int/float/str"
        )
    if not isinstance(columns, Sequence):
        raise ValueError(
            "columns must be a sequence of names or a mapping name -> int/float/str"
        )
    names = list(columns)
    if not names:
        raise ValueError("columns must not be empty")
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError("column names must be non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError("columns must not contain duplicate names")
    return names, [None] * len(names)


def _validate_limit(limit: Any) -> None:
    """Validate ``limit``/``live_rows`` style arguments (contract 3.2)."""
    if limit is None:
        return
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer or None")


def _validate_plot_limit(limit: Any) -> None:
    """Validate ``csvview``/``csvplot`` window sizes (contract 3.3)."""
    _validate_limit(limit)


def _validate_flush_every(flush_every: Any) -> None:
    if type(flush_every) is not int or flush_every <= 0:
        raise ValueError("flush_every must be a positive integer")


def _validate_fsync(fsync: Any) -> None:
    if type(fsync) is not bool:
        raise ValueError("fsync must be a bool")


def _ipython_available() -> bool:
    """Return True when running inside a live IPython/Jupyter kernel."""
    try:
        from IPython import get_ipython
    except ImportError:
        return False
    try:
        return get_ipython() is not None
    except Exception:  # noqa: BLE001 - probing third party internals
        _LOGGER.debug("IPython probe failed", exc_info=True)
        return False


def render_payload(
    payload: Dict[str, Any],
    colors: Optional[Dict[str, str]] = None,
    number_columns: Optional[Sequence[str]] = None,
    on_created: Optional[Any] = None,
) -> None:
    """Display one payload through the panel frontend in an IPython session.

    Never raises; failures are logged so recording always outlives display.
    ``colors`` carries user palette overrides and ``number_columns`` selects
    which detail columns use the number color (contract §4.2).
    ``on_created(panel)`` (when provided) runs after a successful panel
    construction and before ``display`` so callers can register the panel
    with their own syncer.
    """
    if not _ipython_available():
        _LOGGER.warning("No IPython display environment available")
        return
    try:
        from IPython.display import display

        from viewviewview.panel import VvvPanel  # type: ignore[attr-defined]
    except ImportError:
        _LOGGER.exception("Panel frontend unavailable; payload not displayed")
        return
    try:
        panel = VvvPanel(
            payload=payload,
            colors=colors or {},
            number_columns=list(number_columns or []),
        )
        if on_created is not None:
            on_created(panel)
        display(panel)
    except Exception:  # noqa: BLE001 - display boundary must not propagate
        _LOGGER.exception("Panel display failed")


class _TailReader:
    """Streaming tail-window reader for a UTF-8 CSV file (contract 3.3).

    Provides O(limit) memory scanning: the whole file is streamed once while
    only the last ``limit`` records are retained. Records are returned in
    file order (oldest to newest).
    """

    def __init__(self, path: str, logger: Optional[logging.Logger] = None) -> None:
        self._path = path
        self._logger = logger or _LOGGER

    def read(self, limit: Optional[int]) -> Tuple[List[str], List[List[str]]]:
        """Return ``(header, rows)``; rows are the last ``limit`` records.

        A malformed record (field count differing from the header) raises
        ``ValueError(f"Malformed CSV record {i} in {path}")`` where ``i`` is
        the 0-based data record index (header not counted).
        """
        if limit is None:
            rows: List[List[str]] = []
            with open(self._path, "r", encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                try:
                    header = next(reader)
                except StopIteration:
                    return [], []
                for index, record in enumerate(reader, start=1):
                    if len(record) != len(header):
                        raise ValueError(
                            f"Malformed CSV record {index} in {self._path}"
                        )
                    rows.append(record)
            return header, rows

        kept: Deque[List[str]] = deque(maxlen=limit)
        with open(self._path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                return [], []
            for index, record in enumerate(reader, start=1):
                if len(record) != len(header):
                    raise ValueError(f"Malformed CSV record {index} in {self._path}")
                if limit > 0:
                    kept.append(record)
        return header, list(kept)


class Syncer:
    """Frame-merging push service between the recorder and panels (4.1).

    Combine frequent ``wake()`` calls into a single payload refresh roughly
    every 50 ms. Never blocks the recorder and never raises into it; on
    internal errors the failure is logged at DEBUG level only.
    """

    def __init__(self, snapshot_provider: Any, logger: Optional[logging.Logger] = None) -> None:
        """Create a syncer.

        ``snapshot_provider()`` must return a fresh payload dict; it is only
        called when at least one panel is attached (zero overhead otherwise).
        """
        self._snapshot_provider = snapshot_provider
        self._logger = logger or _LOGGER
        self._panels_lock = threading.Lock()
        self._panels: List[Any] = []
        self._timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------
    def attach(self, panel: Any) -> None:
        """Start pushing snapshot payloads to ``panel``."""
        with self._panels_lock:
            if panel not in self._panels:
                self._panels.append(panel)

    def detach(self, panel: Any) -> None:
        """Stop pushing payloads to ``panel`` (idempotent)."""
        with self._panels_lock, contextlib.suppress(ValueError):
            self._panels.remove(panel)

    def detach_all(self) -> None:
        """Detach every panel and cancel any pending timer."""
        with self._panels_lock:
            self._panels.clear()
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def has_panels(self) -> bool:
        """Return True when at least one panel is attached."""
        with self._panels_lock:
            return bool(self._panels)

    def panel_count(self) -> int:
        """Return the number of attached panels (diagnostics/testing)."""
        with self._panels_lock:
            return len(self._panels)

    # ------------------------------------------------------------------
    # Push path
    # ------------------------------------------------------------------
    def wake(self) -> None:
        """Schedule a payload refresh at most one frame into the future."""
        try:
            with self._panels_lock:
                if not self._panels:
                    return
                if self._timer is not None:
                    return
                timer = threading.Timer(_FRAME_DELAY, self._refresh)
                timer.daemon = True
                self._timer = timer
            timer.start()
        except Exception:  # noqa: BLE001 - syncer must never break update()
            self._logger.debug("Syncer wake failed", exc_info=True)

    def _refresh(self) -> None:
        try:
            with self._panels_lock:
                self._timer = None
                panels = list(self._panels)
            if not panels:
                return
            payloads = self._snapshot_provider()
            if payloads is None:
                return
            for panel in panels:
                panel.payload = payloads
        except Exception:  # noqa: BLE001 - syncer must never break update()
            self._logger.debug("Syncer refresh failed", exc_info=True)


class ViewWidget:
    """Live data recorder with a compatible flat CSV channel.

    Semantics follow the architecture contract Section 3.2: first accepted
    ``update`` call freezes the exclusive schema (insertion order, exact
    ``int``/``float``/``str`` types), subsequent records must match it exactly,
    every accepted record lands on CSV first (authoritative), then in the
    in-memory counts/ring channel, then notifies the :class:`Syncer`.
    """

    def __init__(
        self,
        csv_path: Optional[str] = None,
        display_limit: Optional[int] = None,
        flush_every: int = 1,
        live_rows: Optional[int] = None,
        fsync: bool = False,
        columns: Optional[Any] = None,
        colors: Optional[Mapping[str, str]] = None,
        number_columns: Optional[Sequence[str]] = None,
    ) -> None:
        _validate_limit(display_limit)
        _validate_limit(live_rows)
        _validate_flush_every(flush_every)
        _validate_fsync(fsync)
        if columns is None:
            schema_names: List[str] = []
            schema_types: List[Optional[type]] = []
        else:
            schema_names, schema_types = _normalize_columns(columns)
        self._colors: Dict[str, str] = _normalize_colors(colors)
        self._number_columns: List[str] = _normalize_number_columns(number_columns)

        self._lock = threading.RLock()
        self._logger = _LOGGER
        self._csv_path: str = (
            os.path.abspath(os.path.expanduser(csv_path))
            if csv_path is not None
            else self._new_default_csv_path()
        )
        self._display_limit: Optional[int] = display_limit
        self._flush_every: int = flush_every
        self._live_rows: Optional[int] = live_rows
        self._fsync: bool = bool(fsync)

        if live_rows is None:
            ring_depth = _DEFAULT_RING_DEPTH
        elif live_rows > 0:
            ring_depth = live_rows
        else:
            ring_depth = 0
        self._ring_max: int = ring_depth
        self._ring: Deque[Tuple[Any, ...]] = deque(maxlen=ring_depth) if ring_depth > 0 else deque()

        self._schema: List[Tuple[str, Optional[type]]] = list(
            zip(schema_names, schema_types)
        )
        self._schema_names: List[str] = schema_names
        self._schema_types: List[Optional[type]] = schema_types
        self._pending_types: bool = any(value_type is None for value_type in schema_types)
        self._counts: Dict[str, Dict[str, int]] = {}
        self._rows_written: int = 0
        self._rows_since_flush: int = 0
        self._version: int = 0

        self._csv_failed: bool = False
        self._closed: bool = False
        self._csv_file: Optional[Any] = None
        self._csv_writer: Optional[Any] = None

        # Panel push state (last show() selection wins for later frames).
        self._selected_state: str = "state"
        self._selected_depth: Optional[int] = None

        self._syncer = Syncer(self._build_live_snapshot, _LOGGER)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------
    @property
    def csv_path(self) -> Optional[str]:
        """Absolute CSV path, fixed at widget creation (start time by default)."""
        with self._lock:
            return self._csv_path

    @property
    def rows_written(self) -> int:
        """Number of records accepted by this widget so far."""
        with self._lock:
            return self._rows_written

    @property
    def _column(self) -> Dict[str, Optional[type]]:
        """FaultViz compatibility shim: column name -> frozen type.

        FaultViz exposes ``_column`` as a plain dict after the first row and
        some experiment scripts read it to detect schema changes. ViewViewView
        keeps the authoritative schema in ``_schema_names``/``_schema_types``;
        this read-only snapshot mirrors it (``None`` means a pre-declared
        column whose type is still pending the first record).
        """
        with self._lock:
            return dict(zip(self._schema_names, self._schema_types))

    @property
    def display_limit(self) -> Optional[int]:
        """Configured live detail row limit (diagnostics)."""
        return self._display_limit

    @property
    def syncer(self) -> Syncer:
        """Frame merging push service (contract 4.1)."""
        return self._syncer

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def update(self, **kwargs: Any) -> bool:
        """Record one row. Returns True when accepted, False when rejected.

        Failures log the contract error phrase and return False without
        touching the CSV channel or in-memory channel. Fatal recording
        errors raise; afterwards the widget refuses further updates.
        """
        with self._lock:
            if self._csv_failed:
                raise RuntimeError("CSV recording previously failed; create a new widget")

            if not self._schema_names:
                # Legacy mode: freeze the schema from the first valid record.
                # Reject empty kwargs and any non-exact int/float/str value
                # (bool, numpy scalars, None, ...) before touching CSV/memory.
                if not kwargs or any(type(value) not in _TYPE_NAMES for value in kwargs.values()):
                    self._logger.error(
                        "record names/types mismatch; supported types: int, float, str"
                    )
                    return False
                self._schema_names = list(kwargs)
                self._schema_types = [type(value) for value in kwargs.values()]
                self._schema = list(zip(self._schema_names, self._schema_types))
                self._pending_types = False
            else:
                if not self._row_matches_schema(kwargs):
                    self._logger.error(
                        "record names/types mismatch; supported types: int, float, str"
                    )
                    return False
                if self._pending_types:
                    # Names were declared up front; adopt exact types now.
                    self._schema_types = [
                        type(kwargs[name]) for name in self._schema_names
                    ]
                    self._schema = list(zip(self._schema_names, self._schema_types))
                    self._pending_types = False

            self._write_record_locked(kwargs)

            values = [kwargs[name] for name in self._schema_names]
            self._rows_written += 1
            self._record_in_memory_locked(values)
            self._syncer.wake()
            return True

    def flush(self) -> None:
        """Flush buffered records to disk; honorable fsync (contract 3.2)."""
        with self._lock:
            if self._csv_failed:
                raise RuntimeError("CSV recording previously failed; create a new widget")
            self._flush_file_locked()

    def close(self) -> None:
        """Flush pending records, detach panels and release the CSV handle.

        Idempotent and safe to call from within a context manager exit or a
        worker thread.
        """
        with self._lock:
            if self._closed and self._csv_file is None:
                return
            if not self._csv_failed:
                with contextlib.suppress(Exception):
                    self._flush_file_locked()
            self._syncer.detach_all()
            handle, self._csv_file, self._csv_writer = self._csv_file, None, None
            self._closed = True
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - close silently
                self._logger.debug("Closing CSV handle failed", exc_info=True)

    def __enter__(self) -> "ViewWidget":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    def show(self, state: str = "state", limit: Optional[int] = None, full: bool = False) -> None:
        """Display the live payload in a new panel (contract 3.2)."""
        _validate_limit(limit)
        with self._lock:
            if self._rows_written == 0 and not self._schema_names:
                _LOGGER.info("No data recorded.")
                return
            if self._rows_written == 0:
                _LOGGER.info("No data recorded yet; showing the declared columns.")
            self._selected_state = state
            self._selected_depth = self._resolve_depth(limit, full)
            payload = self._build_payload_locked(state, self._selected_depth)
        render_payload(
            payload,
            colors=self._colors,
            number_columns=self._number_columns,
            on_created=self._syncer.attach,
        )

    def plot(self, state: str = "state", limit: Optional[int] = None, full: bool = False) -> None:
        """Visualise the current window via pygwalker or the CSV fallback (3.2)."""
        _validate_limit(limit)
        with self._lock:
            if self._rows_written == 0:
                _LOGGER.info("No data recorded.")
                return
            depth = self._resolve_depth(limit, full)
            # CSV-only mode, counts-only windows and no attached panels all
            # degrade to the historical CSV plot path.
            use_csv = self._live_rows == 0 or depth == 0 or not self._syncer.has_panels()
            if use_csv:
                self._flush_file_locked()
                csv_target = self._csv_path
            else:
                names = list(self._schema_names)
                rows = self._ring_window_locked(depth)
        if use_csv:
            import importlib

            compat = importlib.import_module("viewviewview.compat")
            compat.csvplot(csv_target, limit=limit, full=full)
            return
        try:
            import pandas as pd
            import pygwalker as pyg
        except ImportError:
            import importlib

            compat = importlib.import_module("viewviewview.compat")
            compat.csvplot(self._csv_path, limit=limit, full=full)
            return
        try:
            pyg.walk(pd.DataFrame(rows, columns=names))
        except Exception:  # noqa: BLE001 - plot boundary must never raise
            _LOGGER.debug("pygwalker walk failed", exc_info=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _row_matches_schema(self, kwargs: Dict[str, Any]) -> bool:
        """Exact key set and exact value types; key order is irrelevant.

        FaultViz accepts the same keys in any insertion order, and CSV output
        follows the frozen schema order regardless, so parity requires an
        order-insensitive comparison here.
        """
        if len(kwargs) != len(self._schema_names):
            return False
        for name, expected_type in self._schema:
            if name not in kwargs:
                return False
            value = kwargs[name]
            if expected_type is None:
                # Sequence-declared columns: any exact int/float/str is fine
                # for this first record; the types are adopted afterwards.
                if type(value) not in _TYPE_NAMES:
                    return False
            elif type(value) is not expected_type:
                return False
        return True

    def _record_in_memory_locked(self, values: List[Any]) -> None:
        """Update counts (str columns only) and the ring buffer (contract 2)."""
        for index, (name, value_type) in enumerate(self._schema):
            if value_type is not str:
                continue
            column_counts = self._counts.setdefault(name, {})
            key = str(values[index])
            column_counts[key] = column_counts.get(key, 0) + 1
        self._ring.append(tuple(values))

    def _write_record_locked(self, kwargs: Dict[str, Any]) -> None:
        """Append one record to the CSV channel (authoritative, step 5)."""
        values = [kwargs[name] for name in self._schema_names]
        try:
            if self._csv_file is None:
                self._open_csv_locked()
            self._csv_writer.writerow(values)
            self._rows_since_flush += 1
            if self._rows_since_flush >= self._flush_every:
                self._flush_file_locked()
        except Exception:
            self._csv_failed = True
            self._logger.exception("CSV recording failed; stopping instead of dropping results")
            raise

    def _open_csv_locked(self) -> None:
        """Lazily open (and validate) the CSV channel. Raises ValueError/OSError."""
        path = self._csv_path
        if path is None:  # defensive; the path is fixed at construction
            path = self._new_default_csv_path()
            self._csv_path = path
        had_records = self._validate_existing_csv_locked(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(path, "a", encoding="utf-8", newline="")
        self._csv_file = handle
        self._csv_writer = csv.writer(handle)
        if not had_records:
            self._csv_writer.writerow(self._schema_names)

    def _new_default_csv_path(self) -> str:
        """Collision-safe absolute CSV name based on the start time (3.2/§5)."""
        stamp = datetime.now().strftime(_DEFAULT_CSV_FORMAT)
        directory = os.getcwd()
        candidate = os.path.join(directory, f"{stamp}.csv")
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(directory, f"{stamp}-{counter}.csv")
            counter += 1
        return candidate

    def _validate_existing_csv_locked(self, path: str) -> bool:
        """Inspect an existing CSV before resuming; returns True when it holds data.

        Raises ``ValueError`` (headers/unfinished record) without poisoning the
        widget so the user may repair the file and retry.
        """
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return False
                handle.seek(0)
                head = handle.readline(65536)
                handle.seek(-1, os.SEEK_END)
                last_byte = handle.read(1)
        except FileNotFoundError:
            return False
        if not head:
            return False
        reader = csv.reader([head.decode("utf-8")])
        header = next(reader, [])
        if header != self._schema_names:
            raise ValueError(
                f"CSV header mismatch for {path}: "
                f"expected {self._schema_names!r}, found {header!r}"
            )
        if last_byte != b"\n":
            raise ValueError(
                f"CSV has an unfinished last record: {path}; "
                "inspect the file before resuming or choose a new path"
            )
        return True

    def _flush_file_locked(self) -> None:
        """Write-through pending bytes; I/O trouble marks the CSV channel fatal."""
        if self._csv_file is None:
            return
        try:
            self._csv_file.flush()
            if self._fsync:
                os.fsync(self._csv_file.fileno())
            self._rows_since_flush = 0
        except Exception:
            self._csv_failed = True
            self._logger.exception("CSV recording failed; stopping instead of dropping results")
            raise

    def _ring_window_locked(self, depth: int) -> List[List[Any]]:
        """Newest-first copy of the last ``depth`` ring rows."""
        if depth <= 0 or not self._ring:
            return []
        window = list(self._ring)[-depth:]
        return [list(row) for row in reversed(window)]

    def _resolve_depth(self, limit: Optional[int], full: bool) -> int:
        """Effective detail depth for a show()/plot() call (contract 3.2/4.3)."""
        if limit is not None:
            chosen = limit
        elif full:
            chosen = min(self._ring_max, _MAX_WINDOW_ROWS)
        elif self._display_limit is not None:
            chosen = self._display_limit
        elif self._live_rows is not None and self._live_rows > 0:
            chosen = self._live_rows
        elif self._live_rows == 0:
            chosen = 0
        else:
            chosen = _DEFAULT_DETAIL_ROWS
        return min(chosen, _MAX_WINDOW_ROWS)

    def _counts_for_state_locked(self, state: str) -> Tuple[Optional[str], Dict[str, int]]:
        """Resolve the payload state column selection (contract 3.2/4.3)."""
        if state in self._counts:
            return state, dict(self._counts[state])
        return None, {}

    def _build_payload_locked(self, state_key: Any, depth: int) -> Dict[str, Any]:
        """Snapshot payload per the frozen contract Section 4.3."""
        columns = [
            {"name": name, "type": _TYPE_NAMES.get(value_type, "str")}
            for name, value_type in zip(self._schema_names, self._schema_types)
        ]
        state_value: Optional[str] = None
        counts: Dict[str, int] = {}
        if isinstance(state_key, str) and state_key in self._schema_names:
            index = self._schema_names.index(state_key)
            column_type = self._schema_types[index]
            if column_type is str or column_type is None:
                state_value = state_key
                counts = dict(self._counts.get(state_key, {}))
        rows = self._ring_window_locked(depth)
        self._version += 1
        return {
            "columns": columns,
            "state_key": state_value,
            "counts": counts,
            "rows": rows,
            "total": self._rows_written,
            "window": len(rows),
            "version": self._version,
        }

    def _build_live_snapshot(self) -> Optional[Dict[str, Any]]:
        """Latest state snapshot for the Syncer (contract 4.1/4.3)."""
        with self._lock:
            if self._selected_depth is None:
                return None
            return self._build_payload_locked(self._selected_state, self._selected_depth)
