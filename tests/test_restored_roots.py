"""Restored transcript roots: NAME=PATH labels, plain claude-code harness, cross-root de-dup (identical, prefix, overlap)."""
import glob, json, os, shutil

import duckdb

from obs import ingest, synth


def q(out, sql):
    return duckdb.sql(sql.replace("EV", f"read_parquet('{out}/events/*/*.parquet', union_by_name=true)")).fetchall()


def test_restored_roots_dedup(tmp_path):
    src, out = str(tmp_path / "src"), str(tmp_path / "data")
    synth.run(src, days=3)
    live = os.path.join(src, "claude")
    ingest.run("5d", str(tmp_path / "base"), [live])
    base = q(str(tmp_path / "base"), "select count(*), sum(input_tokens + cache_read_tokens + cache_write_tokens) from EV")[0]

    files = sorted(glob.glob(os.path.join(live, "*", "*.jsonl")))
    assert len(files) >= 3
    gcs, r2 = str(tmp_path / "gcs"), str(tmp_path / "r2" / ".claude" / "projects")
    for p in files:  # r2: identical copies of every live file, plus one prefix copy rewritten below
        d = os.path.join(r2, os.path.relpath(p, live))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy(p, d)
    lines = open(files[0]).readlines()
    open(os.path.join(r2, os.path.relpath(files[0], live)), "w").writelines(lines[: len(lines) // 2])
    rel1 = os.path.relpath(files[1], live)  # gcs: same file as r2 (overlap between restored roots)
    os.makedirs(os.path.dirname(os.path.join(gcs, rel1)), exist_ok=True)
    shutil.copy(files[1], os.path.join(gcs, rel1))
    # one session that exists only in the restore (live copy deleted): must be counted once
    only = os.path.join(gcs, "-lost-project", "0000aaaa-0000-0000-0000-000000000001.jsonl")
    os.makedirs(os.path.dirname(only))
    sid = os.path.basename(only)[:-6]
    recs = [json.loads(l) for l in open(files[2]) if l.strip()]
    with open(only, "w") as f:
        for o in recs:
            if isinstance(o, dict):
                o["sessionId"] = sid
                if isinstance(o.get("message"), dict) and o["message"].get("id"):
                    o["message"]["id"] = "lost-" + o["message"]["id"]
            f.write(json.dumps(o) + "\n")

    ingest.run("5d", out, [live, "restored-gcs=" + gcs, "restored-r2=" + r2])
    srcs = json.load(open(os.path.join(out, "sources.json")))
    assert srcs["claude-restored-gcs"] == os.path.abspath(gcs) and srcs["claude-restored-r2"] == os.path.abspath(r2)
    by = dict(q(out, "select split_part(src_file, '/', 1), count(*) from EV group by 1"))
    assert set(by) == {"claude", "claude-restored-gcs"}  # r2 is entirely duplicate
    assert by["claude"] == base[0]
    assert q(out, "select count(distinct session_id) from EV where src_file like 'claude-restored-gcs/%'")[0][0] == 1
    assert {h for (h,) in q(out, "select distinct harness from EV")} == {"claude-code"}
    st = json.load(open(os.path.join(out, "ingest_stats_claude-restored-r2.json")))
    assert st["events"] > 0 and st["dup_events_skipped"] == st["events"]
    # profile roots without the restored label keep their own events (no de-dup)
    assert ingest.root_key("/x/.claude-deepseek/projects") == "claude-deepseek"
    assert ingest.harness_of_key("claude-restored-r2") == "claude-code"
    assert ingest.harness_of_key("claude-deepseek") == "claude-code:deepseek"
