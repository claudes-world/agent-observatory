import gzip, hashlib, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs import retain, synth, verify, ingest, excerpts

DAY = 86400


def _tree(tmp_path, n=3):
    out = str(tmp_path / "syn")
    synth.run(out, days=2, seed=3, start="2026-01-01")
    files = sorted(p for p in (os.path.join(d, f) for d, _, fs in os.walk(out + "/claude") for f in fs) if p.endswith(".jsonl"))
    return out, files[:n]


def test_roundtrip_and_index(tmp_path):
    out, files = _tree(tmp_path)
    root = out + "/claude"
    old = time.time() - 45 * DAY
    before = {}
    for p in files:
        before[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
        os.utime(p, (old, old))
    dry = retain.retire(root, "30d", dry_run=True)
    assert dry["retired"] == 0 and all(os.path.exists(p) for p in files)
    res = retain.retire(root, "30d")
    assert res["failed"] == 0 and res["retired"] >= len(files)
    for p in files:
        assert not os.path.exists(p) and os.path.exists(p + ".gz")
        assert int(os.path.getmtime(p + ".gz")) == int(old)
        assert hashlib.sha256(gzip.open(p + ".gz", "rb").read()).hexdigest() == before[p]
    rows = [json.loads(l) for l in open(os.path.join(root, retain.INDEX))]
    r = next(x for x in rows if x["path"] == files[0])
    assert r["gz"] == files[0] + ".gz" and r["lines"] > 0 and r["bytes"] > r["gz_bytes"] and r["sessions"] and r["first_ts"] <= r["last_ts"]
    assert retain.retire(root, "30d")["retired"] == 0  # idempotent


def test_recent_kept(tmp_path):
    out, files = _tree(tmp_path, 1)
    p = files[0]
    now = time.time()
    os.utime(p, (now, now))
    # line timestamps are from 2026-01 (old) -> OR rule retires it even with a fresh mtime
    assert retain.retire(os.path.dirname(os.path.dirname(p)), "30d", dry_run=True)["candidates"] >= 1
    assert retain.retire(os.path.dirname(os.path.dirname(p)), "100000d")["retired"] == 0


def test_pointers_resolve_through_gz(tmp_path):
    out, files = _tree(tmp_path, 1)
    p = files[0]
    want = open(p, "rb").read().split(b"\n")[1].decode()
    ptr = {"file": "claude/" + os.path.relpath(p, out + "/claude").replace(os.sep, "/"), "line": 2}
    got = excerpts.Lines({"claude": out + "/claude"}).fetch([ptr])
    assert got[(p, 2)].rstrip("\n") == want
    old = time.time() - 60 * DAY
    os.utime(p, (old, old))
    assert retain.retire(out + "/claude", "30d")["retired"] >= 1 and not os.path.exists(p)
    got = excerpts.Lines({"claude": out + "/claude"}).fetch([ptr])
    assert got[(p, 2)].rstrip("\n") == want
    assert verify.read_line(retain.resolve(p), 2).rstrip("\n") == want
    assert retain.resolve(p) == p + ".gz"


def test_ingest_same_events_and_src_file_after_retire(tmp_path):
    out, files = _tree(tmp_path, 0)
    import duckdb
    root = out + "/claude"
    # cover every file so the comparison is whole-tree
    d1, d2 = str(tmp_path / "d1"), str(tmp_path / "d2")
    ingest.run("2000d", d1, root, align_day=True)
    old = time.time() - 60 * DAY
    for dp, _, fs in os.walk(root):
        for f in fs:
            os.utime(os.path.join(dp, f), (old, old))
    assert retain.retire(root, "30d")["retired"] > 0
    ingest.run("2000d", d2, root, align_day=True)
    q = "select src_file, src_line, session_id, event_type, content_bytes from read_parquet('%s/events/*/*.parquet') order by all"
    a, b = duckdb.sql(q % d1).fetchall(), duckdb.sql(q % d2).fetchall()
    assert a and a == b and all(r[0].endswith(".jsonl") for r in b)
