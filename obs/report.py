"""Daily report from the Parquet events via DuckDB: reports/{date}.md/.json (aggregates with transcript
pointers), per-call ledgers in reports/{date}/*.csv (no payload text) and a self-contained reports/{date}/report.html."""
import csv, json, os, shutil
from datetime import date, timedelta
import duckdb
from . import excerpts
from .panels import THREADS, WRAP, coverage_text, source_coverage
from .prices import prices
from .queries import KIND, SHELL_IN, events_select, kind, lineage_view, spawns

README = """# reports

- `YYYY-MM-DD/excerpts/<item>.txt`: first 4 KB behind each ranked item, scanned and redacted before write (`excerpt` in the json).
- `YYYY-MM-DD/`: per-call ledgers (`tool-calls.csv`, `bash-commands.csv`, `subagents.csv`, `skills.csv`,
  `model-requests.csv`; one row per event, no payload text) and `report.html` (self-contained, opens offline).
- `YYYY-MM-DD.md` / `.json`: daily report, format version `schema_version: "1"` (json block at the end of each .md; layout defined in the skill's report-format.md).
- `latest.md`: copy of the newest daily report (rewritten by `obs report`).
- `summary-14d.md`: output of `obs summary --since 14d` (<= 40 lines).
- cost_usd: per-session totalCostUSD from cost-state lines, stored on the session's last llm_request (NULL elsewhere).
- `pointer: {file, line}` / `file:line`: 1-based line in the source transcript; `file` starts with its source
  (`claude/` = the Claude projects folder, `codex/` = the Codex sessions folder; roots in the data folder's sources.json).
  `obs verify-pointers --date D` re-opens a random selection and checks the hashes.
"""

EST = "estimate = text_bytes / 3.6 + 1,500 per image (approximation), not a tokenizer count"


def q(con, sql, *a):
    cur = con.execute(sql, list(a))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def ptr(r):
    """Move src_file/src_line out of a row into pointer {file, line}."""
    f, l = r.pop("src_file", None), r.pop("src_line", None)
    r["pointer"] = {"file": f, "line": int(l)} if f and l is not None else None
    return r


def fl(r):
    p = r.get("pointer")
    return f"{p['file']}:{p['line']}" if p else ""


def fnum(x):
    return float(x) if isinstance(x, float) else (int(x) if x is not None else 0)


def day_stats(con, d):
    t = q(con, """select
      coalesce(sum(input_tokens) filter (event_type='llm_request'),0) it,
      coalesce(sum(output_tokens) filter (event_type='llm_request'),0) ot,
      coalesce(sum(cache_read_tokens) filter (event_type='llm_request'),0) cr,
      count(distinct session_id) s,
      count(*) filter (event_type='tool_call') tc,
      count(*) filter (event_type='error') er,
      coalesce(sum(content_bytes) filter (event_type='tool_result'),0) rb,
      coalesce(sum(image_bytes) filter (event_type='tool_result'),0) ib,
      coalesce(sum(image_count) filter (event_type='tool_result'),0) ic,
      coalesce(sum(reported_cost_alloc_usd) filter (event_type='llm_request'),0)::double rc,
      coalesce(sum(list_cost_usd) filter (event_type='llm_request'),0)::double lc,
      count(*) filter (event_type='llm_request' and list_cost_usd is null) lu
      from ev where cast(ts as date)=cast(? as date)""", d)[0]
    return {k: fnum(v) for k, v in t.items()}


def run(d, data, out, mask=True):
    masker = None
    if mask:
        from .redact_reports import LedgerMasker
        masker = LedgerMasker()
    files = os.path.join(data, "events", "*", "*.parquet")
    con = duckdb.connect()
    con.execute(f"set threads={THREADS}")
    con.execute("set TimeZone='UTC'")  # days are UTC everywhere (DuckDB defaults to the host zone)
    import glob
    if not glob.glob(files):
        raise SystemExit(f"no parquet under {files}")
    con.execute(f"create view ev0 as select * exclude (sc, sl, st) from ({events_select(files)})")
    # files ingested before the harness column existed: derive it from the src_file prefix
    old = ("case split_part(src_file, '/', 1) when 'claude' then 'claude-code' when 'codex' then 'codex' "
           "when 'kimi' then 'kimi-code' else 'claude-code:' || substr(split_part(src_file, '/', 1), 8) end")
    have = "harness" in [r[0] for r in con.execute("describe ev0").fetchall()]
    sel = f"* replace (coalesce(harness, {old}) as harness)" if have else f"*, {old} as harness"
    con.execute(f"create view ev as select {sel} from ev0")
    con.execute(lineage_view(data))
    date.fromisoformat(d)  # validates; safe to inline
    D = f"cast(ts as date)=date '{d}'"
    cur = day_stats(con, d)
    totals = {"input_tokens": int(cur["it"]), "output_tokens": int(cur["ot"]),
              "cache_read_tokens": int(cur["cr"]), "sessions": int(cur["s"]),
              "tool_calls": int(cur["tc"]), "errors": int(cur["er"]), "estimated_cost_usd": round(cur["lc"], 2),
              "reported_cost_usd": round(cur["rc"], 2), "list_cost_usd": round(cur["lc"], 2),
              "list_unpriced_requests": int(cur["lu"]), "prices_as_of": prices().asof_text(),
              "tool_result_bytes": int(cur["rb"]), "image_bytes": int(cur["ib"]), "image_count": int(cur["ic"])}

    per_model = q(con, f"""select model, sum(input_tokens)::bigint input_tokens, sum(output_tokens)::bigint output_tokens,
      round(sum(list_cost_usd), 2)::double list_cost_usd, round(sum(reported_cost_alloc_usd), 2)::double reported_cost_alloc_usd, count(*)::bigint calls from ev where event_type='llm_request' and {D} group by 1 order by 2 desc""")

    by_harness = q(con, f"""select harness, count(*) filter (event_type='llm_request')::bigint requests,
      count(distinct session_id)::bigint sessions, count(*) filter (event_type='tool_call')::bigint tool_calls,
      coalesce(sum(content_bytes) filter (event_type='tool_result'),0)::bigint tool_result_bytes,
      coalesce(sum(input_tokens) filter (event_type='llm_request'),0)::bigint input_tokens,
      coalesce(sum(output_tokens) filter (event_type='llm_request'),0)::bigint output_tokens,
      round(coalesce(sum(list_cost_usd),0), 2)::double list_cost_usd
      from ev where {D} group by 1 order by requests desc, 1""")

    per_tool = q(con, f"""select coalesce(tool_name,'(unknown)') tool,
      count(*) filter (event_type='tool_call')::bigint calls,
      coalesce(sum(content_bytes) filter (event_type='tool_result'),0)::bigint output_bytes,
      coalesce(sum(estimated_tokens) filter (event_type='tool_result'),0)::bigint output_tokens,
      count(*) filter (event_type='tool_result' and is_error)::bigint errors,
      coalesce(sum(image_bytes) filter (event_type='tool_result'),0)::bigint image_bytes,
      coalesce(sum(image_count) filter (event_type='tool_result'),0)::bigint image_count
      from ev where {D} group by 1 order by output_bytes desc""")

    per_session = q(con, f"""with s as (select session_id,
        sum(input_tokens) filter (event_type='llm_request') it, sum(output_tokens) filter (event_type='llm_request') ot,
        count(*) filter (event_type='tool_call') tc, min(ts) a, max(ts) b, mode(model) filter (event_type='llm_request') model,
        arg_min(src_file, ts) f, arg_min(src_line, ts) l
        from ev where {D} group by 1)
      select session_id, coalesce(model,'unknown') model, coalesce(it,0)::bigint input_tokens, coalesce(ot,0)::bigint output_tokens,
        tc::bigint tool_calls, round(epoch(b-a)/60.0,1) duration_min, f src_file, l src_line
      from s order by it desc nulls last limit 10""")
    per_session = [ptr(r) for r in per_session]

    top20 = q(con, f"""with r as (select * from ev where event_type='tool_result' and {D}),
      c as (select result_sha256, count(*) n from ev where event_type='tool_result' and result_sha256 is not null group by 1)
      select r.session_id, coalesce(r.project,'?') lane, coalesce(r.tool_name,'(unknown)') tool, r.content_bytes::bigint bytes,
        coalesce(r.image_count,0)::bigint image_count, coalesce(r.estimated_tokens,0)::bigint est_tokens,
        coalesce(r.reread_turns,0)::bigint reread_turns, coalesce(r.reread_tokens,0)::bigint reread_tokens,
        coalesce(ss.n,0)::bigint session_requests, coalesce(ss.seg,1)::bigint segments,
        r.result_sha256 payload_hash, c.n::bigint count_same_sha, r.src_file, r.src_line
      from r join c using(result_sha256)
        left join (select session_id, count(*) n, max(segment_index)+1 seg from ev where event_type='llm_request' group by 1) ss
          on ss.session_id = r.session_id
      order by reread_tokens desc, r.content_bytes desc limit 20""")
    top20 = [ptr(r) for r in top20]
    largest = [{k: v for k, v in x.items() if k != "count_same_sha"} for x in top20[:10]]

    repeated = q(con, f"""select result_sha256 payload_hash, coalesce(mode(tool_name),'(unknown)') tool,
        count(*)::bigint occurrences, max(content_bytes)::bigint bytes_each,
        (max(content_bytes)*(count(*)-1))::bigint wasted_bytes, count(distinct session_id)::bigint sessions,
        coalesce(sum(reread_tokens),0)::bigint reread_tokens,
        arg_min(src_file, ts) src_file, arg_min(src_line, ts) src_line
      from ev where event_type='tool_result' and {D} and result_sha256 is not null
      group by 1 having count(*)>=3 and max(content_bytes)>0 order by reread_tokens desc, wasted_bytes desc limit 20""")
    repeated = [ptr(r) for r in repeated]

    # retry loops: same session + tool + input hash, >=5 calls inside any 10 minute window
    loops = q(con, f"""with c as (select session_id, tool_name, input_sha256, ts, tool_call_id, src_file, src_line from ev
        where event_type='tool_call' and input_sha256 is not null and {D}),
      w as (select *, count(*) over (partition by session_id, tool_name, input_sha256 order by epoch(ts)
              range between current row and 600 following) n from c),
      g as (select session_id, tool_name, input_sha256, max(n) attempts, arg_min(src_file, ts) src_file,
              arg_min(src_line, ts) src_line from w group by 1,2,3 having max(n)>=5),
      r as (select session_id, tool_name, input_sha256, count(*) filter (is_error) errs,
              coalesce(sum(estimated_tokens),0) toks from ev where event_type='tool_result' and {D} group by 1,2,3)
      select g.session_id, coalesce(g.tool_name,'(unknown)') tool, g.attempts::bigint attempts,
        case when r.errs>0 then 'tool_error' else 'identical_call_no_error' end error_class,
        coalesce(r.toks,0)::bigint tokens_spent, g.input_sha256 input_hash, g.src_file, g.src_line
      from g left join r using(session_id, tool_name, input_sha256) order by attempts desc limit 20""")
    loops = [ptr(r) for r in loops]

    prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
    pv = day_stats(con, prev)
    def pct(a, b):
        return None if not b else round(100.0 * (a - b) / b, 1)
    spike = [("input_tokens", cur["it"], pv["it"]), ("output_tokens", cur["ot"], pv["ot"]),
             ("tool_calls", cur["tc"], pv["tc"]), ("tool_result_bytes", cur["rb"], pv["rb"])]
    contrib = q(con, f"""with t as (select coalesce(tool_name,'(unknown)') tool,
        sum(content_bytes) filter (cast(ts as date)=date '{d}') cur,
        coalesce(sum(content_bytes) filter (cast(ts as date)=date '{prev}'),0) prev
        from ev where event_type='tool_result' group by 1)
      select tool, coalesce(cur,0)::bigint cur, prev::bigint prev, (coalesce(cur,0)-prev)::bigint delta from t order by delta desc limit 3""")

    cp = q(con, f"""select coalesce(detail,'unknown') trigger, count(*)::bigint n from ev
      where event_type='compaction' and {D} group by 1""")
    cn = {r["trigger"]: r["n"] for r in cp}
    segs = q(con, f"""with g as (select compaction_id, count(*) n from ev where event_type='llm_request'
        and compaction_id is not null and {D} group by 1)
      select count(*)::bigint segments, coalesce(median(n),0)::bigint median_requests,
        coalesce(quantile_cont(n,0.95),0)::bigint p95_requests from g""")[0]
    ctop = q(con, f"""select session_id, count(*)::bigint compactions, count(*) filter (detail='manual')::bigint manual,
        count(*) filter (detail='auto')::bigint auto, count(*) filter (coalesce(detail,'unknown')='unknown')::bigint unknown
      from ev where event_type='compaction' and {D} group by 1 order by compactions desc limit 10""")
    compactions = {"total": sum(cn.values()), "manual": cn.get("manual", 0), "auto": cn.get("auto", 0),
                   "unknown": cn.get("unknown", 0), "segments": segs["segments"],
                   "median_segment_requests": segs["median_requests"], "p95_segment_requests": segs["p95_requests"],
                   "top_sessions": ctop}
    dq = {}
    p = os.path.join(data, "ingest_stats.json")
    if os.path.exists(p):
        dq = json.load(open(p))

    cv = q(con, "select strftime(min(ts), '%Y-%m-%dT%H:%M:%SZ') e, strftime(max(ts), '%Y-%m-%dT%H:%M:%SZ') l from ev")[0]
    srcs = source_coverage(data)
    cov_line = (f"- data coverage: {coverage_text(cv['e'], cv['l'])}; days before {cv['e'][:10]} are no data, not zero"
                + (" (" + "; ".join(f"{x['name']}: {x['files']} files, transcripts modified {(x['earliest'] or '?')[:10]} to {(x['latest'] or '?')[:10]}" for x in srcs) + ")" if srcs else "")
                + "\n") if cv["e"] else "- data coverage: no events recorded\n"

    # excerpts: the span behind every ranked item, scanned + redacted before write (obs/excerpts.py)
    ex = excerpts.write(data, os.path.join(out, d), masker=masker, groups=[
        ("largest", "result", "payload_hash", top20), ("repeat", "result", "payload_hash", repeated),
        ("loop", "input", "input_hash", loops), ("session", "session", "session_id", per_session)])
    largest = [{k: v for k, v in x.items() if k != "count_same_sha"} for x in top20[:10]]

    from .panels import Store, View, p_readtypes
    try:
        rt = p_readtypes(View(Store(data), day=d))
    except Exception:
        rt = {"kinds": [], "files": [], "hint": ""}
    mk = (lambda x: masker.text(x)) if masker else (lambda x: x)
    read_by_file_type = {
        "total_bytes": int(rt.get("total_bytes") or 0),
        "by_kind": [{"kind": k["kind"], "reads": k["calls"], "bytes": k["bytes"], "reread_tokens": k["tail"],
                     "image_count": k["images"], "share": k["share"]} for k in rt["kinds"]],
        "top_files": [{"path": mk(f["path"]), "kind": f["kind"], "ext": f["ext"], "reads": f["calls"], "bytes": f["bytes"],
                       "reread_tokens": f["tail"], "sessions": f["sessions"], "lane": f["lane"]} for f in rt["files"]],
        "what_to_do": rt.get("hint", ""),
    }
    agg = {"schema_version": "1", "date": d, "totals": totals, "per_model": per_model, "by_harness": by_harness, "per_tool": per_tool,
           "per_session": per_session, "largest_tool_outputs": largest,
           "repeated_payloads": [{k: v for k, v in x.items() if k != "sessions"} for x in repeated],
           "retry_loops": loops, "read_by_file_type": read_by_file_type,
           "compactions": compactions}

    def tbl(rows, cols):
        if not rows:
            return "_none_\n"
        s = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
        for r in rows:
            s += "| " + " | ".join(fl(r) if c == "file:line" else str(r.get(c, "")) if c != "payload_hash" else str(r[c])[:12]
                                   for c in cols) + " |\n"
        return s

    md = [f"# Agent Observatory daily report {d}\n",
          f"Cost: **reported ${totals['reported_cost_usd']:,.2f}** (harness cost-state, where recorded) | "
          f"**list price ${totals['list_cost_usd']:,.2f}** ({totals['prices_as_of']}; "
          f"{totals['list_unpriced_requests']:,} requests unpriced: model missing or rate null).\n",
          f"From Claude Code and Codex transcripts (UTC day). {totals['sessions']} sessions, "
          f"{totals['input_tokens']:,} input / {totals['output_tokens']:,} output / {totals['cache_read_tokens']:,} cache-read tokens, "
          f"{totals['tool_calls']:,} tool calls, {totals['errors']} errors. "
          f"Tool results: {totals['tool_result_bytes']:,} bytes, {totals['image_bytes'] / (totals['tool_result_bytes'] or 1):.0%} of them images "
          f"({totals['image_count']:,} images). estimated_cost_usd = list_cost_usd (input, cached input and output tokens at list prices; cache writes at the input rate); "
          f"tool output tokens are {EST}.\n",
          "## Spike vs previous day (" + prev + ")\n",
          "| metric | today | previous | delta % |\n|---|---|---|---|\n" +
          "".join(f"| {n} | {int(a):,} | {int(b):,} | {pct(a, b)} |\n" for n, a, b in spike),
          "\nTop contributors (tool result bytes):\n\n" + tbl(contrib, ["tool", "cur", "prev", "delta"]),
          "\n## By harness\n\n" + tbl(by_harness, ["harness", "requests", "sessions", "tool_calls", "tool_result_bytes", "input_tokens", "output_tokens", "list_cost_usd"]),
          "\n## Largest tool outputs by re-read tokens (top 20)\n\n"
          "reread_tokens = est_tokens x reread_turns (re-read tokens, cumulative: tokens x turns re-read until the next compaction; session_requests and segments describe the whole session).\n\n"
          + tbl(top20, ["session_id", "lane", "tool", "reread_tokens", "reread_turns", "session_requests", "segments", "bytes", "image_count", "payload_hash", "count_same_sha", "file:line"])
          + f"\nWhat to do: {WRAP}.\n",
          "\n## Repeated payloads (sha seen >= 3 times, by re-read tokens)\n\n" + tbl(repeated, ["payload_hash", "tool", "occurrences", "reread_tokens", "bytes_each", "wasted_bytes", "sessions", "file:line"]),
          "\n## Read by file type (bytes returned by file reads: Read tool, Codex cat/sed/head/tail and view_image)\n\n"
          + tbl(read_by_file_type["by_kind"], ["kind", "reads", "bytes", "reread_tokens", "image_count", "share"])
          + "\nTop files by re-read tokens:\n\n"
          + tbl(read_by_file_type["top_files"], ["path", "kind", "reads", "bytes", "reread_tokens", "sessions", "lane"])
          + f"\nWhat to do: {read_by_file_type['what_to_do']}\n",
          "\n## Retry loops (same tool + input hash >= 5 within 10 min)\n\n" + tbl(loops, ["session_id", "tool", "attempts", "error_class", "tokens_spent", "file:line"]),
          "\n## Compactions\n\n"
          f"{compactions['total']} compactions: {compactions['manual']} manual, {compactions['auto']} auto, {compactions['unknown']} unknown "
          f"(unknown = the transcript does not say; Codex records carry no trigger). Median segment {compactions['median_segment_requests']} requests, "
          f"p95 {compactions['p95_segment_requests']}.\n\n"
          + tbl(ctop, ["session_id", "compactions", "manual", "auto", "unknown"]),
          "\n## Heaviest sessions (by input tokens)\n\n" + tbl(per_session, ["session_id", "model", "input_tokens", "output_tokens", "tool_calls", "duration_min", "file:line"]),
          "\n## Data quality\n\n"
          f"- files read: {dq.get('files', 'n/a')}; lines read: {dq.get('lines', 'n/a')}; lines skipped (unparseable): {dq.get('skipped', 'n/a')}\n"
          + cov_line +
          "- source: Claude Code session transcripts (plus subagent transcripts, agent='subagent', parent_session_id set) and Codex rollouts; days are UTC\n"
          "- file:line / pointer: 1-based line in the transcript, path relative to its source root (claude/ or codex/)\n"
          f"- ledgers: {d}/tool-calls.csv, bash-commands.csv, subagents.csv, skills.csv, model-requests.csv; report: {d}/report.html\n"
          f"- token estimates for tool output: {EST}; llm token counts are provider-reported usage, de-duplicated per message id\n"
          "- cost_usd only where a cost-state line exists (on the session's last llm_request); latency_ms not present\n"
          f"- excerpts: {d}/excerpts/<item>.txt for every ranked item (first 4 KB of the pointed span, images as [image N bytes], "
          f"scanned and redacted before write): {ex['written']} written, {ex['missing']} unavailable, {ex['findings_masked']} scanner findings masked\n",
          "\n## Aggregates\n\n```json\n" + json.dumps(agg, indent=2) + "\n```\n"]
    os.makedirs(out, exist_ok=True)
    open(os.path.join(out, d + ".md"), "w").write("\n".join(md))
    json.dump(agg, open(os.path.join(out, d + ".json"), "w"), indent=2)
    days = sorted(f[:-3] for f in os.listdir(out) if len(f) == 13 and f.endswith(".md") and f[:4].isdigit())
    if days and days[-1] == d:
        shutil.copyfile(os.path.join(out, d + ".md"), os.path.join(out, "latest.md"))
    open(os.path.join(out, "README.md"), "w").write(README)
    n = write_ledgers(con, d, os.path.join(out, d), masker)
    kb = write_html(data, d, os.path.join(out, d), masker)
    print(f"wrote {out}/{d}.md and .json; ledgers {n}; report.html {kb} KB")
    return 0


LEDGERS = {
    "tool-calls.csv": f"""with r as (select session_id, tool_call_id, sum(content_bytes) b, sum(estimated_tokens) t,
          max(duration_ms) dur, bool_or(is_error) err from ev where event_type='tool_result' group by 1,2)
        select strftime(c.ts, '%Y-%m-%dT%H:%M:%S.%gZ') ts, c.session_id session, {kind('c')} agent_kind,
          c.tool_name tool, left(c.input_sha256, 12) input_sha12, r.b result_bytes, r.t est_tokens, r.dur duration_ms,
          coalesce(r.err, false) is_error, c.src_file || ':' || c.src_line "file:line"
        from ev c left join r on r.session_id = c.session_id and r.tool_call_id = c.tool_call_id
        where c.event_type='tool_call' and {{D}} order by c.ts""",
    "bash-commands.csv": f"""with r as (select session_id, tool_call_id, sum(content_bytes) b, sum(estimated_tokens) t,
          sum(reread_tokens) tail, max(duration_ms) dur, bool_or(is_error) err, max(exit_code) ec
          from ev where event_type='tool_result' group by 1,2)
        select strftime(c.ts, '%Y-%m-%dT%H:%M:%S.%gZ') ts, c.session_id session, c.cmd_family, c.cmd_target, c.cmd_cwd,
          left(c.cmd_text, 200) cmd_text, r.t est_tokens, r.tail reread_tokens, r.dur duration_ms,
          r.b result_bytes,
          case when r.session_id is null then 'no result' when r.ec is not null and r.ec <> 0 then 'exit ' || r.ec
               when r.err then 'error' else 'ok' end exit,
          c.src_file || ':' || c.src_line "file:line"
        from ev c left join r on r.session_id = c.session_id and r.tool_call_id = c.tool_call_id
        where c.event_type='tool_call' and c.tool_name in {SHELL_IN} and {{D}} order by c.ts""",
    "subagents.csv": None,  # built from queries.spawns
    "skills.csv": f"""select strftime(ts, '%Y-%m-%dT%H:%M:%S.%gZ') ts, session_id session, skill_name skill, {KIND} agent_kind,
          src_file || ':' || src_line "file:line"
        from ev where event_type='tool_call' and skill_name is not null and {{D}} order by ts""",
    "model-requests.csv": """select strftime(ts, '%Y-%m-%dT%H:%M:%S.%gZ') ts, session_id session, model, provider,
          input_tokens input, cache_read_tokens cache_read, cache_write_tokens cache_write, output_tokens output,
          latency_ms, src_file || ':' || src_line "file:line"
        from ev where event_type='llm_request' and {D} order by ts""",
}


def write_ledgers(con, d, folder, masker=None):
    """One CSV per ledger, header row, one row per event; payload text is never written."""
    os.makedirs(folder, exist_ok=True)
    D = f"cast(ts as date)=date '{d}'"
    n = 0
    for name, sql in LEDGERS.items():
        if sql is None:
            sql = f"""select strftime(ts, '%Y-%m-%dT%H:%M:%S.%gZ') ts, parent_session, subagent_session, subagent_type,
                model_requested, model_used, context_tokens, output_tokens, tool_calls, duration_ms, pointer "file:line", linked_by
                from ({spawns(D)}) order by ts"""
        else:
            sql = sql.replace("{D}", D.replace("ts", "c.ts") if " c " in sql or "ev c" in sql else D)
        cur = con.execute(sql)
        cols = [x[0] for x in cur.description]
        tmp = os.path.join(folder, name + ".tmp")
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for row in cur.fetchall():
                row = ["" if v is None else ("true" if v else "false") if isinstance(v, bool) else v for v in row]
                if masker:
                    row = masker.row(cols, [v if isinstance(v, str) else str(v) for v in row], only_text=True) if "cmd_text" in cols else row
                w.writerow(row)
        os.replace(tmp, os.path.join(folder, name))
        n += 1
    return n


def mask_tree(o, masker):
    """Run every string leaf (and dict key) of the panel data through the span masker."""
    if isinstance(o, str):
        return masker.text(o)
    if isinstance(o, list):
        return [mask_tree(i, masker) for i in o]
    if isinstance(o, dict):
        return {(masker.text(k) if isinstance(k, str) else k): mask_tree(v, masker) for k, v in o.items()}
    return o


def write_html(data, d, folder, masker=None):
    """Self-contained report.html: inline CSS, uPlot, app.js and this day's panel data. Returns size in KB."""
    from .panels import Store, all_panels
    st = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    rd = lambda n: open(os.path.join(st, n), encoding="utf-8").read()
    panels = json.loads(json.dumps(all_panels(Store(data), d), default=str))
    if masker:
        panels = mask_tree(panels, masker)
    payload = json.dumps(panels, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    html = rd("index.html")
    html = html.replace('<link rel="stylesheet" href="/static/uPlot.min.css">', "<style>" + rd("uPlot.min.css") + "</style>")
    html = html.replace('<link rel="stylesheet" href="/static/app.css">', "<style>" + rd("app.css") + "</style>")
    html = html.replace('<script src="/static/uPlot.iife.min.js"></script>',
                        "<script>" + rd("uPlot.iife.min.js").replace("</script", "<\\/script") + "</script>")
    html = html.replace('<script src="/static/app.js"></script>',
                        "<script>window.OBS_DATA=" + payload + ";</script>\n<script>" + rd("app.js") + "</script>")
    html = html.replace("<title>Agent Observatory</title>", f"<title>Agent Observatory {d}</title>")
    html = html.replace("<h1>Agent Observatory</h1>", f"<h1>Agent Observatory, {d}</h1>")
    html = html.replace("Black-box recorder for your coding agents. Private, on this NAS, read-only.",
                        "Daily report for one UTC day. Everything on this page is inside this file; it works offline.")
    html = html.replace("Reading the recorder&hellip;", "Daily report")
    for x, y in (("<h2>Context processed per day</h2>", "<h2>Context processed per hour</h2>"),
                 ("tokens, UTC days", "tokens, UTC hours"), ("tokens, median and p95 per day", "tokens, median and p95 per hour")):
        html = html.replace(x, y)
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "report.html")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(p + ".tmp", p)
    return round(os.path.getsize(p) / 1024)
