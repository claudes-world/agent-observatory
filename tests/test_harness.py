import glob, json, os, shutil
from datetime import datetime, timezone

import duckdb
from fastapi.testclient import TestClient

from obs import ingest, synth
from obs.dashboard import create_app


def kimi_wire(root, ms):
    d = os.path.join(root, "wd_proj_abcdef12", "session_s1", "agents", "main")
    os.makedirs(d)
    recs = [{"type": "llm.request", "time": ms, "model": "kimi-k2"},
            {"type": "context.append_loop_event", "time": ms + 1, "event": {
                "type": "step.end", "messageId": "m1", "usage": {"inputOther": 10, "output": 5}}},
            {"type": "context.append_loop_event", "time": ms + 2, "event": {
                "type": "tool.call", "toolCallId": "c1", "name": "ReadFile", "args": {"path": "x"}}},
            {"type": "context.append_loop_event", "time": ms + 3, "event": {
                "type": "tool.result", "toolCallId": "c1", "result": {"output": "hello"}}}]
    open(os.path.join(d, "wire.jsonl"), "w").write("\n".join(json.dumps(r) for r in recs) + "\n")


def build(tmp):
    src, out = str(tmp / "src"), str(tmp / "data")
    synth.run(src, days=3)
    prof = os.path.join(str(tmp), ".claude-deepseek", "projects")
    shutil.copytree(os.path.join(src, "claude"), prof)
    kroot = str(tmp / "kimi")
    kimi_wire(kroot, int(datetime.now(timezone.utc).timestamp() * 1000))
    ingest.run("5d", out, [os.path.join(src, "claude"), prof])
    ingest.run_codex("5d", out, os.path.join(src, "codex", "sessions"))
    ingest.run_kimi("5d", out, kroot)
    return out


def test_harness_derivation(tmp_path):
    out = build(tmp_path)
    rows = duckdb.sql(f"select harness, count(*) n from read_parquet('{out}/events/*/*.parquet') group by 1").fetchall()
    got = {h: n for h, n in rows}
    assert set(got) == {"claude-code", "claude-code:deepseek", "codex", "kimi-code"}, got
    # subagent rows keep the harness of their session (agent stays main|subagent)
    n = duckdb.sql(f"select count(*) from read_parquet('{out}/events/*/*.parquet') where agent='subagent' and harness is null").fetchall()[0][0]
    assert n == 0


def test_endpoint_filters_to_harness(tmp_path):
    out = build(tmp_path)
    c = TestClient(create_app(out))
    allm = c.get("/api/meta?days=14").json()["rows"][0]
    assert {h["harness"] for h in allm["harnesses"]} == {"claude-code", "claude-code:deepseek", "codex", "kimi-code"}
    one = c.get("/api/meta?days=14&harness=codex").json()["rows"][0]
    assert one["requests"] == next(h["requests"] for h in allm["harnesses"] if h["harness"] == "codex")
    assert one["requests"] < allm["requests"]
    for name in ("largest", "repeats", "sessions", "loops"):
        for r in c.get(f"/api/{name}?days=14&harness=codex").json()["rows"]:
            assert r.get("harness") in (None, "codex")
    m = c.get("/api/models?days=14&harness=kimi-code").json()["rows"]
    assert m and all(s["harness"] == "kimi-code" for r in m for s in r["harness_split"])
    assert c.get("/api/meta?days=14&harness=bad'name").status_code == 400
