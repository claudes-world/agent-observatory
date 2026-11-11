import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs import ingest


class FakeSink:
    host = "h"

    def __init__(self):
        self.rows = []

    def add(self, r):
        self.rows.append(r)


def _ts(i):
    return f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}.000Z"


def _asst(i, ctx, tool=None):
    c = [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}] if tool else [{"type": "text", "text": "x"}]
    return {"type": "assistant", "timestamp": _ts(i), "sessionId": "s1", "cwd": "/w",
            "message": {"id": f"m{i}", "model": "claude-x", "content": c,
                        "usage": {"input_tokens": 1, "cache_read_input_tokens": ctx, "cache_creation_input_tokens": 0, "output_tokens": 5}}}


def _result(i):
    return {"type": "user", "timestamp": _ts(i), "sessionId": "s1", "cwd": "/w",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x" * 36000}]}}


def _run(tmp_path, recs):
    p = tmp_path / "proj" / "s1.jsonl"
    p.parent.mkdir()
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    ingest.ROOT_HINT[0], ingest.SRC_KEY[0] = str(tmp_path), "claude"
    sink, stats = FakeSink(), {"lines": 0, "skipped": 0, "events": 0, "models": set()}
    ingest.process_file(str(p), ingest.parse_ts("2000-01-01T00:00:00Z"), sink, stats)
    res = next(r for r in sink.rows if r["event_type"] == "tool_result")
    reqs = sorted((r for r in sink.rows if r["event_type"] == "llm_request"), key=lambda r: r["ts"])
    comps = [r for r in sink.rows if r["event_type"] == "compaction"]
    return res, reqs, comps


def test_explicit_marker(tmp_path):
    recs = [_asst(1, 50000, tool=True), _result(2), _asst(3, 51000), _asst(4, 52000),
            {"type": "system", "subtype": "compact_boundary", "timestamp": _ts(5), "compactMetadata": {"trigger": "manual", "preTokens": 52000}},
            _asst(6, 60000), _asst(7, 61000)]  # no shrink: only the marker splits
    res, reqs, comps = _run(tmp_path, recs)
    assert res["reread_turns"] == 2 and res["reread_tokens"] == res["estimated_tokens"] * 2
    assert [r["segment_index"] for r in reqs] == [0, 0, 0, 1, 1]
    assert reqs[-1]["compaction_id"] == "s1#1"
    assert [(c["detail"], c["input_tokens"]) for c in comps] == [("manual", 52000)]


def test_fallback_shrink(tmp_path):
    recs = [_asst(1, 80000, tool=True), _result(2), _asst(3, 82000), _asst(4, 20000), _asst(5, 22000)]
    res, reqs, comps = _run(tmp_path, recs)  # 20k < 60% of 82k, previous > 30k
    assert res["reread_turns"] == 1
    assert [r["segment_index"] for r in reqs] == [0, 0, 1, 1]
    assert [c["detail"] for c in comps] == ["unknown"]


def test_no_compaction(tmp_path):
    recs = [_asst(1, 40000, tool=True), _result(2), _asst(3, 41000), _asst(4, 30000), _asst(5, 43000)]
    res, reqs, comps = _run(tmp_path, recs)  # 30k/41k = 73%: not a shrink
    assert res["reread_turns"] == 3
    assert {r["segment_index"] for r in reqs} == {0}
    assert comps == []
