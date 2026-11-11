import glob, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from obs.scan import scan_text
from obs import redact as R, verify, excerpts

# optional local check against a real transcript copy: OBS_REDACTION_TRIAL=<dir>, OBS_REDACTION_TRIAL_ENV=<env file>
TRIAL = os.environ.get("OBS_REDACTION_TRIAL", "")
TRIAL_ENV = os.environ.get("OBS_REDACTION_TRIAL_ENV", "")
FAKE = "zq9Lm2-fake-secret-value-7788"


def rx(names=R.DEFAULT_NAMES, secrets=None, kw=""):
    return R.Redactor(names, secrets, kw)


def test_api_key_prefix_rules():
    r = rx()
    s = "key sk-or-v1-" + "a1B2c3D4" * 5
    out = r.mask_text(s)
    assert out.startswith("key sk-X") and len(out) == len(s) and set(out[7:]) == {"X"}
    g = "ghp_" + "aB3dE5gH7j" * 4
    o = r.mask_text(g)
    assert o.startswith("ghp_X") and o[4:] == "X" * (len(g) - 4) and len(o) == len(g)
    assert R.mask_span("api_key", "AKIA" + "B" * 16) == "AKIAB" + "X" * 15


def test_email_keeps_at():
    out = rx().mask_text("mail bob.smith@example.com now")
    assert out == "mail XXXXXXXXX@XXXXXXX.XXX now"


def test_phone_ip_all_x():
    r = rx()
    for s in ("+1 415 555 0132", "10.20.30.40"):
        out = r.mask_text("a " + s + " b")
        assert out == "a " + "X" * len(s) + " b"


def test_jwt_and_telegram():
    r = rx()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    tg = "123456789:" + "AbC_dEf-GhI" * 3 + "jk"
    assert r.mask_text(jwt) == "X" * len(jwt)
    assert r.mask_text("t " + tg) == "t " + "X" * len(tg)


def test_pem_block():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----"
    out = rx().mask_text("x\n" + pem + "\ny")
    assert "MIIE" not in out and "BEGIN" not in out and out.count("\n") == 4 and len(out) == len(pem) + 4


def test_high_entropy():
    s = "Qx9Zk2Lm8Pw4Rt7Yv1Bn6Cd3Fg5Hj0KaSeUiOoXzWqVuTpNrMlJ"
    assert rx().mask_text("v " + s) == "v " + "X" * len(s)


def test_names_case_insensitive():
    out = rx().mask_text("hi ALICE and carol, CaRoL!")
    assert out == "hi XXXXX and XXXXX, XXXXX!"


def test_known_secret_variants(tmp_path):
    f = tmp_path / "x.env"
    f.write_text(f"FOO='{FAKE}'\nSHORT=abc\n")
    rxs, nf, nv = R.load_secrets([str(f)])
    assert (nf, nv) == (1, 1)
    r = rx(secrets=rxs)
    from urllib.parse import quote
    txt = f"a {FAKE} b {quote('p q' + FAKE, safe='')} c"
    out = r.mask_text(txt)
    assert FAKE not in out and len(out) == len(txt)
    assert out.startswith("a zq9Lm2-XXXX") is False and out.startswith("a zq9Lm")  # '-' is at index 6 > 4: 5 chars kept
    assert r.rules["known_secret"] == 2


def test_known_secret_prefix_rule():
    k = R.mask_span("known_secret", "sk-or-v1-abcdefabcdef")
    assert k == "sk-" + "X" * 18


def test_prose_and_tool_output_record(tmp_path):
    src = tmp_path / "s"; src.mkdir()
    sec = tmp_path / "k.env"; sec.write_text(f"K={FAKE}\n")
    recs = [
        {"type": "user", "uuid": "u1", "cwd": "/home/alice/proj", "message": {"role": "user", "content": "please read my plan\nline2"}},
        {"type": "assistant", "uuid": "u2", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "sure thing"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat secret.txt"}}]}},
        {"type": "user", "uuid": "u3", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": f"ok output\nemail a.b@c.io token {FAKE} end"}]}},
    ]
    (src / "a.jsonl").write_text("\n".join(json.dumps(x) for x in recs) + "\n")
    dst = tmp_path / "d"
    assert R.redact(str(src), str(dst), secrets_files=[str(sec)]) == 0
    out = [json.loads(l) for l in (dst / "a.jsonl").read_text().splitlines()]
    assert len(out) == 3
    assert out[0]["message"]["content"] == "X" * 19 + "\n" + "X" * 5
    assert out[0]["cwd"] == "/home/XXXXX/proj"
    assert out[1]["message"]["content"][0]["text"] == "X" * 10
    assert out[1]["message"]["content"][1]["input"]["command"] == "X" * 14
    assert out[1]["message"]["content"][1]["name"] == "Bash"
    res = out[2]["message"]["content"][0]["content"]
    assert res.startswith("ok output\nemail XXX@X.XX token ") and res.endswith(" end") and FAKE not in res
    man = json.load(open(dst / "manifest.json"))
    assert man["rules"]["known_secret"] == 1 and man["rules"]["email"] == 1 and man["prose_chars_masked"] > 0
    assert man["file_detail"][0]["lines"] == 3 and len(man["file_detail"][0]["sha256"]) == 64


def test_keyword_record(tmp_path):
    src = tmp_path / "s"; src.mkdir()
    recs = [{"type": "user", "uuid": "u1", "message": {"role": "user", "content": "I'm in Atlantis"}},
            {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "el dorado city is nice"}]}},
            {"type": "user", "uuid": "u3", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "plain tool output"}]}}]
    (src / "a.jsonl").write_text("\n".join(json.dumps(x) for x in recs) + "\n")
    R.redact(str(src), str(tmp_path / "d"), secrets_files=[str(tmp_path / "none")])
    out = [json.loads(l) for l in (tmp_path / "d" / "a.jsonl").read_text().splitlines()]
    assert out[0]["message"]["content"] == "X" * len("I'm in Atlantis") and out[0]["uuid"] == "u1"
    assert set(out[1]["message"]["content"][0]["content"]) == {"X"}
    assert out[2]["message"]["content"][0]["content"] == "plain tool output"
    assert json.load(open(tmp_path / "d" / "manifest.json"))["keyword_masked_records"] == 2


def test_missing_source_message(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    (data / "sources.json").write_text(json.dumps({"claude": str(tmp_path / "nope")}))
    rep = tmp_path / "rep"; rep.mkdir()
    (rep / "2026-01-01.json").write_text(json.dumps({"largest_tool_outputs": [
        {"payload_hash": "h", "pointer": {"file": "claude/a.jsonl", "line": 3}}]}))
    assert verify.run("2026-01-01", str(data), str(rep), 5, 1) == 1
    assert verify.run("2026-01-01", str(data), str(rep), 5, 1, allow_missing=True) == 0
    rows = [{"pointer": {"file": "claude/a.jsonl", "line": 3}}]
    st = excerpts.write(str(data), str(tmp_path / "out" / "2026-01-01"), [("largest", "result", "payload_hash", rows)])
    assert st["source_missing"] == 1
    assert open(tmp_path / "out" / rows[0]["excerpt"]).read().startswith("Linked transcript not on this NAS")


def test_trial_copy(tmp_path):
    files = [f for f in glob.glob(TRIAL + "/**/*.json*", recursive=True) if os.path.isfile(f)] if TRIAL else []
    if not files or not os.path.isfile(TRIAL_ENV):
        import pytest; pytest.skip("trial copy absent")
    dst = tmp_path / "dst"
    assert R.redact(TRIAL, str(dst)) == 0
    key = None
    for line in open(TRIAL_ENV):
        if line.startswith("OPENROUTER_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
    assert key and len(key) > 8
    n = 0
    emails = 0
    for f in glob.glob(str(dst) + "/**/*.jsonl", recursive=True):
        t = open(f, encoding="utf-8", errors="replace").read()
        n += t.count(key)
        for ln in t.splitlines():
            if "@" in ln:
                emails += sum(1 for *_, k, _m in scan_text(ln, names=R._NONE) if k == "email")
    assert n == 0
    assert emails == 0
    man = json.load(open(dst / "manifest.json"))
    assert man["files"] == len(files)
    print("TRIAL", json.dumps({k: v for k, v in man.items() if k not in ("file_detail", "src")}))


def test_known_secret_guard(tmp_path):
    f = tmp_path / "g.env"
    f.write_text("PORT=8080\nLONGPLAIN=localhost.example.internal\nAPI_TOKEN=ab12cd34\nRAND=Zk3vQ9xLp2Wm8TnR5yBc\nFLAG=true\n")
    rxs, nf, loaded, skipped, key_of = R.load_secrets_ex([str(f)])
    assert (loaded, skipped) == (2, 3)                       # RAND + API_TOKEN kept
    assert sorted(set(key_of.values())) == ["API_TOKEN", "RAND"]
    r = R.Redactor(None, rxs, None, key_of)
    out = r.mask_text("port 8080 on localhost.example.internal true ab12cd34 Zk3vQ9xLp2Wm8TnR5yBc")
    assert out.startswith("port 8080 on localhost.example.internal true ")
    assert "ab12cd34" not in out and "Zk3vQ9xLp2Wm8TnR5yBc" not in out
    assert dict(r.key_hits) == {"API_TOKEN": 1, "RAND": 1}
