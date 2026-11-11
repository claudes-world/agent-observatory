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
