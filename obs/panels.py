"""Panel computations shared by the LAN dashboard (/api/*) and the self-contained daily report.html.

A View is either a trailing range of UTC days ending at the newest recorded day (dashboard) or one
UTC day (report.html, charts bucketed by hour). Every panel returns {"rows": [...], "hint": "...", ...}.
"""
import glob, json, os, re, statistics, threading
from datetime import date, datetime, timedelta, timezone
import duckdb
from .ingest import IMAGE_TOKENS
from .prices import prices
from .queries import KIND, LANE, POINTER, SHELL_IN, SPAWN_IN, events_select, lineage_view, spawns

# DuckDB worker threads per connection. Each thread can hold its own handle on every parquet file of a scan,
# so this (times concurrent queries) bounds open files; 4 keeps the dashboard far below a 1024 fd limit.
THREADS = max(1, int(os.environ.get("OBS_DUCKDB_THREADS", "4")))

RANGES = (7, 14, 30)
BIG = 256 * 1024         # "very large single output" threshold
# owner decision (spec "Ranking metric"): the fix is at the read boundary, never cap/truncate/page
WRAP = ("wrap the command so it prints one summary line (pass/fail + counts); read the full output only on failure, "
        "then grep for the latest error")
MARKER = ("2026-10-23", "executor-wrapper rule")  # before/after marker (spec "Findings log")
PAUSED = 3600 * 1000     # a shell result more than 1 h after its call is a paused/resumed session, not run time


class PanelError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def human_bytes(b):
    b = float(b or 0)
    for u, n in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= n:
            return f"{b / n:.1f} {u}"
    return f"{int(b)} B"


def human_tokens(t):
    t = float(t or 0)
    for u, n in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if t >= n:
            return f"{t / n:.1f}{u}"
    return f"{int(t)}"


def secs(ms):
    ms = float(ms or 0)
    return f"{ms / 60000:.1f} min" if ms >= 120000 else f"{ms / 1000:.1f} s"


def pl(n, word, many=None):
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def short(sid):
    return (sid or "?").replace("agent-", "")[:8]


class Store:
    """The parquet store. shared=True (dashboard): one long-lived in-memory DuckDB holding the events as a table,
    reloaded only when the parquet file set changes (path, size, mtime); every query runs on a cursor of it,
    so a request opens no parquet files at all. shared=False (reports, CLI): a fresh connection per query."""
    def __init__(self, data, shared=False):
        self.data = os.path.abspath(data)
        self.files = os.path.join(self.data, "events", "*", "*.parquet")
        self.lock = threading.Lock()
        self.shared = shared
        self._con, self._sig = None, None

    def file_list(self):
        return sorted(glob.glob(self.files))

    def connect(self):
        """(connection, events source SQL); the caller closes it. Shared mode returns a cursor over the cached table."""
        fs = self.file_list()
        if not fs:
            raise PanelError(503, "no events recorded yet; the first ingest has not finished")
        if not self.shared:
            con = duckdb.connect()
            con.execute(f"set threads={THREADS}")
            return con, f"select * exclude (sc, sl, st) from ({events_select(fs)})"
        sig = []
        for f in fs:
            try:
                s = os.stat(f)
                sig.append((f, s.st_size, s.st_mtime_ns))
            except FileNotFoundError:
                pass
        sig = tuple(sig)
        with self.lock:
            if self._con is None:
                self._con = duckdb.connect()
                self._con.execute(f"set threads={THREADS}")
                self._con.execute("set TimeZone='UTC'")
            if sig != self._sig:
                try:
                    self._con.execute("create or replace table events as select * exclude (sc, sl, st) from ("
                                      + events_select([x[0] for x in sig]) + ")")
                except duckdb.IOException as e:  # a file was replaced mid-read by the watcher
                    raise PanelError(503, f"events are being rewritten, retry: {e}")
                self._sig = sig
            return self._con.cursor(), "select * from events"


class View:
    """days: trailing range (dashboard). day: one ISO date (report.html)."""
    def __init__(self, st, days=None, day=None, harness=None):
        if day is None and days not in RANGES:
            raise PanelError(400, f"days must be one of {RANGES}")
        if day is not None:
            date.fromisoformat(day)  # validates; safe to inline
        self.st, self.days, self.day = st, days, day
        self.harness = [h for h in (harness or []) if h]
        for h in self.harness:
            if not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", h):
                raise PanelError(400, "bad harness name")
        self.bucket = "hour" if day else "day"
        self.span = "this day" if day else "this range"

    def q(self, sql, params=()):
        st = self.st
        con, src = st.connect()
        try:
            con.execute("set TimeZone='UTC'")
            con.execute(f"create temp view raw0 as {src}")
            # files ingested before the harness column existed: derive it from the src_file prefix
            old = ("case split_part(src_file, '/', 1) when 'claude' then 'claude-code' when 'codex' then 'codex' "
                   "when 'kimi' then 'kimi-code' else 'claude-code:' || substr(split_part(src_file, '/', 1), 8) end")
            cols0 = [r[0] for r in con.execute("describe raw0").fetchall()]
            have = "harness" in cols0
            hx = f"coalesce(harness, {old})" if have else old
            sel = f"* replace ({hx} as harness)" if have else f"*, {hx} as harness"
            # files ingested before the compaction columns existed: NULL until re-ingested (no guessed values)
            sel += "".join(f", null::{t} as {c}" for c, t in (("reread_turns", "bigint"), ("reread_tokens", "bigint"),
                           ("segment_index", "bigint"), ("compaction_id", "varchar"),
                           ("file_path", "varchar"), ("file_ext", "varchar"), ("file_kind", "varchar")) if c not in cols0)
            con.execute(f"create temp view rawall as select {sel} from raw0")
            hw = f" where harness in ({', '.join(repr(h) for h in self.harness)})" if self.harness else ""
            con.execute(f"create temp view raw as select * from rawall{hw}")
            con.execute(lineage_view(st.data).replace("create or replace view", "create or replace temp view", 1))
            if self.day:
                con.execute(f"create temp view ev as select * from raw where cast(ts as date) = date '{self.day}'")
            else:
                end = con.execute("select max(cast(ts as date)) from raw").fetchone()[0]
                con.execute(f"create temp view ev as select * from raw where cast(ts as date) > date '{end}' - interval {self.days} day")
            cur = con.execute(sql, list(params))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except duckdb.IOException as e:  # a file was replaced mid-read by the watcher
            raise PanelError(503, f"events are being rewritten, retry: {e}")
        finally:
            con.close()
        for r in rows:
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
                elif v is not None and type(v).__name__ == "Decimal":
                    r[k] = float(v)
        return rows

    def harnesses(self):
        """Every harness in the store (unfiltered), with request counts, for the header chips."""
        return self.q("select harness, count(*) filter (event_type='llm_request')::bigint requests from rawall "
                      "group by 1 order by requests desc, 1")

    def bk(self):
        """SQL for the chart bucket: label `day` and epoch seconds `t`."""
        if self.bucket == "hour":
            return ("strftime(date_trunc('hour', ts), '%Y-%m-%d %H:00') as day, "
                    "epoch(date_trunc('hour', ts))::bigint as t")
        return "cast(ts as date)::varchar as day, epoch(date_trunc('day', ts))::bigint as t"


def source_coverage(data):
    """[{name, files, earliest, latest}] per source root from the ingest stats files (transcript mtimes)."""
    out = []
    for p in sorted(glob.glob(os.path.join(data, "ingest_stats*.json"))):
        try:
            c = json.load(open(p)).get("coverage")
        except (OSError, ValueError):
            continue
        if c:
            out.append({"name": c["name"], "files": c["files"], "earliest": c["mtime_earliest"], "latest": c["mtime_latest"],
                        "bytes": c.get("bytes"), "event_first": c.get("event_first"), "event_last": c.get("event_last")})
    return out


def coverage_text(earliest, latest, shown_days=None, with_data=None):
    """One sentence stating what the data covers; days before `earliest` are no data, not zero."""
    s = f"{earliest[:10]} to {latest[:10]} UTC; transcripts on this host start {earliest[:10]}"
    if shown_days:
        s += f"; {shown_days}-day view shows {with_data} days with data"
    return s


def no_data_rows(first_day, last_day, days, earliest_day, fields):
    """Rows for window days before the earliest recorded day: marked no_data, values None (not 0)."""
    start = date.fromisoformat(last_day) - timedelta(days=days - 1)
    out, d = [], start
    while d < date.fromisoformat(earliest_day) and d <= date.fromisoformat(last_day):
        out.append({"day": d.isoformat(), "t": int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()),
                    "no_data": True, **{f: None for f in fields}})
        d += timedelta(days=1)
    return out


def p_meta(v):
    r = v.q("""select min(cast(ts as date)) first_day, max(cast(ts as date)) last_day,
        strftime(max(ts), '%Y-%m-%dT%H:%M:%SZ') last_event, count(*) events,
        count(distinct session_id) sessions,
        count(distinct session_id) filter (agent='subagent') subagent_sessions,
        count(*) filter (event_type='llm_request') requests,
        coalesce(sum(input_tokens + cache_read_tokens),0) context_tokens,
        coalesce(sum(input_tokens) filter (cache_read_tokens is not null),0) input_tokens,
        coalesce(sum(cache_read_tokens) filter (input_tokens is not null),0) cache_read_tokens,
        coalesce(sum(output_tokens),0) output_tokens,
        coalesce(sum(content_bytes) filter (event_type='tool_result'),0) tool_bytes,
        coalesce(sum(image_bytes) filter (event_type='tool_result'),0) image_bytes,
        coalesce(sum(image_count) filter (event_type='tool_result'),0) image_count,
        count(*) filter (event_type='tool_call') tool_calls,
        coalesce(sum(reported_cost_alloc_usd),0) cost_usd, coalesce(sum(cost_usd),0) cost_usd_raw, coalesce(sum(list_cost_usd),0) list_cost_usd,
        count(*) filter (event_type='llm_request' and list_cost_usd is null) list_unpriced,
        count(distinct host) hosts from ev""")
    m = r[0]
    for name in ("ingest_stats.json", "watch_status.json"):
        p = os.path.join(v.st.data, name)
        if os.path.exists(p) and not v.day:
            try:
                m[name.split(".")[0]] = json.load(open(p))
            except ValueError:
                pass
    m["days"], m["day"] = v.days, v.day
    # share of context served from cache: cache_read / (input + cache_read), selected range
    m["cache_hit_rate"] = (m["cache_read_tokens"] / m["context_tokens"]) if m["context_tokens"] else None
    if v.day:
        m["range_start"] = m["range_end"] = v.day
    else:
        end = v.q("select max(cast(ts as date))::varchar e from raw")[0]["e"]
        m["range_end"] = end
        m["range_start"] = (date.fromisoformat(end) - timedelta(days=v.days - 1)).isoformat() if end else None
    m["harnesses"], m["harness_filter"] = v.harnesses(), v.harness
    g = v.q("""select strftime(min(ts), '%Y-%m-%dT%H:%M:%SZ') e, strftime(max(ts), '%Y-%m-%dT%H:%M:%SZ') l,
        (select count(distinct cast(ts as date)) from ev) wd from raw""")[0]
    m["coverage"] = {"earliest_event": g["e"], "latest_event": g["l"], "sources": source_coverage(v.st.data)}
    m["days_with_data"] = g["wd"]
    m["prices_as_of"] = prices().asof_text()
    m["now"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not m["events"]:
        return {"rows": [m], "hint": f"Nothing recorded {v.span}."}
    when = f"on {v.day} (UTC)" if v.day else f"between {m['first_day']} and {m['last_day']} (UTC)"
    return {"rows": [m], "hint": f"{m['events']:,} events from {m['sessions']:,} sessions recorded {when}."}


def p_context(v):
    rows = v.q(f"""select {v.bk()},
        coalesce(sum(input_tokens),0)::bigint input, coalesce(sum(cache_read_tokens),0)::bigint cache_read,
        coalesce(sum(cache_write_tokens),0)::bigint cache_write, coalesce(sum(output_tokens),0)::bigint output,
        count(*)::bigint requests
        from ev where event_type='llm_request' group by 1,2 order by 2""")
    if not rows:
        return {"rows": rows, "hint": f"No model requests {v.span}.", "bucket": v.bucket}
    for r in rows:
        r["context"] = r["input"] + r["cache_read"]
    peak = max(rows, key=lambda r: r["context"])
    med = sorted(r["context"] for r in rows)[len(rows) // 2] or 1
    ctx, outp = sum(r["context"] for r in rows), sum(r["output"] for r in rows) or 1
    unit = "hour" if v.bucket == "hour" else "day"
    days_med = statistics.median(r["context"] for r in rows)
    hint = (f"Peak {peak['day']}: {human_tokens(peak['context'])} context tokens, {peak['context'] / med:.1f}x the median {unit}. "
            f"Large tool outputs are what grows context: have tools return a summary line instead of a full dump.")
    if not v.day:
        g = v.q("select strftime(min(ts), '%Y-%m-%d') e, strftime(max(ts), '%Y-%m-%d') l from raw")[0]
        pad = no_data_rows(rows[0]["day"], g["l"], v.days, g["e"], ["input", "cache_read", "cache_write", "output", "requests", "context"])
        rows = pad + rows
        if pad:
            hint += f" {pad[0]['day']} to {pad[-1]['day']} predate the first recorded event ({g['e']}): no data, not zero."
    return {"rows": rows, "hint": hint, "total_context": ctx, "total_output": outp, "median_context": days_med,
            "bucket": v.bucket}


def p_cache(v):
    """Panel 02: context tokens (input + cache read) carried by each model request, median and p95 per bucket."""
    rows = v.q(f"""select {v.bk()}, count(*)::bigint requests,
        median(input_tokens + cache_read_tokens)::bigint p50,
        quantile_cont(input_tokens + cache_read_tokens, 0.95)::bigint p95
        from ev where event_type='llm_request' and input_tokens + cache_read_tokens > 0
        group by 1,2 order by 2""")
    o = v.q("""select quantile_cont(input_tokens + cache_read_tokens, 0.5)::bigint p50,
        quantile_cont(input_tokens + cache_read_tokens, 0.95)::bigint p95, count(*)::bigint requests
        from ev where event_type='llm_request' and input_tokens + cache_read_tokens > 0""")[0]
    if not o["requests"]:
        return {"rows": rows, "hint": f"No model requests recorded {v.span}.", "p50": None, "p95": None, "bucket": v.bucket}
    hint = (f"A typical request carries {human_tokens(o['p50'])} tokens of context and the heaviest 5% carry "
            f"{human_tokens(o['p95'])} or more. Every request re-sends the whole context, so keep large outputs out of "
            f"the main session: delegate them to subagents and compact earlier.")
    return {"rows": rows, "hint": hint, "p50": o["p50"], "p95": o["p95"],
            "requests": o["requests"], "bucket": v.bucket}


def p_tools(v):
    rows = v.q(f"""select coalesce(tool_name,'(unknown)') tool, count(*)::bigint results,
        sum(content_bytes)::bigint bytes, max(content_bytes)::bigint max_bytes,
        coalesce(sum(reread_tokens),0)::bigint reread_tokens,
        coalesce(sum(reread_turns),0)::bigint reread_turns,
        count(*) filter (is_error)::bigint errors,
        coalesce(sum(image_bytes),0)::bigint image_bytes, coalesce(sum(image_count),0)::bigint images
        from ev where event_type='tool_result' group by 1 order by bytes desc limit 12""")
    if not rows:
        return {"rows": rows, "hint": f"No tool results {v.span}."}
    tot = v.q("""select coalesce(sum(content_bytes),0)::bigint b, coalesce(sum(image_bytes),0)::bigint ib,
        coalesce(sum(image_count),0)::bigint n from ev where event_type='tool_result'""")[0]
    allb = tot["b"] or 1
    for r in rows:
        r["share"] = round(r["bytes"] / allb, 4)
        r["image_share"] = round(r["image_bytes"] / (r["bytes"] or 1), 4)
    from .ingest import SHELL_TOOLS
    for r in rows:  # drill-down: shell-ish tools -> family -> target; Read -> file kind -> ext -> top files
        if r["tool"] in SHELL_TOOLS:
            f = shell_families(v, top_fam=8, top_tgt=8, top_calls=0, tools=f"('{r['tool']}')", by="bytes")
            r["families"] = f["families"]
        elif r["tool"] in READ_TOOLS:
            try:
                r["kinds"] = read_kinds(v, f"('{r['tool']}')")
            except Exception:  # data ingested before file_kind existed
                r["kinds"] = []
    t = rows[0]
    tt = max(rows, key=lambda r: r["reread_tokens"])
    hint = (f"{t['tool']} returned {t['share']:.0%} of all tool bytes. Highest re-read cost: {tt['tool']}, "
            f"{human_tokens(tt['reread_tokens'])} tokens re-read (tokens x turns until the next compaction; "
            f"{tt['reread_turns'] / (tt['results'] or 1):,.0f} turns per result on average). "
            f"What to do: {WRAP}.")
    if tot["ib"]:
        hint += (f" Images were {tot['ib'] / allb:.0%} of all tool bytes ({pl(tot['n'], 'image')}, "
                 f"{human_bytes(tot['ib'])}); each image costs about {IMAGE_TOKENS:,} tokens however large its base64.")
    return {"rows": rows, "hint": hint, "total_bytes": allb, "image_bytes": tot["ib"], "images": tot["n"],
            "image_share": round(tot["ib"] / allb, 4)}


def p_largest(v):
    rows = v.q(f"""with c as (select result_sha256, count(*) n from ev
          where event_type='tool_result' and result_sha256 is not null group by 1)
        select coalesce(tool_name,'(unknown)') tool, content_bytes::bigint bytes, left(result_sha256,12) sha12,
          coalesce(image_bytes,0)::bigint image_bytes, coalesce(image_count,0)::bigint images,
          c.n::bigint times_seen, session_id, agent, harness, coalesce(project,'?') project,
          coalesce(reread_turns,0)::bigint reread_turns, coalesce(reread_tokens,0)::bigint reread_tokens,
          coalesce(estimated_tokens,0)::bigint est_tokens,
          coalesce(ss.s_requests,0)::bigint session_requests, coalesce(ss.s_segments,1)::bigint session_segments,
          strftime(ts, '%Y-%m-%d %H:%M') seen_at, {POINTER} pointer
        from ev join c using(result_sha256) left join (select session_id, count(*)::bigint s_requests, (max(segment_index)+1)::bigint s_segments
                  from ev where event_type='llm_request' group by 1) ss using(session_id) where event_type='tool_result'
        order by reread_tokens desc nulls last, content_bytes desc limit 15""")
    big = v.q(f"""select count(*)::bigint n,
        coalesce(mode(tool_name),'(unknown)') tool,
        count(*) filter (tool_name = (select mode(tool_name) from ev where event_type='tool_result' and content_bytes > {BIG}))::bigint tool_n
        from ev where event_type='tool_result' and content_bytes > {BIG}""")[0]
    for r in rows:
        r["session"] = short(r.pop("session_id"))
    if not rows:
        return {"rows": rows, "hint": f"No tool outputs {v.span}."}
    t = rows[0]
    hint = (f"Ranked by re-read tokens, cumulative (estimated tokens x turns re-read until the next compaction). Top: {t['tool']} in "
            f"{t['session']} ({t['project']}), {human_bytes(t['bytes'])} re-read over {t['reread_turns']:,} turns, "
            f"about {human_tokens(t['reread_tokens'])} tokens; that session has {t['session_requests']:,} requests in "
            f"{pl(t['session_segments'], 'segment')}. ")
    if big["n"]:
        hint += (f"{pl(big['n'], 'single output')} exceeded 256 KB ("
                 f"{'all ' + big['tool'] if big['tool_n'] == big['n'] else str(big['tool_n']) + ' of them ' + big['tool']}). ")
    hint += f"What to do: {WRAP}."
    return {"rows": rows, "hint": hint}


def p_repeats(v):
    q = f"""select left(result_sha256,12) sha12, coalesce(mode(tool_name),'(unknown)') tool,
          count(*)::bigint times, max(content_bytes)::bigint bytes_each,
          sum(content_bytes)::bigint total_bytes, (max(content_bytes)*(count(*)-1))::bigint repeat_bytes,
          count(distinct session_id)::bigint sessions, string_agg(distinct harness, ', ' order by harness) harness, arg_min({POINTER}, ts) pointer,
          coalesce(sum(reread_tokens),0)::bigint reread_tokens, coalesce(sum(reread_turns),0)::bigint reread_turns,
          max(ss.s_requests)::bigint session_requests, max(ss.s_segments)::bigint session_segments
        from ev left join (select session_id, count(*)::bigint s_requests, (max(segment_index)+1)::bigint s_segments
                  from ev where event_type='llm_request' group by 1) ss using(session_id)
        where event_type='tool_result' and result_sha256 is not null and content_bytes > 0
        group by result_sha256 having count(*) >= 3"""
    rows = v.q(q + " order by reread_tokens desc, repeat_bytes desc limit 15")
    agg = v.q(f"select count(*)::bigint n, coalesce(sum(repeat_bytes),0)::bigint b from ({q})")[0]
    if not rows:
        return {"rows": rows, "hint": f"No payload came back 3 or more times {v.span}."}
    t = rows[0]
    hint = (f"{pl(agg['n'], 'payload')} came back 3+ times; the repeats alone were {human_bytes(agg['b'])}. "
            f"Worst by re-read tokens: {t['tool']} {t['sha12']} seen {t['times']}x ({human_bytes(t['repeat_bytes'])} of repeats, "
            f"{human_tokens(t['reread_tokens'])} tokens over {t['reread_turns']:,} turns). "
            f"Point agents at a saved copy or a summary instead of re-reading; for command output, {WRAP}.")
    return {"rows": rows, "hint": hint, "payloads": agg["n"], "repeat_bytes": agg["b"]}


def p_sessions(v):
    q = f"""select session_id, coalesce(mode(project),'?') project, bool_or(agent='subagent') subagent,
          any_value(parent_session_id) parent, count(*)::bigint requests,
          (max(segment_index)+1)::bigint segments, count(distinct cast(ts as date))::bigint days_active,
          sum(input_tokens + cache_read_tokens)::bigint context, sum(output_tokens)::bigint output,
          coalesce(mode(model),'?') model, any_value(harness) harness, arg_min({POINTER}, ts) pointer
        from ev where event_type='llm_request' group by session_id"""
    rows = v.q(q + " order by context desc limit 15")
    tot = v.q(f"""select coalesce(sum(context),0)::bigint c, count(*)::bigint n,
        count(*) filter (subagent)::bigint subs, count(*) filter (requests > 2000)::bigint long_n from ({q})""")[0]
    if not rows:
        return {"rows": rows, "hint": f"No sessions {v.span}."}
    for r in rows:
        r["session"], r["parent"] = short(r.pop("session_id")), short(r["parent"]) if r["parent"] else None
        r["per_request"] = r["context"] // max(r["requests"], 1)
    top5 = sum(r["context"] for r in rows[:5]) / (tot["c"] or 1)
    t = rows[0]
    hint = (f"The top 5 of {tot['n']:,} sessions processed {top5:.0%} of all context. "
            f"Heaviest: {t['session']} ({t['project']}) at {human_tokens(t['per_request'])} tokens per request "
            f"over {t['requests']:,} requests. What to do: hand off or restart; {pl(tot['long_n'], 'session')} over 2,000 requests.")
    return {"rows": rows, "hint": hint, "sessions": tot["n"], "subagent_sessions": tot["subs"]}


def p_models(v):
    rows = v.q("""select coalesce(model,'(unknown)') model, count(*)::bigint requests,
        count(*) filter (agent='subagent')::bigint subagent_requests,
        sum(input_tokens + cache_read_tokens)::bigint context, sum(output_tokens)::bigint output
        from ev where event_type='llm_request' group by 1 order by requests desc""")
    split = {}
    for x in v.q("select coalesce(model,'(unknown)') model, harness, count(*)::bigint n from ev "
                 "where event_type='llm_request' group by 1,2 order by n desc"):
        split.setdefault(x["model"], []).append({"harness": x["harness"], "requests": x["n"]})
    n = sum(r["requests"] for r in rows) or 1
    for r in rows:
        r["harness_split"] = split.get(r["model"], [])
        r["share"] = round(r["requests"] / n, 4)
    if not rows:
        return {"rows": rows, "hint": f"No model requests {v.span}."}
    t = rows[0]
    small = next((r["model"] for r in rows[1:] if any(k in r["model"] for k in ("sonnet", "haiku", "mini", "flash", "luna"))), None)
    hint = (f"{t['model']} served {t['share']:.0%} of requests, {t['subagent_requests'] / (t['requests'] or 1):.0%} "
            f"of them from subagents. Subagent work is the first candidate for "
            f"{small or 'a smaller model'}.")
    return {"rows": rows, "hint": hint}


def p_loops(v):
    rows = v.q(f"""with c as (select session_id, tool_name, input_sha256, ts, harness, {POINTER} pointer from ev
          where event_type='tool_call' and input_sha256 is not null),
        w as (select *, count(*) over (partition by session_id, tool_name, input_sha256 order by epoch(ts)
                range between current row and 600 following) n from c),
        g as (select session_id, tool_name, input_sha256, any_value(harness) harness, max(n) attempts, min(ts) first_ts,
                arg_min(pointer, ts) pointer from w
              group by 1,2,3 having max(n) >= 5),
        r as (select session_id, tool_name, input_sha256, count(*) filter (is_error) errs,
                coalesce(sum(estimated_tokens),0) toks, coalesce(sum(content_bytes),0) bytes
              from ev where event_type='tool_result' group by 1,2,3)
        select g.session_id, g.harness, coalesce(g.tool_name,'(unknown)') tool, g.attempts::bigint attempts,
          case when r.errs > 0 then 'tool error' else 'identical call, no error' end kind,
          coalesce(r.toks,0)::bigint est_tokens, coalesce(r.bytes,0)::bigint bytes,
          strftime(g.first_ts, '%Y-%m-%d %H:%M') seen_at, g.pointer
        from g left join r using(session_id, tool_name, input_sha256)
        order by attempts desc, est_tokens desc""")
    total, rows = len(rows), rows[:20]
    for r in rows:
        r["session"] = short(r.pop("session_id"))
    if not rows:
        return {"rows": rows, "hint": f"No retry loops (5+ identical calls within 10 minutes) {v.span}.", "loops": 0}
    t = rows[0]
    hint = (f"{pl(total, 'loop')}; worst: {t['tool']} x{t['attempts']} in {t['session']} ({t['kind']}). "
            f"Add a stop rule: after 3 identical calls in 10 minutes, change approach or ask.")
    return {"rows": rows, "hint": hint, "loops": total}


def p_subagents(v):
    """Panel 09: spawns by subagent_type, model requested vs default, tokens per type, delegation by lane."""
    S = spawns()
    rows = v.q(f"""select subagent_type, count(*)::bigint spawns,
        count(*) filter (model_requested <> 'default')::bigint explicit,
        sum(context_tokens)::bigint context, sum(output_tokens)::bigint output,
        (sum(context_tokens) / count(*))::bigint per_spawn, count(subagent_session)::bigint linked,
        count(*) filter (linked_by = 'lineage')::bigint linked_by_lineage
        from ({S}) group by 1 order by context desc, spawns desc limit 12""")
    req = v.q(f"""select model_requested model, count(*)::bigint spawns from ({S}) group by 1 order by 2 desc""")
    lanes = v.q(f"""with c as (select {LANE} lane, count(*)::bigint calls,
            count(*) filter (tool_name in {SPAWN_IN})::bigint spawns
          from ev where event_type='tool_call' and agent <> 'subagent' group by 1)
        select lane, calls, spawns, round(100.0 * spawns / calls, 2) per_100 from c where calls >= 50
        order by per_100 desc, calls desc""")
    n = sum(r["spawns"] for r in req)  # all spawns; rows keep the top 12 types only
    if not n:
        return {"rows": rows, "hint": f"No subagents spawned {v.span}.", "spawns": 0, "requested": req,
                "lanes_most": [], "lanes_least": []}
    explicit = sum(r["spawns"] for r in req if r["model"] != "default")
    for r in rows:
        r["share"] = round(r["spawns"] / n, 4)
    most = [l for l in lanes if l["spawns"]][:5]
    least = [l for l in sorted(lanes, key=lambda l: (l["per_100"], -l["calls"]))][:3]
    t = rows[0]
    dft = n - explicit
    hint = (f"{pl(n, 'spawn')}; {dft / n:.0%} ran on the default model and {explicit / n:.0%} named one. "
            f"{t['subagent_type']} used the most context ({human_tokens(t['context'])}, {human_tokens(t['per_spawn'])} per spawn). ")
    if dft / n > 0.5:
        hint += "Name a smaller model on routine spawns: most delegation inherits the parent's model today."
    else:
        hint += f"Check that {t['subagent_type']} spawns need their context: brief them with paths, not pasted files."
    if most:
        hint += f" Busiest delegator: {most[0]['lane']} ({most[0]['per_100']} spawns per 100 tool calls)."
    return {"rows": rows, "hint": hint, "spawns": n, "explicit": explicit, "default": dft, "requested": req,
            "lanes_most": most, "lanes_least": least}


def p_skills(v):
    """Panel 10: skills invoked by name and by agent kind (Claude Skill tool; Codex SKILL.md reads)."""
    rows = v.q(f"""select skill_name skill, count(*)::bigint calls, count(distinct session_id)::bigint sessions,
        string_agg(distinct {KIND}, ', ') kinds
        from ev where event_type='tool_call' and skill_name is not null group by 1 order by calls desc, skill limit 15""")
    kinds = v.q(f"""select {KIND} kind, count(*)::bigint calls, count(distinct skill_name)::bigint skills
        from ev where event_type='tool_call' and skill_name is not null group by 1 order by calls desc""")
    tot = v.q("""select count(*)::bigint n, count(distinct skill_name)::bigint k
        from ev where event_type='tool_call' and skill_name is not null""")[0]
    once = v.q("""select count(*)::bigint n from (select skill_name from ev where event_type='tool_call'
        and skill_name is not null group by 1 having count(*) = 1)""")[0]["n"]
    if not tot["n"]:
        return {"rows": rows, "hint": f"No skills invoked {v.span}.", "kinds": kinds, "calls": 0, "skills": 0}
    t = rows[0]
    hint = (f"{pl(tot['n'], 'skill invocation')} across {pl(tot['k'], 'skill')}; {t['skill']} leads with {t['calls']} "
            f"({t['calls'] / tot['n']:.0%}). ")
    if once:
        hint += (f"{once} skill{'s were' if once != 1 else ' was'} used once {v.span}: "
                 f"if {'they stay' if once != 1 else 'it stays'} that rare, fold {'them' if once != 1 else 'it'} into a busier skill so fewer descriptions ride along in every session.")
    else:
        hint += f"Keep {t['skill']} short: its instructions enter context every time it runs."
    return {"rows": rows, "hint": hint, "kinds": kinds, "calls": tot["n"], "skills": tot["k"], "once": once}


READ_TOOLS = ("Read", "ReadFile")
READ_IN = "(" + ",".join(f"'{t}'" for t in READ_TOOLS) + ")"
FILE_EXPR = "coalesce(file_kind, 'unknown')"


def read_kinds(v, tools, top_ext=8, top_files=5):
    """Results of `tools` grouped file_kind -> file_ext -> top files, by bytes (re-read tokens alongside)."""
    rows = v.q(f"""with t as (select {FILE_EXPR} kind, coalesce(file_ext, 'none') ext, coalesce(file_path, '(unknown)') path,
          coalesce(content_bytes,0) b, coalesce(reread_tokens,0) tail, coalesce(image_count,0) img
        from ev where event_type='tool_result' and tool_name in {tools}),
      e as (select kind, ext, count(*)::bigint n, sum(b)::bigint b, sum(tail)::bigint tail from t group by 1,2),
      f as (select kind, ext, path, count(*)::bigint n, sum(b)::bigint b, sum(tail)::bigint tail,
          row_number() over (partition by kind, ext order by sum(tail) desc, sum(b) desc) rn from t group by 1,2,3)
      select e.kind, e.ext, e.n, e.b, e.tail, f.path, f.n fn, f.b fb, f.tail ft from e
      left join f on f.kind = e.kind and f.ext = e.ext and f.rn <= {top_files}
      order by e.b desc, e.kind, e.ext, f.rn""")
    ks, order = {}, []
    for r in rows:
        k = ks.get(r["kind"])
        if k is None:
            k = ks[r["kind"]] = {"kind": r["kind"], "calls": 0, "bytes": 0, "tail": 0, "exts": {}}
            order.append(k)
        e = k["exts"].get(r["ext"])
        if e is None:
            e = k["exts"][r["ext"]] = {"ext": r["ext"], "calls": r["n"], "bytes": r["b"], "tail": r["tail"], "files": []}
            k["calls"] += r["n"]; k["bytes"] += r["b"]; k["tail"] += r["tail"]
        if r["path"] is not None:
            e["files"].append({"path": r["path"], "calls": r["fn"], "bytes": r["fb"], "tail": r["ft"]})
    for k in order:
        es = sorted(k["exts"].values(), key=lambda x: -x["bytes"])
        k["exts"] = es[:top_ext]
        k["more_exts"] = max(0, len(es) - top_ext)
    return sorted(order, key=lambda x: -x["bytes"])


def shell_families(v, top_fam=12, top_tgt=12, top_calls=5, tools=None, by="tokens"):
    """Shell results grouped family -> target -> calls, ranked by estimated tokens returned (re-read tokens alongside).
    Duration is a secondary column (results past PAUSED are left out of it). Drill-down rows carry the pointer."""
    rows = v.q(f"""with t as (select coalesce(r.cmd_family, 'unclassified') fam, coalesce(r.cmd_target, 'unclassified') tgt,
          coalesce(r.estimated_tokens, 0) tok, coalesce(r.reread_tokens, 0) tail, coalesce(r.content_bytes, 0) b,
          case when r.duration_ms <= {PAUSED} then r.duration_ms end dur, r.session_id sid, r.harness, r.exit_code, r.is_error,
          left(c.cmd_text, 160) cmd, c.src_file || ':' || c.src_line pointer
        from ev r left join ev c on c.event_type='tool_call' and c.session_id = r.session_id and c.tool_call_id = r.tool_call_id
        where r.event_type='tool_result' and r.tool_name in {tools or SHELL_IN}),
      g as (select fam, tgt, count(*)::bigint calls, sum(tok)::bigint tok, sum(tail)::bigint tail, sum(b)::bigint b,
          coalesce(sum(dur), 0)::bigint dur from t group by 1,2),
      k as (select *, row_number() over (partition by fam, tgt order by tok desc, b desc) rn from t)
      select g.fam, g.tgt, g.calls, g.tok, g.tail, g.b, g.dur, k.sid, k.harness, k.exit_code, k.is_error, k.cmd, k.pointer,
        k.tok call_tok, k.b call_b, k.dur call_dur, k.rn
      from g left join k on k.fam = g.fam and k.tgt = g.tgt and k.rn <= {top_calls}
      order by g.tok desc, g.fam, g.tgt, k.rn""")
    fams, order = {}, []
    for r in rows:
        f = fams.get(r["fam"])
        if f is None:
            f = fams[r["fam"]] = {"family": r["fam"], "calls": 0, "tokens": 0, "tail": 0, "bytes": 0, "duration_ms": 0,
                                  "targets": {}}
            order.append(f)
        t = f["targets"].get(r["tgt"])
        if t is None:
            t = f["targets"][r["tgt"]] = {"target": r["tgt"], "calls": r["calls"], "tokens": r["tok"], "tail": r["tail"],
                                          "bytes": r["b"], "duration_ms": r["dur"], "top": []}
            f["calls"] += r["calls"]; f["tokens"] += r["tok"]; f["tail"] += r["tail"]
            f["bytes"] += r["b"]; f["duration_ms"] += r["dur"]
        if r["rn"] is not None:
            t["top"].append({"session": short(r["sid"]), "harness": r["harness"], "bytes": r["call_b"], "tokens": r["call_tok"],
                             "duration_ms": r["call_dur"], "exit_code": r["exit_code"], "is_error": r["is_error"],
                             "command": r["cmd"], "pointer": r["pointer"]})
    out = []
    for f in sorted(order, key=lambda x: -x[by])[:top_fam]:
        ts = sorted(f["targets"].values(), key=lambda x: -x[by])
        f["targets"] = ts[:top_tgt]
        f["more_targets"] = max(0, len(ts) - top_tgt)
        out.append(f)
    tot = {"calls": sum(f["calls"] for f in order), "tokens": sum(f["tokens"] for f in order),
           "tail": sum(f["tail"] for f in order)}
    return {"families": out, "shell_total": tot}


def shell_hint(fam):
    """What-to-do line: the top target by estimated tokens returned."""
    best = None
    for f in fam["families"]:
        for t in f["targets"]:
            if best is None or t["tokens"] > best[1]["tokens"]:
                best = (f, t)
    tot = fam["shell_total"]
    if not best or not tot["tokens"]:
        return ""
    f, t = best
    tail = f" (re-read tokens {human_tokens(tot['tail'])})" if tot["tail"] else ""
    return (f"Shell output returned about {human_tokens(tot['tokens'])} tokens over {pl(tot['calls'], 'call')}{tail}. "
            f"Top: `{t['target']}` ({f['family']}) returned {human_tokens(t['tokens'])} tokens in {pl(t['calls'], 'call')}"
            f" ({t['tokens'] / tot['tokens']:.0%}). Wrap it so it prints one summary line (pass/fail + counts) and read the "
            f"full output only on failure. ")


def p_bash(v):
    """Panel 11: shell calls per bucket, p50/p95 duration, slowest 10 commands (masked).
    Results more than 1 h after their call (PAUSED) are counted separately and left out of the timings."""
    J = f"""from ev r join ev c on c.event_type='tool_call' and c.session_id = r.session_id
          and c.tool_call_id = r.tool_call_id
        where r.event_type='tool_result' and r.tool_name in {SHELL_IN} and r.duration_ms is not null
          and r.duration_ms <= {PAUSED}"""
    fam = shell_families(v)
    rows = v.q(f"""select {v.bk().replace('ts', 'r.ts')}, count(*)::bigint calls,
        median(r.duration_ms)::bigint p50, quantile_cont(r.duration_ms, 0.95)::bigint p95,
        count(*) filter (r.is_error)::bigint errors from ev r
        where r.event_type='tool_result' and r.tool_name in {SHELL_IN} and r.duration_ms is not null
          and r.duration_ms <= {PAUSED}
        group by 1,2 order by 2""")
    o = v.q(f"""select count(*)::bigint calls, median(duration_ms)::bigint p50,
        quantile_cont(duration_ms, 0.95)::bigint p95, count(*) filter (duration_ms > 60000)::bigint over_60s,
        coalesce(sum(duration_ms) filter (duration_ms > 60000), 0)::bigint over_60s_ms,
        count(*) filter (is_error)::bigint errors
        from ev where event_type='tool_result' and tool_name in {SHELL_IN} and duration_ms is not null
          and duration_ms <= {PAUSED}""")[0]
    o["paused"] = v.q(f"""select count(*)::bigint n from ev where event_type='tool_result' and tool_name in {SHELL_IN}
        and duration_ms > {PAUSED}""")[0]["n"]
    slow = v.q(f"""select coalesce(c.detail, '(command not recorded)') command, r.duration_ms::bigint duration_ms,
        r.session_id, r.harness, r.tool_name tool, r.exit_code, r.is_error, strftime(r.ts, '%Y-%m-%d %H:%M') seen_at,
        c.src_file || ':' || c.src_line pointer {J} order by r.duration_ms desc limit 10""")
    for r in slow:
        r["session"] = short(r.pop("session_id"))
    if not o["calls"]:
        return {"rows": rows, "hint": f"No shell commands recorded {v.span}.", "slowest": slow, "bucket": v.bucket,
                "calls": 0, "p50": None, "p95": None, **fam}
    hint = shell_hint(fam) + (f"{pl(o['calls'], 'timed shell call')}; median {secs(o['p50'])}, p95 {secs(o['p95'])}. ")
    if slow:
        hint += f"Slowest: `{slow[0]['command'][:48]}` at {secs(slow[0]['duration_ms'])}. "
    if o["over_60s"]:
        hint += (f"{pl(o['over_60s'], 'call')} ran past 60 s ({secs(o['over_60s_ms'])} of waiting): run long builds "
                 f"and test suites in the background with a timeout, and poll their log instead of blocking.")
    else:
        hint += "No call ran past 60 s; shell time is not the bottleneck."
    if o["paused"]:
        hint += f" ({pl(o['paused'], 'result')} arrived over an hour after the call, a paused session; left out of the timings.)"
    return {"rows": rows, "hint": hint, "slowest": slow, "bucket": v.bucket, **o, **fam}


def marker():
    """(date, label) of the before/after marker; OBS_MARKER='YYYY-MM-DD|label' overrides."""
    d, _, l = (os.environ.get("OBS_MARKER") or "").partition("|")
    try:
        date.fromisoformat(d)
        return d, l or "marker"
    except ValueError:
        return MARKER


def before_after(con_q, end_sql, mk):
    """Daily tool-result MB and context tokens over the 30 days ending at end_sql, plus 7-day averages
    before (the 7 days ending the day before the marker) and after (the 7 days starting the day after).
    Averages are over the days that have events. con_q(sql) -> list of dict rows (table `raw`)."""
    rows = con_q(f"""with e as (select max(cast(ts as date)) d from {end_sql})
        select cast(ts as date)::varchar as day, epoch(date_trunc('day', ts))::bigint as t,
          round(coalesce(sum(content_bytes) filter (event_type='tool_result'),0) / 1e6, 2)::double tool_mb,
          round(coalesce(sum(image_bytes) filter (event_type='tool_result'),0) / 1e6, 2)::double image_mb,
          coalesce(sum(input_tokens + cache_read_tokens) filter (event_type='llm_request'),0)::bigint context
        from raw, e where cast(ts as date) > e.d - interval 30 day and cast(ts as date) <= e.d
        group by 1,2 order by 2""")
    m = date.fromisoformat(mk[0])
    def avg(lo, hi):
        xs = [r for r in rows if lo <= (date.fromisoformat(r["day"]) - m).days <= hi]
        if not xs:
            return None
        return {"days": len(xs), "tool_mb": round(sum(r["tool_mb"] for r in xs) / len(xs), 1),
                "context": round(sum(r["context"] for r in xs) / len(xs))}
    return rows, avg(-7, -1), avg(1, 7)


def p_compactions(v):
    """Panel 13: compactions per day by harness, manual / auto / unknown, segment sizes, top sessions."""
    by_day = v.q(f"""select strftime(cast(ts as date), '%Y-%m-%d') "day", harness, count(*)::bigint compactions
        from ev where event_type='compaction' group by 1, 2 order by 1, 2""")
    split = {r["trigger"]: r["n"] for r in v.q("""select coalesce(detail,'unknown') trigger, count(*)::bigint n
        from ev where event_type='compaction' group by 1""")}
    seg = f"""select compaction_id cid, any_value(session_id) session_id, count(*)::bigint requests,
          sum(input_tokens + cache_read_tokens + cache_write_tokens)::bigint context
        from ev where event_type='llm_request' and compaction_id is not null group by 1"""
    stats = v.q(f"""select count(*)::bigint segments, coalesce(median(requests),0)::bigint median_requests,
        coalesce(quantile_cont(requests, 0.95),0)::bigint p95_requests, coalesce(median(context),0)::bigint median_context,
        coalesce(quantile_cont(context, 0.95),0)::bigint p95_context from ({seg})""")[0]
    sizes = v.q(f"""select case when requests < 50 then '1-49' when requests < 200 then '50-199'
          when requests < 1000 then '200-999' else '1000+' end bucket, count(*)::bigint segments, min(requests) lo
        from ({seg}) group by 1 order by lo""")
    top = v.q(f"""with c as (select session_id, count(*)::bigint compactions,
            count(*) filter (detail='manual')::bigint manual, count(*) filter (detail='auto')::bigint auto,
            count(*) filter (coalesce(detail,'unknown')='unknown')::bigint unknown
          from ev where event_type='compaction' group by 1),
        g as ({seg})
        select c.session_id, c.compactions, c.manual, c.auto, c.unknown,
          (select count(*) from ev e where e.event_type='llm_request' and e.session_id=c.session_id)::bigint requests,
          coalesce(avg(g.requests),0)::bigint avg_segment_requests, coalesce(quantile_cont(g.requests,0.95),0)::bigint p95_segment_requests,
          coalesce(avg(g.context),0)::bigint avg_segment_context, coalesce(quantile_cont(g.context,0.95),0)::bigint p95_segment_context
        from c left join g on g.session_id = c.session_id group by all order by c.compactions desc limit 15""")
    for r in top:
        r["session"] = short(r.pop("session_id"))
    total = sum(split.values())
    man, auto, unk = split.get("manual", 0), split.get("auto", 0), split.get("unknown", 0)
    out = {"rows": top, "by_day": by_day, "split": {"manual": man, "auto": auto, "unknown": unk}, "total": total,
           "sizes": [{"bucket": r["bucket"], "segments": r["segments"]} for r in sizes], "segment_stats": stats}
    if not total:
        out["hint"] = f"No compactions {v.span}."
        return out
    t = top[0]
    lead = ("most are automatic: the context filled up before anyone chose a boundary" if auto > man
            else "most are manual: people already compact on purpose")
    out["hint"] = (f"{total:,} compactions {v.span}: {man:,} manual, {auto:,} auto, {unk:,} unknown "
                   f"(Codex records carry no trigger); {lead}. Median segment {stats['median_requests']:,} requests "
                   f"({human_tokens(stats['median_context'])} context tokens processed), p95 {stats['p95_requests']:,}. "
                   f"Most compacted: {t['session']} with {t['compactions']:,}. "
                   f"What to do: hand off or restart at a task boundary instead of compacting a long session again.")
    return out


def p_beforeafter(v):
    """Panel 14: tool-result MB/day and context/day over the trailing 30 days, marker at the executor-wrapper rule."""
    mk = marker()
    rows, b, a = before_after(v.q, "ev", mk)
    if rows:
        e = v.q("select strftime(min(ts), '%Y-%m-%d') e from raw")[0]["e"]
        rows = no_data_rows(None, rows[-1]["day"], 30, e, ["tool_mb", "image_mb", "context"]) + rows
    out = {"rows": rows, "marker": {"date": mk[0], "label": mk[1]}, "before": b, "after": a}
    if not rows:
        out["hint"] = f"No events {v.span}."
    elif not b or not a:
        out["hint"] = (f"The {mk[1]} marker ({mk[0]}) needs recorded days on both sides; "
                       f"the data covers {rows[0]['day']} to {rows[-1]['day']}. Ingest 30 days: obs ingest all --since 30d.")
    else:
        out["hint"] = (f"7-day averages around the {mk[1]} ({mk[0]}): tool results {b['tool_mb']:,} MB/day before, "
                       f"{a['tool_mb']:,} MB/day after ({(a['tool_mb'] - b['tool_mb']) / (b['tool_mb'] or 1):+.0%}); context "
                       f"{human_tokens(b['context'])}/day before, {human_tokens(a['context'])}/day after "
                       f"({(a['context'] - b['context']) / (b['context'] or 1):+.0%}).")
    return out


READ_ADVICE = {
    "jsonl-transcript": "jsonl transcripts read directly: {mb} MB - query them (jq / DuckDB read_json), never cat them",
    "image": "images: {mb} MB - subagent views only, shrink to 1024 px first",
    "log": "logs: {mb} MB - grep or tail for the latest error instead of reading the file",
    "json-yaml": "json/yaml: {mb} MB - extract the keys you need with jq / yq",
    "binary": "binary files: {mb} MB - do not read them; use file / strings / a parser",
    "code": "code: {mb} MB - read the function you need (offset + limit), not the whole file",
    "markdown": "markdown: {mb} MB - read the section you need (offset + limit)",
}
READ_ROWS = f"event_type='tool_result' and file_kind is not null and (tool_name in {READ_IN} or tool_name in {SHELL_IN})"


def p_readtypes(v):
    """Panel 12: bytes per file_kind per bucket (stacked), top 15 file paths by re-read tokens, what-to-do computed."""
    if "file_kind" not in {r["column_name"] for r in v.q("describe ev")}:
        return {"rows": [], "kinds": [], "files": [], "hint": "No file-type data yet; re-ingest with this version (obs ingest all --since 30d)."}
    bk = v.bk()
    days = v.q(f"select {bk}, file_kind kind, sum(content_bytes)::bigint b from ev where {READ_ROWS} group by 1,2,3 order by 2")
    kinds = v.q(f"""select file_kind kind, count(*)::bigint calls, sum(content_bytes)::bigint bytes,
        coalesce(sum(reread_tokens),0)::bigint tail, coalesce(sum(image_count),0)::bigint images
        from ev where {READ_ROWS} group by 1 order by bytes desc""")
    files = v.q(f"""select file_path path, any_value(file_kind) kind, any_value(file_ext) ext, count(*)::bigint calls,
        sum(content_bytes)::bigint bytes, coalesce(sum(reread_tokens),0)::bigint tail,
        count(distinct session_id)::bigint sessions, mode({LANE}) lane
        from ev where {READ_ROWS} and file_path is not null group by 1 order by tail desc, bytes desc limit 15""")
    if not kinds:
        # parquet written before file_kind existed reads back NULL: say so instead of "no reads"
        old = v.q(f"select count(*)::bigint n from ev where event_type='tool_result' and tool_name in {READ_IN} and file_kind is null")[0]["n"]
        if old:
            return {"rows": [], "kinds": [], "files": [], "hint": f"{old:,} file reads {v.span} were ingested before file types were "
                    "recorded; re-ingest those days to fill this panel (obs ingest all --since 30d)."}
        return {"rows": [], "kinds": [], "files": [], "hint": f"No file reads recorded {v.span}."}
    by = {}
    for r in days:
        d = by.setdefault(r["t"], {"day": r["day"], "t": r["t"]})
        d[r["kind"]] = r["b"]
    rows = [by[k] for k in sorted(by)]
    tot = sum(k["bytes"] for k in kinds) or 1
    for k in kinds:
        k["share"] = round(k["bytes"] / tot, 4)
    parts = []
    for k in kinds[:4]:
        a = READ_ADVICE.get(k["kind"])
        if a and (k["share"] >= 0.05 or k["kind"] in ("jsonl-transcript", "image")):
            parts.append(a.format(mb=f"{k['bytes'] / 1e6:,.0f}") + f" ({human_tokens(k['tail'])} tokens re-read)")
    hint = (f"File reads returned {human_bytes(tot)}; {kinds[0]['kind']} is {kinds[0]['share']:.0%}. "
            + ("; ".join(parts) + ". " if parts else "") + "Ranked by re-read tokens.")
    return {"rows": rows, "kinds": kinds, "files": files, "bucket": v.bucket, "total_bytes": tot, "hint": hint}


PANELS = {"meta": p_meta, "context": p_context, "cache": p_cache, "tools": p_tools, "largest": p_largest,
          "repeats": p_repeats, "sessions": p_sessions, "models": p_models, "loops": p_loops,
          "subagents": p_subagents, "skills": p_skills, "bash": p_bash, "beforeafter": p_beforeafter,
          "readtypes": p_readtypes, "compactions": p_compactions}


def all_panels(st, day):
    """Every panel for one UTC day (report.html)."""
    v = View(st, day=day)
    return {k: f(v) for k, f in PANELS.items()}
