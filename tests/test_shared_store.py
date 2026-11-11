import os

from obs import ingest, synth
from obs.panels import PANELS, Store, View
from obs.queries import parquet_src


def build(tmp):
    src, out = str(tmp / "src"), str(tmp / "data")
    synth.run(src, days=3)
    ingest.run("5d", out, [os.path.join(src, "claude")])
    ingest.run_codex("5d", out, os.path.join(src, "codex", "sessions"))
    return out


def norm(x):
    """Float sums depend on scan order: compare them rounded."""
    if isinstance(x, float):
        return round(x, 6)
    if isinstance(x, dict):
        return {k: norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [norm(v) for v in x]
    return x


def fds():
    n = 0
    for f in os.listdir("/proc/self/fd") if os.path.isdir("/proc/self/fd") else []:
        try:
            n += os.readlink(os.path.join("/proc/self/fd", f)).endswith(".parquet")
        except OSError:  # the listing fd itself is gone by now
            pass
    return n


def test_parquet_src_is_one_explicit_list(tmp_path):
    out = build(tmp_path)
    s = parquet_src(os.path.join(out, "events", "*", "*.parquet"))
    assert s.count("read_parquet(") == 1 and "*" not in s and s.count(".parquet'") >= 2


def test_shared_store_matches_per_query_and_reloads(tmp_path):
    out = build(tmp_path)
    fresh, shared = Store(out), Store(out, shared=True)
    for name in ("meta", "tools", "readtypes", "compactions", "bash"):
        a, b = PANELS[name](View(fresh, days=14)), PANELS[name](View(shared, days=14))
        assert norm(a["rows"]) == norm(b["rows"]), name
    assert fds() == 0  # no parquet handle survives a query
    n0 = View(shared, days=14).q("select count(*) n from rawall")[0]["n"]
    sig0 = shared._sig
    # the watcher rewrites a day: the next query sees the new file set
    f = sorted(shared.file_list())[0]
    os.replace(f, f + ".moved")
    assert View(shared, days=14).q("select count(*) n from rawall")[0]["n"] < n0
    assert shared._sig != sig0
    os.replace(f + ".moved", f)
    assert View(shared, days=14).q("select count(*) n from rawall")[0]["n"] == n0
