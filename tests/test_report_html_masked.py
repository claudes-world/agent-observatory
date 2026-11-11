import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs import panels, report
from obs.redact_reports import LedgerMasker

IP, MAIL = "198.51.100.34", "someone@example.com"
CMD = f"docker run -d -p {IP}:8000:8000 -e OWNER={MAIL} img"


def fixture_panels(st, day):
    return {"cmds": [{"cmd_text": CMD, "pointer": {"file": f"claude/{IP}/x.jsonl", "line": 3}}],
            "excerpt": f"contact {MAIL} at {IP}", "what_to_do": f"ssh {IP}", "path": f"/srv/{MAIL}/f.py"}


def test_report_html_has_no_ipv4_or_email(tmp_path, monkeypatch):
    monkeypatch.setattr(panels, "all_panels", fixture_panels)
    monkeypatch.setattr(panels, "Store", lambda data: None)
    m = LedgerMasker(names="", keywords="", secrets_files=[], secrets_dirs=[str(tmp_path)])
    report.write_html(str(tmp_path), "2026-11-08", str(tmp_path / "out"), m)
    html = (tmp_path / "out" / "report.html").read_text()
    assert "window.OBS_DATA=" in html
    assert IP not in html and "someone" not in html
    assert not re.search(r"(?:\d{1,3}\.){3}\d{1,3}:8000", html)
    assert "@" in html and "docker run" in html  # structure kept, only spans masked
