"""Offline tests for the XE capture/replay driver's pure parsing/scheduling."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "workload-drivers"))

import capture_replay  # noqa: E402


def test_parse_events_extracts_rpc_completed():
    xml = """
    <RingBufferTarget>
      <event name="rpc_completed" timestamp="2026-07-02T08:00:00.000Z">
        <data name="statement"><value>EXEC dbo.p @a=1;</value></data>
      </event>
      <event name="sql_batch_completed" timestamp="2026-07-02T08:00:01.000Z">
        <data name="batch_text"><value>SELECT 1;</value></data>
      </event>
      <event name="rpc_completed" timestamp="2026-07-02T08:00:02.000Z">
        <data name="statement"><value>EXEC dbo.p @a=2;</value></data>
      </event>
    </RingBufferTarget>
    """
    calls = capture_replay.parse_events(xml)
    assert len(calls) == 2
    assert calls[0]["statement"] == "EXEC dbo.p @a=1;"
    assert calls[1]["ts"] == "2026-07-02T08:00:02.000Z"


def test_load_calls_dedupes_and_sorts(tmp_path):
    p = tmp_path / "calls.jsonl"
    p.write_text(
        '{"ts": "2026-07-02T08:00:05.000Z", "statement": "EXEC p 2;"}\n'
        '{"ts": "2026-07-02T08:00:01.000Z", "statement": "EXEC p 1;"}\n'
        '{"ts": "2026-07-02T08:00:05.000Z", "statement": "EXEC p 2;"}\n'
    )
    calls = capture_replay.load_calls(str(p))
    assert len(calls) == 2
    assert calls[0]["statement"] == "EXEC p 1;"


def test_schedule_offsets_original_and_compressed():
    calls = [
        {"ts": "2026-07-02T08:00:00.000Z", "statement": "a"},
        {"ts": "2026-07-02T08:00:10.000Z", "statement": "b"},
        {"ts": "2026-07-02T08:00:30.000Z", "statement": "c"},
    ]
    assert capture_replay.schedule_offsets(calls, 1.0) == [0.0, 10.0, 30.0]
    assert capture_replay.schedule_offsets(calls, 10.0) == [0.0, 1.0, 3.0]
    assert capture_replay.schedule_offsets(calls, 0) == [0.0, 0.0, 0.0]
    assert capture_replay.schedule_offsets([], 1.0) == []
