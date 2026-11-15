import json, os
from datetime import datetime, timedelta, timezone

import duckdb
from fastapi.testclient import TestClient

from obs import ingest
from obs.dashboard import create_app


def agy_brain(root, t0):
    d = os.path.join(root, "conv-1", ".system_generated", "logs")
    os.makedirs(d)
    iso = lambda s: (t0 + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    big = "x" * 36000
    recs = [
        {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": iso(0), "content": "filler"},
        {"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": iso(1), "content": "filler"},
        {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": iso(2),
         "model": "gemini-test", "input_tokens": 3000, "cache_read_tokens": 0, "output_tokens": 50,
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "cat notes.md"}},
                        {"name": "read_file", "args": {"Path": "/w/app.py"}}]},
        {"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": iso(4), "content": big},
        {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": iso(5), "content": "print(1)"},
        {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": iso(6),
         "input_tokens": 12000, "cache_read_tokens": 3000, "output_tokens": 40, "tool_calls": []},
        {"step_index": 6, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE", "created_at": iso(7),
         "input_tokens": 15000, "cache_read_tokens": 0, "output_tokens": 900, "content": "summary"},
        {"step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": iso(8),
         "model": "gemini-test", "input_tokens": 2000, "cache_read_tokens": 0, "output_tokens": 10, "tool_calls": []},
    ]
    open(os.path.join(d, "transcript_full.jsonl"), "w").write("\n".join(json.dumps(r) for r in recs) + "\n")


def test_agy_ingest(tmp_path):
    root, out = str(tmp_path / "brain"), str(tmp_path / "data")
    agy_brain(root, datetime.now(timezone.utc) - timedelta(minutes=5))
    assert ingest.run_agy("2d", out, root) == 0
    ev = f"read_parquet('{out}/events/*/*-agy.parquet')"
    rows = duckdb.sql(f"select event_type, count(*) from {ev} group by 1").fetchall()
    assert dict(rows) == {"llm_request": 3, "tool_call": 2, "tool_result": 2, "compaction": 1}, rows
    h = duckdb.sql(f"select distinct harness, provider, session_id from {ev}").fetchall()
    assert h == [("antigravity", "google", "conv-1")]
    # model carried onto the request that omits it; outputs paired with calls in order
    assert duckdb.sql(f"select count(*) from {ev} where event_type='llm_request' and model='gemini-test'").fetchall()[0][0] == 3
    r = dict(duckdb.sql(f"select tool_name, text_bytes from {ev} where event_type='tool_result'").fetchall())
    assert r == {"run_command": 36000, "read_file": 8}
    fam, kind = duckdb.sql(f"select cmd_family, file_kind from {ev} where event_type='tool_result' and tool_name='run_command'").fetchall()[0]
    assert fam and kind == "markdown"
    # the big output is re-read by the one request before the checkpoint, not by the one after it
    assert duckdb.sql(f"select reread_turns from {ev} where tool_name='run_command' and event_type='tool_result'").fetchall()[0][0] == 1
    assert duckdb.sql(f"select detail from {ev} where event_type='compaction'").fetchall()[0][0] == "auto"
    src, ln = duckdb.sql(f"select src_file, src_line from {ev} where tool_name='read_file' and event_type='tool_result'").fetchall()[0]
    assert src == "agy/conv-1/.system_generated/logs/transcript_full.jsonl" and ln == 5
    assert json.load(open(os.path.join(out, "sources.json")))["agy"] == root


def test_agy_in_harness_filter(tmp_path):
    root, out = str(tmp_path / "brain"), str(tmp_path / "data")
    agy_brain(root, datetime.now(timezone.utc) - timedelta(minutes=5))
    ingest.run_agy("2d", out, root)
    c = TestClient(create_app(out))
    m = c.get("/api/meta?days=7").json()["rows"][0]
    assert {x["harness"] for x in m["harnesses"]} == {"antigravity"}
    assert c.get("/api/meta?days=7&harness=antigravity").json()["rows"][0]["requests"] == 3


def test_agy_pointers_verify(tmp_path):
    from obs import report, verify
    root, out, rep = str(tmp_path / "brain"), str(tmp_path / "data"), str(tmp_path / "reports")
    t0 = datetime.now(timezone.utc).replace(hour=0, minute=10, second=0, microsecond=0)
    agy_brain(root, t0)
    ingest.run_agy("2d", out, root)
    d = t0.strftime("%Y-%m-%d")
    assert report.run(d, out, rep) == 0
    agg = json.load(open(os.path.join(rep, d + ".json")))
    kinds = {s for s in ("largest_tool_outputs", "per_session") for r in agg.get(s) or [] if r.get("pointer")}
    assert kinds == {"largest_tool_outputs", "per_session"}, kinds
    assert verify.run(d, out, rep, n=50) == 0
