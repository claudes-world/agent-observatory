"""Deterministic SYNTHETIC Claude Code + Codex transcripts in the on-disk layouts the parsers read.

Layout under --out:
  claude/<project>/<session>.jsonl and claude/<project>/<session>/subagents/agent-*.jsonl
  codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl
Everything is generated filler (projects acme-*, ids from the seed). No real prompts, no secrets.
Day index 0..days-1 ends today (UTC); day 12 (0-based) is the spike day when days >= 13.
"""
import base64, json, os, random, struct, uuid, zlib
from datetime import datetime, timedelta, timezone

PROJECTS = ["acme-api", "acme-web", "acme-infra"]
CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5"]
CODEX_MODELS = [("gpt-6.1-sol", "openai"), ("deepseek-chat", "deepseek"), ("glm-5", "zhipu")]
CLAUDE_TOOLS = ["Read", "Bash", "Grep", "Edit", "Glob"]
CODEX_TOOLS = ["shell", "read_file", "apply_patch", "grep_files"]
WORDS = "alpha bravo cargo delta echo fable gamma harbor ion jade kilo lumen mango nova orbit pylon quartz relay sigma tango umbra vector willow xenon yarrow zephyr".split()
BASH_CMDS = ["make test", "git status --short", "npm run build", "pytest -q tests/", "docker compose ps",
             "rg -n TODO src/", "ls -la build/", "git diff --stat"]
SKILLS = ["code-review", "release-notes", "db-migrate", "incident-triage"]
SUBAGENT_TYPES = ["general-purpose", "Explore", "code-reviewer"]
SPIKE_DAY = 12
IMAGE_DAYS = (11, 13)  # day indexes whose first claude session carries screenshot-like tool_results
NORMAL_INPUT = 1_200_000
SPIKE_INPUT = 4_300_000


def blob(rng, n, tag):
    head = f"[synthetic filler {tag}]\n"
    lines, size = [head], len(head)
    while size < n:
        l = f"{rng.getrandbits(32):08x} " + " ".join(rng.choice(WORDS) for _ in range(9)) + "\n"
        lines.append(l)
        size += len(l)
    return "".join(lines)[:n]


def png_b64(rng, w=300, h=300):
    """A valid w x h RGB PNG of noise (incompressible, so ~270 KB raw / ~360 KB base64), base64-encoded."""
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rng.randbytes(w * 3) for _ in range(h))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


def add_images(seed, d, steps):
    """On IMAGE_DAYS, 4 tool results get 1-3 PNG image blocks (own RNG: the rest of the fixture is unchanged)."""
    rng = random.Random(f"img-{seed}-{d}")
    cand = [s for s in steps if s.tool in CLAUDE_TOOLS and s.tool != "Skill" and not s.agent_id]
    for s, n in zip(rng.sample(cand, min(4, len(cand))), (1, 2, 3, 2)):
        s.images = [png_b64(rng) for _ in range(n)]


def uid(rng):
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


class Step:
    """One model request plus an optional tool call and its result."""
    def __init__(self, t, tool, inp, out, err=False, dur=2):
        self.t, self.tool, self.inp, self.out, self.err, self.dur = t, tool, inp, out, err, dur
        self.tid = self.agent_id = self.model_used = None
        self.images = []
        self.uncached = self.cache_read = self.cache_write = self.output = 0


class SubSteps(list):
    """A subagent's steps plus the parent's spawn Step (set in run)."""
    spawn = None


def plan_session(rng, start, spike, kind, big_reads):
    """Returns list of Steps. spike: 'fetch' (61 identical payloads + 14-call retry loop) or None."""
    tools = CLAUDE_TOOLS if kind == "claude" else CODEX_TOOLS
    n = rng.randint(14, 40)
    steps, t = [], start
    for i in range(n):
        t += timedelta(seconds=rng.randint(15, 90))
        tool = rng.choice(tools)
        size = rng.choice([300, 800, 1500, 3000, 6000, 12000, 20000])
        inp, dur = {"arg": f"synthetic-{i}-{rng.getrandbits(24):06x}"}, rng.choice([1, 1, 2, 3])
        if tool == "Bash":
            inp, dur = {"command": rng.choice(BASH_CMDS) + f"  # step {i}"}, rng.choice([1, 2, 3, 5, 8, 14, 30, 55])
        elif tool == "shell":
            inp, dur = {"command": ["bash", "-lc", rng.choice(BASH_CMDS)]}, rng.choice([1, 2, 3, 5, 8, 14, 30])
        steps.append(Step(t, tool, inp, blob(rng, size, f"{tool}-{i}"), dur=dur))
    if rng.random() < 0.6:  # a skill near the start of the session
        sk = rng.choice(SKILLS)
        if kind == "claude":
            steps[0].tool, steps[0].inp = "Skill", {"skill": sk}
        else:
            steps[0].tool, steps[0].inp = "shell", {"command": ["bash", "-lc", f"sed -n 1,80p ~/.codex/skills/{sk}/SKILL.md"]}
    if big_reads:
        for _ in range(big_reads):
            s = steps[rng.randrange(len(steps))]
            s.tool = tools[0]
            s.out = blob(rng, 600_000, "big-read-" + uid(rng)[:8])
    if spike:
        payload = blob(random.Random("fetch-logs-payload"), 80_000, "fetch_logs-fixed")
        extra, tt = [], t
        for i in range(61):
            tt += timedelta(seconds=rng.randint(20, 60))
            extra.append(Step(tt, "fetch_logs", {"service": "acme-api", "page": i}, payload))
        for i in range(14):
            tt += timedelta(seconds=25)
            extra.append(Step(tt, "Bash", {"command": "make test-integration  # synthetic retry loop"},
                              "Exit code 2\n[synthetic] ERROR integration suite failed: upstream timeout\n", err=True, dur=20))
        steps += extra
    return steps


def assign_usage(rng, groups, target):
    """groups: list of step lists (one per file). Scale uncached input so the day sums to ~target."""
    allsteps = [s for g in groups for s in g]
    w = []
    for g in groups:
        prev = 0
        for s in g:
            w.append(1.0 + rng.random() + prev / 15000.0)
            prev = len(s.out)
    tot = sum(w)
    for s, x in zip(allsteps, w):
        s.uncached = max(200, int(target * x / tot))
    for g in groups:
        n = len(g)
        base = rng.randint(50_000, 90_000)
        top = rng.randint(220_000, 300_000)
        for i, s in enumerate(g):
            s.cache_read = max(50_000, min(300_000, int(base + (top - base) * i / max(n - 1, 1) + rng.randint(-8000, 8000))))
            s.cache_write = rng.randint(500, 6000)
            s.output = rng.randint(200, 4000)


def write_claude(path, sid, project, model, steps, sub, rng):
    cwd = f"/srv/synth/{project}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    total = 0.0
    with open(path, "w") as f:
        def w(o):
            f.write(json.dumps(o) + "\n")
        w({"type": "user", "timestamp": iso(steps[0].t - timedelta(seconds=5)), "sessionId": sid, "cwd": cwd,
           "isSidechain": sub, "message": {"role": "user", "content": "synthetic task prompt (generated)"}})
        for s in steps:
            tid, mid = s.tid or "toolu_" + uid(rng)[:16], "msg_" + uid(rng)[:16]
            u = {"input_tokens": s.uncached, "output_tokens": s.output,
                 "cache_read_input_tokens": s.cache_read, "cache_creation_input_tokens": s.cache_write}
            total += (s.uncached * 5 + s.cache_read * 0.5 + s.cache_write * 6 + s.output * 25) / 1e6
            w({"type": "assistant", "timestamp": iso(s.t), "sessionId": sid, "cwd": cwd, "isSidechain": sub,
               "uuid": uid(rng), "message": {"id": mid, "role": "assistant", "model": model, "usage": u,
               "content": [{"type": "tool_use", "id": tid, "name": s.tool, "input": s.inp}]}})
            body = s.out if not s.images else [{"type": "text", "text": s.out}] + [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}} for b in s.images]
            res = {"type": "user", "timestamp": iso(s.t + timedelta(seconds=s.dur)), "sessionId": sid, "cwd": cwd,
                   "isSidechain": sub, "message": {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": tid, "content": body, "is_error": s.err}]}}
            if s.agent_id:
                res["toolUseResult"] = {"status": "completed", "agentId": s.agent_id, "agentType": s.inp.get("subagent_type"),
                                        "resolvedModel": s.model_used, "totalDurationMs": s.dur * 1000}
            w(res)
        w({"type": "cost-state", "totalCostUSD": round(total, 4)})
    ts = (max(s.t + timedelta(seconds=s.dur) for s in steps)).timestamp()
    os.utime(path, (ts, ts))


def write_codex(path, sid, project, model, prov, steps, parent, rng, agent_path=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        def w(t, o, p):
            f.write(json.dumps({"timestamp": iso(t), "type": o, "payload": p}) + "\n")
        t0 = steps[0].t - timedelta(seconds=5)
        meta = {"id": sid, "cwd": f"/srv/synth/{project}", "model_provider": prov}
        if parent:
            meta["parent_thread_id"] = parent
            meta["agent_path"] = agent_path
        w(t0, "session_meta", meta)
        w(t0, "turn_context", {"model": model})
        for s in steps:
            cid = "call_" + uid(rng)[:16]
            w(s.t, "token_usage_record", {"response_id": "resp_" + uid(rng)[:16], "usage": {
                "input_tokens": s.uncached + s.cache_read, "cached_input_tokens": s.cache_read,
                "output_tokens": s.output}})
            w(s.t, "response_item", {"type": "function_call", "call_id": cid, "name": s.tool,
                                      "arguments": json.dumps(s.inp)})
            w(s.t + timedelta(seconds=s.dur), "response_item", {"type": "function_call_output", "call_id": cid,
                                                                 "output": s.out})
    ts = (max(s.t + timedelta(seconds=s.dur) for s in steps)).timestamp()
    os.utime(path, (ts, ts))


def run(out, days=14, seed=7, start=None):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    first = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) if start else today - timedelta(days=days - 1)
    nfiles = 0
    nsess = 0
    for d in range(days):
        day = first + timedelta(days=d)
        rng = random.Random(f"{seed}-{d}")
        spike = d == SPIKE_DAY
        ns = rng.randint(2, 6)
        specs = []  # (kind, project, model, prov, steps, subagent_steps list)
        for k in range(ns):
            start_t = day + timedelta(hours=8, minutes=rng.randint(0, 600))
            kind = "codex" if rng.random() < 0.3 else "claude"
            project = PROJECTS[(d + k) % 3] if k else PROJECTS[0]
            sp = spike and k == 0
            if sp:
                kind, project = "claude", "acme-api"
            big = 1 if (d in (3, 8, 12) and k == 0) or (d == 5 and k == 1) else 0
            steps = plan_session(rng, start_t, sp, kind, big)
            subs = []
            if k % 3 == 0:
                for _ in range(rng.randint(1, 2)):
                    subs.append(SubSteps(plan_session(rng, start_t + timedelta(minutes=2), False, kind, 0)[:rng.randint(6, 12)]))
            if kind == "claude":
                model, prov = rng.choice(CLAUDE_MODELS), "anthropic"
            else:
                model, prov = CODEX_MODELS[rng.randrange(3)]
            if sp:
                model = "claude-opus-5-5"
            for j, sub in enumerate(subs):  # the parent's spawn call, answered when the subagent finishes
                span = int((sub[-1].t - sub[0].t).total_seconds()) + 10
                at = sub[0].t - timedelta(seconds=3)
                if kind == "claude":
                    inp = {"subagent_type": rng.choice(SUBAGENT_TYPES), "description": f"synthetic subtask {j}",
                           "prompt": "synthetic delegated task (generated)"}
                    if rng.random() < 0.5:
                        inp["model"] = "sonnet"
                    st = Step(at, "Agent", inp, "synthetic subagent summary (generated)", dur=span)
                    st.tid, st.agent_id = "toolu_" + uid(rng)[:16], uid(rng).replace("-", "")[:17]
                    st.model_used = "claude-sonnet-5-5" if inp.get("model") else model
                else:
                    inp = {"task_name": f"task_{d}_{k}_{j}", "message": "synthetic delegated task (generated)"}
                    if rng.random() < 0.5:
                        inp["model"] = model
                    st = Step(at, "spawn_agent", inp, json.dumps({"agent_path": "/root/" + inp["task_name"]}), dur=1)
                sub.spawn = st
                steps.append(st)
            steps.sort(key=lambda x: x.t)
            specs.append((kind, project, model, prov, steps, subs))
        if d in IMAGE_DAYS:
            first_claude = next((sp_ for sp_ in specs if sp_[0] == "claude"), None)
            if first_claude:
                add_images(seed, d, first_claude[4])
        groups = [g for sp_ in specs for g in [sp_[4]] + sp_[5]]
        target = SPIKE_INPUT if spike else int(NORMAL_INPUT * rng.uniform(0.9, 1.1))
        assign_usage(rng, groups, target)
        for kind, project, model, prov, steps, subs in specs:
            sid = uid(rng)
            nsess += 1
            if kind == "claude":
                p = os.path.join(out, "claude", project, sid + ".jsonl")
                write_claude(p, sid, project, model, steps, False, rng)
                nfiles += 1
                for sub in subs:
                    st = sub.spawn
                    base_p = os.path.join(out, "claude", project, sid, "subagents", "agent-" + st.agent_id)
                    write_claude(base_p + ".jsonl", sid, project, st.model_used, sub, True, rng)
                    with open(base_p + ".meta.json", "w") as f:
                        json.dump({"agentType": st.inp["subagent_type"], "toolUseId": st.tid,
                                   "model": st.inp.get("model")}, f)
                    nfiles += 1
            else:
                def cp(s_id, st):
                    t = st[0].t
                    return os.path.join(out, "codex", "sessions", t.strftime("%Y/%m/%d"),
                                        f"rollout-{t.strftime('%Y-%m-%dT%H-%M-%S')}-{s_id}.jsonl")
                write_codex(cp(sid, steps), sid, project, model, prov, steps, None, rng)
                nfiles += 1
                for sub in subs:
                    aid = uid(rng)
                    write_codex(cp(aid, sub), aid, project, model, prov, sub, sid, rng,
                                "/root/" + sub.spawn.inp["task_name"])
                    nfiles += 1
    print(json.dumps({"out": out, "first_day": first.strftime("%Y-%m-%d"), "days": days, "sessions": nsess, "files": nfiles}))
    return 0
