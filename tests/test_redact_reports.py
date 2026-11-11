import csv, io, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs.redact_reports import LedgerMasker, redact_reports
from obs.scan import scan_text

SECRET = "zq9Lm2-fake-secret-value-7788"
HDR = ["ts", "session", "cmd_text", "exit", "file:line"]


def tree(tmp_path, rows, excerpt="hello", md="ssh 10.20.30.40 now", env=True):
    src = tmp_path / "src"; (src / "d" / "excerpts").mkdir(parents=True)
    buf = io.StringIO(); w = csv.writer(buf); w.writerow(HDR); [w.writerow(r) for r in rows]
    (src / "d" / "bash-commands.csv").write_text(buf.getvalue())
    (src / "d" / "excerpts" / "largest-01.txt").write_text(excerpt)
    (src / "d.md").write_text(md); (src / "d.json").write_text(json.dumps({"a": md}))
    (src / "d" / "report.html").write_text("<p>" + md + "</p>")
    sf = tmp_path / "s.env"; sf.write_text(f"TOKEN={SECRET}\n")
    return src, [str(sf)]


def run(tmp_path, rows, **k):
    src, sf = tree(tmp_path, rows, **k)
    dst = tmp_path / "dst"
    redact_reports(str(src), str(dst), sf, "Alice", "Atlantis")
    return src, dst, json.load(open(dst / "manifest.json"))


def rd(p):
    return list(csv.reader(open(p, newline="")))


def test_ipv4_row_masked_columns_kept(tmp_path):
    src, dst, man = run(tmp_path, [["t", "s", "ssh root@10.20.30.40 ls", "ok", "a:1"]])
    out = rd(dst / "d" / "bash-commands.csv")
    assert len(out) == 2 and all(len(r) == 5 for r in out)
    assert "10.20.30.40" not in out[1][2] and len(out[1][2]) == len("ssh root@10.20.30.40 ls")
    assert out[1][0] == "t" and out[1][4] == "a:1"


def test_email_in_excerpt_keeps_at(tmp_path):
    _, dst, _ = run(tmp_path, [], excerpt="mail bob.smith@example.com done")
    t = (dst / "d" / "excerpts" / "largest-01.txt").read_text()
    assert "bob" not in t and "@" in t and t.startswith("mail ") and len(t) == len("mail bob.smith@example.com done")


def test_known_secret_in_cmd_text(tmp_path):
    _, dst, man = run(tmp_path, [["t", "s", f"curl -H {SECRET}", "ok", "a:1"]])
    out = rd(dst / "d" / "bash-commands.csv")[1][2]
    assert SECRET not in out and len(out) == len(f"curl -H {SECRET}") and out.startswith("curl -H ")
    assert man["rules"].get("known_secret", 0) >= 1


def test_keyword_row_masked_but_shape_kept(tmp_path):
    _, dst, _ = run(tmp_path, [["t", "s", "trip to Atlantis", "ok", "a:1"]])
    out = rd(dst / "d" / "bash-commands.csv")[1]
    assert set(out[2]) == {"X"} and len(out[2]) == len("trip to Atlantis") and len(out) == 5


def test_keyword_excerpt_all_x(tmp_path):
    _, dst, _ = run(tmp_path, [], excerpt="a Atlantis b\nline2")
    assert (dst / "d" / "excerpts" / "largest-01.txt").read_text() == "X" * len("a Atlantis b") + "\n" + "X" * 5


def test_md_json_html_masked_and_manifest(tmp_path):
    _, dst, man = run(tmp_path, [["t", "s", "ls", "ok", "a:1"]])
    for f in ("d.md", "d.json", "d/report.html"):
        assert "10.20.30.40" not in (dst / f).read_text()
    json.loads((dst / "d.json").read_text())
    assert sum(man["rules"].values()) >= 3
    assert set(man["spans_by_file_type"]) >= {"md", "json", "html"}
    assert man["files"] == 5


def test_writetime_masker_only_text_cols():
    m = LedgerMasker(names="Alice", secrets_files=[], keywords="")
    row = m.row(["ts", "cmd_text", "n"], ["2026-11-07T01:02:03.123Z", "ping 192.168.1.5", "5551234567"], only_text=True)
    assert "192.168.1.5" not in row[1] and row[0] == "2026-11-07T01:02:03.123Z" and row[2] == "5551234567"
    assert not scan_text(row[1], None)
