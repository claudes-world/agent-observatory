import pytest

from obs.cmdparse import parse, split_segments, strip_cd

CASES = [
    # (command, cwd, family, target)
    ("cd /home/user/code/app && python3 tools/city/paris/paris_lamp.py build", "/home/user/code/app", "python", "paris/paris_lamp.py"),
    ("cd ~/code/app-wt/r94b; tail -n 4 tools/city/engine.py", "~/code/app-wt/r94b", "inspect", "tail"),
    ("cd \"/tmp/my dir\" && pytest -q tests/", "/tmp/my dir", "test", "pytest"),
    ("python3 /home/user/work/tasks/road-kit/round1/evidence.py --out x", None, "python", "round1/evidence.py"),
    ("python3 -u scripts/run.py | tail -20", None, "python", "scripts/run.py"),
    ("python3 - <<'PY'\nfrom pathlib import Path\np=Path('x')\nprint(p)\nPY", None, "python", "python heredoc"),
    ("cd ~/code/app-wt/r94b && python3 - <<'EOF'\np='tools/tests/test_city.py'\nEOF", "~/code/app-wt/r94b", "python", "python heredoc"),
    ("python3 -c 'import json; print(1)'", None, "python", "python -c"),
    ("python3 -m venv .venv && .venv/bin/pip install duckdb", None, "pip", "pip install"),
    ("python -m pytest tests/test_x.py -x", None, "test", "pytest"),
    ("uv run scripts/ingest.py --since 30d", None, "python", "scripts/ingest.py"),
    ("pipenv run python app/main.py", None, "python", "app/main.py"),
    ("node scripts/build.mjs && echo done", None, "node", "scripts/build.mjs"),
    ("bun run src/server.ts", None, "node", "src/server.ts"),
    ("npx -y prettier --write .", None, "node", "npx prettier"),
    ("npx vitest run", None, "test", "vitest"),
    ("npm run build --silent", None, "npm-script", "build"),
    ("pnpm --filter web lint", None, "npm-script", "lint"),
    ("yarn test", None, "npm-script", "test"),
    ("bun run dev", None, "npm-script", "dev"),
    ("npm install", None, "npm", "npm install"),
    ("bash tools/sop/gate.sh --strict", None, "shell-script", "sop/gate.sh"),
    ("./scripts/deploy.sh prod", None, "shell-script", "scripts/deploy.sh"),
    ("make -C build test", None, "make", "test"),
    ("git -C /repo diff --stat | tail -1", None, "git", "diff"),
    ("git status --short", None, "git", "status"),
    ("gh pr view 12 --json state", None, "gh", "pr"),
    ("docker compose up -d web", None, "docker", "compose up"),
    ("docker ps", None, "docker", "ps"),
    ("cargo test --release", None, "test", "cargo test"),
    ("go test ./...", None, "test", "go test"),
    ("rg -n 'def import_mesh|plan_to' tools/ | head -20", None, "inspect", "rg"),
    ("find . -name '*.py' -newer x | wc -l", None, "inspect", "find"),
    ("ls -la reports/ && cat README.md", None, "inspect", "cat"),
    ("curl -s -H 'Authorization: Bearer abc' https://api.example.com/v1/items", None, "network", "curl api.example.com"),
    ("export FOO=1 && set -e && ./run.py", None, "python", "run.py"),
    ("S=/tmp/x; cd $S; for f in a b; do python3 tools/check.py $f; done", None, "python", "tools/check.py"),
    ("until ssh example-host test -f /x/done; do sleep 30; done", None, "ssh", "ssh example-host"),
    ("ssh example-host 'cd /h/p && python3 jobs/geo.py final'", None, "ssh", "ssh example-host > jobs/geo.py"),
    ("timeout 600 python3 train.py --epochs 3 2>&1 | tee log.txt", None, "python", "train.py"),
    ("FOO=bar BAZ=1 node cli.js", None, "node", "cli.js"),
    ("bash -lc 'cd /x && npm run test'", None, "npm-script", "test"),
    ("text(await tools.write_stdin({session_id:78951,chars:\"\"}))", None, "codex-js", "tools.write_stdin"),
    ("rsync -aR tools/city/road_kit.py example-host:/home/ops/", None, "rsync", "rsync"),
    ("mkdir -p out && cd out", None, "mkdir", "mkdir"),
    ("echo hi", None, "shell", "(no command)"),
]


@pytest.mark.parametrize("cmd,cwd,fam,tgt", CASES)
def test_parse(cmd, cwd, fam, tgt):
    r = parse(cmd)
    assert (r["cmd_cwd"], r["cmd_family"], r["cmd_target"]) == (cwd, fam, tgt)


def test_cd_prefix_stripped_from_text():
    r = parse("cd /a/b && git status")
    assert r["cmd_text"] == "git status" and r["cmd_cwd"] == "/a/b"
    r = parse("cd /a/b; cd /c && ls")  # only the first prefix is the cwd
    assert r["cmd_cwd"] == "/a/b" and r["cmd_text"] == "cd /c && ls"


def test_split_respects_quotes_and_heredoc():
    segs = split_segments("echo 'a && b' && python3 - <<'X'\nfoo && bar; baz\nX\nls | wc -l")
    assert [s for s, _ in segs] == ["echo 'a && b'", "python3 - <<'X'", "ls", "wc -l"]
    assert segs[1][1] is True


def test_text_truncated_and_masked():
    from obs.ingest import mask_secrets
    long = "python3 run.py " + "x" * 3000
    r = parse(long, mask=lambda s, n: mask_secrets(s, n))
    assert len(r["cmd_text"]) == 2000
    r = parse("curl -H 'Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz0123' https://h.io/x", mask=mask_secrets)
    assert "sk-abcdef" not in r["cmd_text"]
