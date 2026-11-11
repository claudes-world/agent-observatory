"""file_ext / file_kind derivation (Claude file tools, Codex cat/sed -n/view_image)."""
from obs.ingest import claude_file, codex_file, file_ext_kind, file_fields, shell_file


def test_ext_kind_basic_and_special_names():
    assert file_ext_kind("/a/b/Main.PY") == ("py", "code")
    assert file_ext_kind("/a/README.md") == ("md", "markdown")
    assert file_ext_kind("/a/Dockerfile") == ("dockerfile", "code")
    assert file_ext_kind("/a/Makefile") == ("makefile", "code")
    assert file_ext_kind("/a/.env") == ("env", "json-yaml")
    assert file_ext_kind("/a/.env.local") == ("env", "json-yaml")
    assert file_ext_kind("/a/LICENSE") == ("none", "other")
    assert file_ext_kind("/a/x.log") == ("log", "log")
    assert file_ext_kind("/a/x.parquet") == ("parquet", "binary")


def test_jsonl_transcript_vs_plain_jsonl():
    assert file_ext_kind("/home/u/.claude/projects/-p/abc.jsonl") == ("jsonl", "jsonl-transcript")
    assert file_ext_kind("/home/u/.codex/sessions/2026/10/05/rollout-2026-10-05T13-22-37-01a1.jsonl")[1] == "jsonl-transcript"
    assert file_ext_kind("/data/events.jsonl") == ("jsonl", "json-yaml")


def test_claude_read_image_and_glob():
    f = claude_file("Read", {"file_path": "/tmp/shots/Screen Shot.PNG"})
    assert (f["file_ext"], f["file_kind"]) == ("png", "image") and f["file_path"].endswith("Screen Shot.PNG")
    g = claude_file("Glob", {"pattern": "**/*.ts", "path": "/repo"})
    assert g["file_path"] == "/repo" and (g["file_ext"], g["file_kind"]) == ("ts", "code")
    assert claude_file("Bash", {"command": "ls"}) == {}


def test_codex_cat_sed_head_targets():
    assert shell_file("cat /repo/obs/ingest.py") == "/repo/obs/ingest.py"
    assert shell_file("cd /repo && sed -n '1,120p' docs/plan.md") == "docs/plan.md"
    assert shell_file("head -n 20 run.log | grep err") == "run.log"
    assert shell_file("tail -c 500 /v/a.json") == "/v/a.json"
    assert shell_file("sed -n '5,9p'") is None and shell_file("python x.py") is None
    f = codex_file("exec_command", "", "sed -n '1,40p' src/app.tsx")
    assert (f["file_path"], f["file_ext"], f["file_kind"]) == ("src/app.tsx", "tsx", "code")


def test_codex_view_image_in_exec_js():
    js = 'const base="/r/ev/";\nfor(const p of ["scopes.jpg","cimd.jpg"]){image((await tools.view_image({path:base+p})).image_url)}'
    f = codex_file("exec", js, js)
    assert f["file_kind"] == "image" and f["file_ext"] == "jpg" and f["file_path"] == "scopes.jpg"
    lit = 'image((await tools.view_image({path:"/shots/a b.png"})).image_url)'
    f = codex_file("exec", lit, lit)
    assert (f["file_path"], f["file_ext"], f["file_kind"]) == ("/shots/a b.png", "png", "image")


def test_path_kept_but_secrets_masked():
    f = file_fields("/srv/app/config.yaml?token=abcd1234efgh5678")
    assert f["file_ext"] and "abcd1234efgh5678" not in f["file_path"] and f["file_path"].startswith("/srv/app/config.yaml")
