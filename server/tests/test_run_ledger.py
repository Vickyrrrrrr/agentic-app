"""Ledger tests: sqlite run-event store, hermetic (tmp_path)."""
from agentic_server import run_ledger


def test_append_read_roundtrip(tmp_path):
    db = run_ledger.db_path_for(tmp_path)
    run_ledger.append_event(db, {"run_id": "r1", "type": "progress", "label": "step one"})
    run_ledger.append_event(db, {"run_id": "r1", "type": "response", "label": "done"})
    events = run_ledger.read_events(db)
    assert len(events) == 2
    assert [e["label"] for e in events] == ["step one", "done"]
    assert all("timestamp" in e for e in events)


def test_null_keys_dropped_like_jsonl_envelope():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        db = run_ledger.db_path_for(d)
        run_ledger.append_event(db, {"type": "progress"})
        (event,) = run_ledger.read_events(db)
        assert "run_id" not in event
        assert event["type"] == "progress"


def test_run_id_filter_and_limit(tmp_path):
    db = run_ledger.db_path_for(tmp_path)
    for i in range(5):
        run_ledger.append_event(db, {"run_id": "a", "type": "progress", "label": f"a{i}"})
    for i in range(3):
        run_ledger.append_event(db, {"run_id": "b", "type": "progress", "label": f"b{i}"})
    only_b = run_ledger.read_events(db, run_id="b")
    assert [e["label"] for e in only_b] == ["b0", "b1", "b2"]
    last_two = run_ledger.read_events(db, limit=2)
    assert [e["label"] for e in last_two] == ["b1", "b2"]


def test_retention_cap_prunes_oldest(tmp_path):
    db = run_ledger.db_path_for(tmp_path)
    for i in range(12):
        run_ledger.append_event(db, {"run_id": "r", "label": f"e{i}"}, cap=10)
    events = run_ledger.read_events(db, limit=50)
    assert [e["label"] for e in events] == [f"e{i}" for i in range(2, 12)]


def test_main_wires_ledger_with_jsonl_fallback():
    from agentic_server import main

    assert main.run_ledger is run_ledger
    assert callable(main._append_run_event)
    assert callable(main._read_run_events)
