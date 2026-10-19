"""Contract conformance tests for the ViewWidget recorder (ARCHITECTURE §2/§3.2)."""

from __future__ import annotations

import csv
import threading
import time

import pytest

from viewviewview import ViewWidget
from viewviewview.core import _TailReader


def read_all(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def test_first_update_freezes_schema(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    assert widget.update(state="PASS", delay=1, pulse=2.5, reply="ok") is True
    assert widget.update(state="FAIL", delay=2, pulse=3.5, reply="no") is True
    widget.close()
    rows = read_all(tmp_path / "a.csv")
    assert rows == [
        ["state", "delay", "pulse", "reply"],
        ["PASS", "1", "2.5", "ok"],
        ["FAIL", "2", "3.5", "no"],
    ]


def test_first_update_rejects_empty_and_invalid_types(tmp_path, caplog):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    assert widget.update() is False
    assert widget.update(state="PASS", delay=1, flag=True) is False
    assert widget.update(state="PASS", delay=None) is False
    assert widget.update(state="PASS", delay=object()) is False
    assert widget.rows_written == 0
    assert "record names/types mismatch; supported types: int, float, str" in caplog.text
    assert not (tmp_path / "a.csv").exists()  # never created
    widget.close()


def test_schema_rejects_drift_extras_and_missing_keys(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    assert widget.update(state="PASS", delay=1) is True
    assert widget.update(state="PASS", delay=2.5) is False  # float drift
    assert widget.update(state="PASS", delay=3, extra=1) is False  # extra key
    assert widget.update(state="PASS") is False  # missing key
    assert widget.update(state="PASS", delay=True) is False  # bool drift
    assert widget.rows_written == 1
    widget.close()


def test_key_order_is_irrelevant_but_csv_order_is_frozen(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    assert widget.update(state="PASS", delay=1, pulse=2.5) is True
    assert widget.update(pulse=3.5, state="FAIL", delay=2) is True
    widget.close()
    assert read_all(tmp_path / "a.csv")[2] == ["FAIL", "2", "3.5"]


def test_default_csv_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    widget = ViewWidget()
    assert widget.update(state="PASS", delay=1) is True
    widget.close()
    path = widget.csv_path
    assert path is not None
    assert path.startswith(str(tmp_path))
    assert "viewviewview-" in path and path.endswith(".csv")
    assert read_all(path)[1] == ["PASS", "1"]


def test_properties(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    assert widget.csv_path == str(tmp_path / "a.csv")
    assert widget.rows_written == 0
    widget.update(state="PASS", delay=1)
    assert widget.rows_written == 1
    widget.close()


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"display_limit": -1}, "limit must be a non-negative integer or None"),
        ({"live_rows": -1}, "limit must be a non-negative integer or None"),
        ({"live_rows": "x"}, "limit must be a non-negative integer or None"),
        ({"flush_every": 0}, "flush_every must be a positive integer"),
        ({"flush_every": True}, "flush_every must be a positive integer"),
        ({"fsync": "yes"}, "fsync must be a bool"),
    ],
)
def test_constructor_validation(kwargs, message):
    with pytest.raises(ValueError) as info:
        ViewWidget(**kwargs)
    assert str(info.value) == message


def test_existing_header_mismatch(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"other,columns\r\nx,1\r\n")
    widget = ViewWidget(csv_path=str(path))
    with pytest.raises(ValueError, match="CSV header mismatch"):
        widget.update(state="PASS", delay=1)
    widget.close()


def test_unfinished_last_record(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"state,delay\r\nPASS,1")
    widget = ViewWidget(csv_path=str(path))
    with pytest.raises(ValueError, match="unfinished last record"):
        widget.update(state="PASS", delay=1)
    widget.close()


def test_empty_existing_file_is_headerless(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"")
    widget = ViewWidget(csv_path=str(path))
    assert widget.update(state="PASS", delay=1) is True
    widget.close()
    assert read_all(path) == [["state", "delay"], ["PASS", "1"]]


def test_resume_existing_csv_appends(tmp_path):
    path = tmp_path / "a.csv"
    first = ViewWidget(csv_path=str(path))
    first.update(state="PASS", delay=1)
    first.close()
    second = ViewWidget(csv_path=str(path))
    second.update(state="FAIL", delay=2)
    second.close()
    assert read_all(path) == [
        ["state", "delay"],
        ["PASS", "1"],
        ["FAIL", "2"],
    ]


def test_flush_every_flushes_on_threshold(tmp_path):
    path = tmp_path / "a.csv"
    widget = ViewWidget(csv_path=str(path), flush_every=5)
    for index in range(5):
        widget.update(state="PASS", delay=index)
    # Rows 1-4 are buffered; the fifth write must hit the disk flush.
    assert len(read_all(path)) == 6
    widget.close()


def test_fsync_option(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"), fsync=True, flush_every=1)
    widget.update(state="PASS", delay=1)
    assert widget.rows_written == 1
    widget.close()


def test_context_manager_and_idempotent_close(tmp_path):
    path = tmp_path / "a.csv"
    with ViewWidget(csv_path=str(path)) as widget:
        widget.update(state="PASS", delay=1)
    widget.close()
    widget.close()
    assert read_all(path)[1] == ["PASS", "1"]


def test_io_failure_latches(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path))  # a directory cannot be opened
    with pytest.raises(OSError):
        widget.update(state="PASS", delay=1)
    with pytest.raises(RuntimeError, match="CSV recording previously failed"):
        widget.update(state="PASS", delay=2)


def test_thread_safety(tmp_path):
    path = tmp_path / "a.csv"
    widget = ViewWidget(csv_path=str(path))
    errors = []

    def worker(worker_id):
        try:
            for index in range(200):
                if not widget.update(state="PASS", seq=worker_id * 1000 + index):
                    errors.append("rejected")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert widget.rows_written == 1600
    widget.close()
    assert len(read_all(path)) == 1601


def test_csv_only_mode(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"), live_rows=0)
    for index in range(25):
        widget.update(state="PASS", delay=index)
    widget.close()
    assert widget.rows_written == 25


def test_ring_window_is_bounded(tmp_path):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"), live_rows=10)
    for index in range(100):
        widget.update(state="PASS", delay=index)
    assert len(widget._ring) == 10
    assert widget._ring[-1] == ("PASS", 99)
    widget.close()


def test_tail_reader_window_and_malformed(tmp_path):
    path = tmp_path / "a.csv"
    path.write_bytes(b"a,b\r\n1,2\r\n3,4\r\n5,6\r\n")
    header, rows = _TailReader(str(path)).read(2)
    assert header == ["a", "b"]
    assert rows == [["3", "4"], ["5", "6"]]
    malformed = tmp_path / "bad.csv"
    malformed.write_bytes(b"a,b\r\n1,2\r\n3\r\n")
    with pytest.raises(ValueError, match="Malformed CSV record 2"):
        _TailReader(str(malformed)).read(None)


def test_show_builds_contract_payload(tmp_path, monkeypatch):
    import viewviewview.core as core

    captured = {}
    monkeypatch.setattr(
        core,
        "render_payload",
        lambda payload, on_created=None: captured.update({"payload": payload}),
    )
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    for index in range(10):
        widget.update(state=("PASS", "FAIL")[index % 2], delay=index, pulse=float(index))
    widget.show()
    payload = captured["payload"]
    assert payload["state_key"] == "state"
    assert payload["counts"] == {"PASS": 5, "FAIL": 5}
    assert payload["rows"][0][1] == 9  # newest first
    assert payload["rows"][-1][1] == 0
    assert payload["total"] == 10
    assert payload["window"] == 10
    assert payload["columns"][1] == {"name": "delay", "type": "int"}
    widget.close()


def test_show_limit_zero_is_counts_only(tmp_path, monkeypatch):
    import viewviewview.core as core

    captured = {}
    monkeypatch.setattr(
        core,
        "render_payload",
        lambda payload, on_created=None: captured.update({"payload": payload}),
    )
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    widget.update(state="PASS", delay=1)
    widget.show(limit=0)
    assert captured["payload"]["rows"] == []
    assert captured["payload"]["counts"] == {"PASS": 1}
    widget.close()


def test_show_without_state_column(tmp_path, monkeypatch):
    import viewviewview.core as core

    captured = {}
    monkeypatch.setattr(
        core,
        "render_payload",
        lambda payload, on_created=None: captured.update({"payload": payload}),
    )
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    widget.update(delay=1, pulse=2.5)
    widget.show()
    assert captured["payload"]["state_key"] is None
    assert captured["payload"]["counts"] == {}
    widget.close()


def test_show_no_data_logs(tmp_path, caplog):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    widget.show()
    assert "No data recorded." in caplog.text
    widget.close()


def test_show_outside_ipython_warns(tmp_path, caplog):
    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    widget.update(state="PASS", delay=1)
    widget.show()
    assert "No IPython display environment available" in caplog.text
    widget.close()


def test_syncer_pushes_to_panels(tmp_path):
    class Panel:
        payload = None

    widget = ViewWidget(csv_path=str(tmp_path / "a.csv"))
    panel = Panel()
    widget.syncer.attach(panel)
    widget.update(state="PASS", delay=1)
    widget.show()  # freezes the snapshot selection
    deadline = time.time() + 2.0
    while panel.payload is None and time.time() < deadline:
        widget.update(state="PASS", delay=2)
        time.sleep(0.03)
    assert panel.payload is not None
    assert panel.payload["total"] >= 1
    widget.close()
    assert widget.syncer.panel_count() == 0
