"""SQL fragments shared by the daily report, the ledgers and the dashboard (DuckDB, view `ev`)."""
from .ingest import SHELL_TOOLS, SPAWN_TOOLS

SHELL_IN = "(" + ",".join(f"'{t}'" for t in SHELL_TOOLS) + ")"
SPAWN_IN = "(" + ",".join(f"'{t}'" for t in SPAWN_TOOLS) + ")"

def parquet_src(files):
    """One read_parquet over an explicit file list (a list, or a glob string expanded here): a single scan per reference."""
    import glob
    fs = sorted(glob.glob(files)) if isinstance(files, str) else list(files)
    return "read_parquet([" + ", ".join("'" + f.replace("'", "''") + "'" for f in fs) + "], union_by_name=true)"


def events_select(files):
    """SELECT over the parquet events adding reported_cost_alloc_usd (llm_request rows only).

    The harness total (cost_usd, stored on a session's last llm_request) is spread over that session's
    llm_requests in proportion to list_cost_usd; a session with no list price at all falls back to
    input+cache_read+output tokens. Computed over the whole session before any date filter."""
    return f"""select *, case when event_type='llm_request' and sc is not null then
        sc * case when sl > 0 then coalesce(list_cost_usd,0)/sl
                  when st > 0 then (coalesce(input_tokens,0)+coalesce(cache_read_tokens,0)+coalesce(output_tokens,0))/st
                  else 0 end end as reported_cost_alloc_usd
      from (select *, sum(cost_usd) filter (event_type='llm_request') over w sc,
              sum(list_cost_usd) filter (event_type='llm_request') over w sl,
              sum(coalesce(input_tokens,0)+coalesce(cache_read_tokens,0)+coalesce(output_tokens,0)) filter (event_type='llm_request') over w st
            from {parquet_src(files)} window w as (partition by session_id))"""


# agent kind: claude | claude-subagent | claude-sidechain | codex | codex-subagent
def kind(a=""):
    p = a + "." if a else ""
    return (f"(coalesce(nullif(split_part({p}src_file, '/', 1), ''), case when {p}agent like 'codex%' then 'codex' else 'claude' end)"
            f" || case {p}agent when 'subagent' then '-subagent' when 'claude-code-sidechain' then '-sidechain' else '' end)")


# lane: the workspace an agent ran in. Claude: <lane> when the project folder is -home-<user>--world-team-<lane>-workspace,
# else the folder name with the home prefix and a trailing "-workspace" removed; Codex: "codex:" + project (lane name or cwd basename).
def lane(a=""):
    p = a + "." if a else ""
    slug = f"split_part({p}src_file, '/', 2)"
    return (f"(case when {p}src_file like 'claude%' then coalesce(nullif(regexp_extract({slug}, '^-home-[^-]+--world-team-(.+)-workspace$', 1), ''),"
            f" regexp_replace(regexp_replace({slug}, '^-home-[^-]+-+', ''), '-workspace$', ''))"
            f" else (case when {p}src_file like 'kimi/%' then 'kimi:' else 'codex:' end) || coalesce({p}project, '?') end)")


KIND, LANE = kind(), lane()

POINTER = "(src_file || ':' || src_line)"


def spawns(where="true"):
    """One row per spawn call (Agent/Task/spawn_agent tool_call) matching `where`, joined to the subagent it started.
    The subagent is found by spawn_key (subagent meta toolUseId, or Codex parent:task_name), else by the
    agentId reported in the spawn result. Child totals use every recorded event of that subagent session.
    Third fallback: the `lineage` view (spawn hook log, see lineage_view): same parent session and subagent_type,
    nearest hook time within 120 s of the spawn call, one lineage row per spawn. `linked_by` says which matched.
    When several spawns resolve to the same subagent session (a reused Codex task name, a resumed agent),
    its totals are counted on the earliest of those spawns only, so sums never double count."""
    return f"""with sp as (select * from ev where event_type='tool_call' and tool_name in {SPAWN_IN} and {where}),
      res as (select session_id, tool_call_id, any_value(child_session_id) child, any_value(model) model,
                max(duration_ms) dur from ev where event_type='tool_result' and tool_name in {SPAWN_IN} group by 1,2),
      bykey as (select spawn_key, any_value(session_id) sid from ev
                where agent='subagent' and spawn_key is not null group by 1),
      lin as (select * from (select sp.session_id, sp.tool_call_id, l.child_session_id child,
                row_number() over (partition by sp.session_id, sp.tool_call_id order by abs(epoch(l.ts) - epoch(sp.ts))) r1,
                row_number() over (partition by l.child_session_id order by abs(epoch(l.ts) - epoch(sp.ts))) r2
              from sp join lineage l on l.session_id = sp.session_id
                and coalesce(l.subagent_type, 'general-purpose') = coalesce(sp.detail, 'general-purpose')
                and abs(epoch(l.ts) - epoch(sp.ts)) <= 120) where r1 = 1 and r2 = 1),
      kids as (select session_id sid,
                mode(model) filter (event_type='llm_request') model,
                coalesce(sum(input_tokens + cache_read_tokens) filter (event_type='llm_request'),0)::bigint context,
                coalesce(sum(output_tokens) filter (event_type='llm_request'),0)::bigint output,
                count(*) filter (event_type='tool_call')::bigint calls,
                (epoch_ms(max(ts)) - epoch_ms(min(ts)))::bigint span
              from ev where agent='subagent' group by 1),
      j as (select sp.ts, sp.session_id parent_session, coalesce(bykey.sid, res.child, lin.child) subagent_session,
        coalesce(sp.detail, 'general-purpose') subagent_type, coalesce(sp.model_requested, 'default') model_requested,
        coalesce(kids.model, res.model, '') model_used, kids.context, kids.output, kids.calls,
        greatest(coalesce(res.dur, 0), coalesce(kids.span, 0))::bigint duration_ms,
        {lane('sp')} lane, sp.src_file || ':' || sp.src_line pointer,
        case when bykey.sid is not null then 'spawn_key' when res.child is not null then 'result'
             when lin.child is not null then 'lineage' end linked_by,
        row_number() over (partition by coalesce(bykey.sid, res.child, lin.child) order by sp.ts, sp.tool_call_id) rn
      from sp left join res on res.session_id = sp.session_id and res.tool_call_id = sp.tool_call_id
        left join bykey on bykey.spawn_key = sp.spawn_key
        left join lin on lin.session_id = sp.session_id and lin.tool_call_id = sp.tool_call_id
        left join kids on kids.sid = coalesce(bykey.sid, res.child, lin.child))
      select ts, parent_session, subagent_session, subagent_type, model_requested, model_used,
        (case when rn = 1 or subagent_session is null then coalesce(context, 0) else 0 end)::bigint context_tokens,
        (case when rn = 1 or subagent_session is null then coalesce(output, 0) else 0 end)::bigint output_tokens,
        (case when rn = 1 or subagent_session is null then coalesce(calls, 0) else 0 end)::bigint tool_calls,
        duration_ms, lane, pointer, linked_by from j"""


def lineage_view(data):
    """SQL creating view `lineage` from <data>/lineage/lineage.parquet, or an empty view with the same columns."""
    import os
    f = os.path.join(os.path.abspath(data), "lineage", "lineage.parquet")
    if os.path.isfile(f):
        return f"create or replace view lineage as select * from read_parquet('{f}')"
    return ("create or replace view lineage as select null::timestamptz ts, null::varchar session_id, null::varchar project,"
            " null::varchar tool_name, null::varchar subagent_type, null::varchar model, null::varchar agent_id,"
            " null::varchar child_session_id, null::bigint src_line where false")
