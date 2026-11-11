"""Compact multi-day summary (<= 40 lines). Aggregates only."""
import glob, os
import duckdb
from .ingest import parse_since
from .panels import THREADS, before_after, coverage_text, marker, source_coverage
from .prices import prices
from .queries import events_select, parquet_src
from datetime import datetime, timezone

MAX_LINES = 40


def mb(b):
    return f"{b / 1e6:.2f}M" if b >= 1e6 else f"{b / 1e3:.0f}K"


def pairs(items):
    return ["  ".join(items[i:i + 2]) for i in range(0, len(items), 2)]


def build(data, since):
    files = os.path.join(data, "events", "*", "*.parquet")
    if not glob.glob(files):
        raise SystemExit(f"no parquet under {files}")
    con = duckdb.connect()
    con.execute(f"set threads={THREADS}")
    con.execute("set TimeZone='UTC'")  # UTC days, like the parquet partitions and the dashboard
    cut = datetime.now(timezone.utc) - parse_since(since)
    con.execute(f"create view ev as select * exclude (sc, sl, st) from ({events_select(files)}) where ts >= TIMESTAMPTZ '{cut.isoformat()}'")
    Q = lambda s: con.execute(s).fetchall()
    L = []
    days = Q("""select cast(ts as date) d, count(*) filter (event_type='llm_request'),
      coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), coalesce(sum(cache_read_tokens),0),
      coalesce(sum(cache_write_tokens),0), count(*) filter (event_type='tool_call')
      from ev group by 1 order by 1""")
    ctx = sum(r[2] + r[4] for r in days)
    cs = Q("""select count(distinct session_id) filter (cost_usd is not null), count(distinct session_id),
      coalesce(sum(reported_cost_alloc_usd),0), coalesce(sum(list_cost_usd),0), count(*) filter (list_cost_usd is null)
      from ev where event_type='llm_request'""")[0]
    L.append(f"# Observatory summary since {since} (UTC days). Context processed (input+cache_read): {ctx:,}")
    L.append(f"Cost: reported ${cs[2]:,.2f} ({cs[0]}/{cs[1]} sessions have cost-state) | list price ${cs[3]:,.2f} "
             f"({prices().asof_text()}; {cs[4]:,} requests unpriced). Sizes in bytes of tool results.")
    con.execute(f"create view raw as select * from {parquet_src(files)}")
    def cq(sql):
        cur = con.execute(sql)
        cols = [x[0] for x in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    ce = Q("select strftime(min(ts), '%Y-%m-%dT%H:%M:%SZ'), strftime(max(ts), '%Y-%m-%dT%H:%M:%SZ') from raw")[0]
    if ce[0]:
        nwin = parse_since(since).days
        nd = Q(f"select count(distinct cast(ts as date)) from ev where cast(ts as date) > (select max(cast(ts as date)) from ev) - interval {nwin} day")[0][0]
        L.append(f"Coverage: {coverage_text(ce[0], ce[1], nwin, nd)}. Days before {ce[0][:10]} are no data, not zero.")
    mk = marker()
    _, b, a = before_after(cq, "raw", mk)
    if b and a:
        L.append(f"Before/after {mk[0]} ({mk[1]}), 7-day averages: tool results {b['tool_mb']:,} -> {a['tool_mb']:,} MB/day; "
                 f"context {b['context']:,} -> {a['context']:,} tokens/day.")
    else:
        L.append(f"Before/after {mk[0]} ({mk[1]}): not enough recorded days on both sides (ingest 30 days).")
    L.append("| day | requests | input | output | cache_read | cache_write | tool_calls |")
    L.append("|---|---|---|---|---|---|---|")
    for d, n, i, o, cr, cw, tc in days:
        L.append(f"| {d} | {n:,} | {int(i):,} | {int(o):,} | {int(cr):,} | {int(cw):,} | {tc:,} |")
    tools = Q("""select coalesce(tool_name,'(unknown)'), sum(content_bytes) b, coalesce(sum(image_bytes),0) ib
      from ev where event_type='tool_result' group by 1 order by b desc limit 10""")
    tb, ib, ic = Q("""select coalesce(sum(content_bytes),0), coalesce(sum(image_bytes),0), coalesce(sum(image_count),0)
      from ev where event_type='tool_result'""")[0]
    L.append(f"Tool results {mb(tb)} bytes, images {ib / (tb or 1):.0%} ({int(ic):,} images, ~1,500 tokens each).")
    L.append("Top 10 tools by result bytes: " + ", ".join(f"{t} {mb(b)}" + (f" ({ib / b:.0%} img)" if ib else "") for t, b, ib in tools))
    big = Q("""select coalesce(tool_name,'(unknown)'), content_bytes, coalesce(reread_turns,0), coalesce(reread_tokens,0),
      coalesce(project,'?'), left(replace(session_id,'agent-',''),8)
      from ev where event_type='tool_result' order by 4 desc, content_bytes desc limit 10""")
    L.append("Top 10 outputs by re-read tokens (tool tokens x turns until the next compaction, lane session8):")
    L += pairs([f"{t} {b} x{k}={int(tc):,} {p} {ss}" for t, b, k, tc, p, ss in big])
    rep = Q("""select left(result_sha256,12), coalesce(mode(tool_name),'(unknown)'), count(*) n, max(content_bytes) b,
      coalesce(sum(reread_tokens),0) tc
      from ev where event_type='tool_result' and result_sha256 is not null and content_bytes>0
      group by result_sha256 having count(*)>=2 order by tc desc, n*b desc limit 10""")
    L.append("Top 10 repeated payloads by re-read tokens (sha12 tool count*bytes re-read tokens):")
    L += pairs([f"{s} {t} {n}*{b} {int(tc):,}" for s, t, n, b, tc in rep])
    ses = Q("""select left(replace(session_id,'agent-',''),8), coalesce(mode(project),'?'), sum(input_tokens+cache_read_tokens) c, any_value(agent)
      from ev where event_type='llm_request' group by session_id order by c desc limit 10""")
    L.append("Top 10 sessions by context processed (session8 project tokens):")
    L += pairs([f"{s}{'(sub)' if a == 'subagent' else ''} {p} {int(c):,}" for s, p, c, a in ses])
    return L[:MAX_LINES]


def run(data, since, out=None):
    txt = "\n".join(build(data, since)) + "\n"
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        open(out, "w").write(txt)
    print(txt, end="")
    return 0
