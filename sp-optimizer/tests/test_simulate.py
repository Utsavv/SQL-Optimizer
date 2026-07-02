"""Offline tests for the load simulator's pure aggregation helpers."""
import time

from scripts.simulate import ProcStats, merge_waits, percentile, summarize


def test_percentile_nearest_rank():
    vals = sorted([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0])
    assert percentile(vals, 50) == 50.0
    assert percentile(vals, 95) == 100.0
    assert percentile(vals, 100) == 100.0
    assert percentile([], 50) == 0.0
    assert percentile([42.0], 95) == 42.0


def test_summarize():
    s = ProcStats("dbo.p")
    for v in (100.0, 200.0, 300.0, 400.0):
        s.record(v)
    s.record_error("boom")
    out = summarize(s, elapsed_s=2.0)
    assert out["executions"] == 4
    assert out["errors"] == 1
    assert out["throughput_per_s"] == 2.0
    assert out["mean_ms"] == 250.0
    assert out["p50_ms"] == 200.0
    assert out["max_ms"] == 400.0
    assert out["first_errors"] == ["boom"]


def test_summarize_empty():
    out = summarize(ProcStats("dbo.p"), elapsed_s=1.0)
    assert out["executions"] == 0 and out["p50_ms"] is None


def test_merge_waits_sums_and_ranks():
    merged = merge_waits([
        {"PAGEIOLATCH_SH": 100.0, "LCK_M_S": 50.0},
        {"PAGEIOLATCH_SH": 200.0, "WRITELOG": 30.0},
    ])
    assert list(merged)[0] == "PAGEIOLATCH_SH"
    assert merged["PAGEIOLATCH_SH"] == 300.0
    assert merged["LCK_M_S"] == 50.0


def test_procstats_thread_safety_smoke():
    import threading
    s = ProcStats("dbo.p")

    def hammer():
        for _ in range(500):
            s.record(1.0)

    ts = [threading.Thread(target=hammer) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(s.latencies_ms) == 2000
