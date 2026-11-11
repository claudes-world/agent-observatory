import json, os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs import ingest, synth
from obs.panels import PANELS, Store, View

START_AGO = 9  # synth data starts 9 days before today: a 30-day view has leading days with no data


def _ingest(tmp_path):
    root, data = str(tmp_path / "syn"), str(tmp_path / "data")
    first = (datetime.now(timezone.utc) - timedelta(days=START_AGO)).strftime("%Y-%m-%d")
    synth.run(root, days=START_AGO + 1, seed=3, start=first)
    ingest.run_all("30d", data, os.path.join(root, "claude"), os.path.join(root, "codex", "sessions"), align_day=True)
    return data, first


def test_coverage_computed_from_synth(tmp_path):
    data, first = _ingest(tmp_path)
    c = json.load(open(os.path.join(data, "ingest_stats.json")))["coverage"]
    assert c["name"] == "claude" and c["files"] > 0 and c["bytes"] > 0
    assert c["event_first"][:10] == first
    assert c["event_first"] <= c["event_last"] and c["mtime_earliest"] <= c["mtime_latest"]
    m = PANELS["meta"](View(Store(data), days=30))["rows"][0]
    cv = m["coverage"]
    assert cv["earliest_event"][:10] == first and cv["latest_event"] >= cv["earliest_event"]
    assert {s["name"] for s in cv["sources"]} >= {"claude", "codex"}
    assert all(set(s) >= {"name", "files", "earliest", "latest"} for s in cv["sources"])
    assert 1 <= m["days_with_data"] <= START_AGO + 1


def test_day_before_earliest_is_no_data(tmp_path):
    data, first = _ingest(tmp_path)
    st = Store(data)
    for panel in ("context", "beforeafter"):
        rows = PANELS[panel](View(st, days=30))["rows"]
        pre = [r for r in rows if r["day"] < first]
        assert pre and all(r["no_data"] and r["context"] is None for r in pre), panel
        assert not any(r.get("no_data") for r in rows if r["day"] >= first)
        assert len({r["day"] for r in rows}) == len(rows)


def test_meta_cache_hit_rate_and_range(tmp_path):
    data, first = _ingest(tmp_path)
    m = PANELS["meta"](View(Store(data), days=30))["rows"][0]
    assert m["context_tokens"] == m["input_tokens"] + m["cache_read_tokens"]
    assert m["cache_hit_rate"] == m["cache_read_tokens"] / m["context_tokens"]
    assert 0 <= m["cache_hit_rate"] <= 1
    end = datetime.fromisoformat(m["range_end"])
    assert m["range_end"] == m["last_day"] and (end - datetime.fromisoformat(m["range_start"])).days == 29


def test_context_median_and_hints(tmp_path):
    data, _ = _ingest(tmp_path)
    st = Store(data)
    c = PANELS["context"](View(st, days=30))
    vals = sorted(r["context"] for r in c["rows"] if not r.get("no_data"))
    assert vals[0] <= c["median_context"] <= vals[-1]
    assert "outweighs" not in c["hint"] and "tool output" in c["hint"]
    k = PANELS["cache"](View(st, days=30))
    assert k["p50"] <= k["p95"] and "split" not in k["hint"] and "subagents" in k["hint"]
