# Daily report format

An outside collector writes one report per day into the folder the user chose, for example `reports/2026-11-07.md`, optionally with a `.json` sibling holding the same JSON block. File name is the ISO date. Reports hold aggregates only; raw prompts and completions are never stored.

## Layout

1. A title line: `# Agent Observatory daily report 2026-11-07`.
2. A short human summary in Markdown (a few lines).
3. Markdown tables for the ranked items; each has a `file:line` column (see Transcript pointers).
4. One fenced `json` block named "aggregates" containing the fields below.

Beside each report, a folder named after the date (`reports/2026-11-07/`) holds the per-call ledgers and `report.html` (see below).

## JSON fields

- `schema_version` (string): currently `1`.
- `date` (string): ISO date, matches the file name.
- `totals` (object): `input_tokens`, `output_tokens`, `cache_read_tokens`, `sessions`, `tool_calls`, `errors`, `estimated_cost_usd`, `tool_result_bytes`, `image_bytes`, `image_count` (all numbers).
- `per_model` (list): `{model, input_tokens, output_tokens, calls}`.
- `per_tool` (list): `{tool, calls, output_bytes, output_tokens, errors, image_bytes, image_count}`.
- `by_harness` (list, most requests first): `{harness, requests, sessions, tool_calls, tool_result_bytes, input_tokens, output_tokens, list_cost_usd}`. `harness` is the tool that ran the session: `claude-code`, `codex`, `kimi-code`, or `claude-code:<profile>` for another Claude config home (e.g. `claude-code:deepseek`). It is not the model vendor and not main/subagent.
- `per_session` (list, heaviest first): `{session_id, model, input_tokens, output_tokens, tool_calls, duration_min, pointer, excerpt}`.
- `largest_tool_outputs` (list, top 10, highest re-read tokens first): `{session_id, lane, tool, bytes, image_count, est_tokens, reread_turns, reread_tokens, session_requests, segments, payload_hash, pointer, excerpt}`.
- `repeated_payloads` (list, highest re-read tokens first): `{payload_hash, tool, occurrences, reread_tokens, bytes_each, wasted_bytes, pointer, excerpt}`.
- `compactions` (object): `{total, manual, auto, unknown, segments, median_segment_requests, p95_segment_requests, top_sessions: [{session_id, compactions, manual, auto, unknown}]}`; `unknown` means the transcript does not say whether the user or the harness triggered it.
- `retry_loops` (list): `{session_id, tool, attempts, error_class, tokens_spent, input_hash, pointer, excerpt}`.
- `read_by_file_type` (object): file reads of the day (Claude Read, Codex shell reads of a file, Codex image views). `total_bytes` (number); `by_kind` (list, biggest first): `{kind, reads, bytes, reread_tokens, image_count, share}` where `kind` is one of `code`, `markdown`, `json-yaml`, `log`, `jsonl-transcript`, `image`, `binary`, `other`; `top_files` (list, top 15 by re-read tokens): `{path, kind, ext, reads, bytes, reread_tokens, sessions, lane}` (`ext` is the lowercase extension, `none`, or `env`, `dockerfile`, `makefile` for those names; `path` has secret-looking parts masked); `what_to_do` (string, computed from the biggest kinds).

## Re-read tokens and what to do

- `est_tokens` = text bytes / 3.6 + 1,500 per image (an approximation, not a tokenizer count).
- `reread_turns` = model requests after the tool result in the same session until the next compaction (a subagent counts its own session only). Each of them re-reads the result, so `reread_tokens` = `est_tokens` x `reread_turns` (re-read tokens, cumulative: tokens x turns re-read until the next compaction). `session_requests` and `segments` (compactions + 1) describe the whole session. Compaction is read from the transcript marker (Claude Code `type: system` with `subtype: compact_boundary`; Codex top-level `type: compacted`), else a request whose context drops below 60% of the previous one (previous over 30k tokens). Rank by re-read tokens; bytes stay as a column.
- What to do for a large or repeated command output: wrap the command so it prints one summary line (pass/fail + counts); read the full output only on failure, then grep for the latest error. Do not recommend capping or truncating output after it is read: the read already happened and cutting it later breaks the prompt cache.

## Transcript pointers

Every ranked item carries `pointer: {file, line}`; the Markdown tables show the same value as `file:line`.

- `line` is the 1-based line number in that JSONL transcript.
- `file` is relative to the transcripts root and starts with its source: `claude/<project>/<session>.jsonl` (subagents: `claude/<project>/<session>/subagents/agent-<id>.jsonl`) or `codex/<yyyy>/<mm>/<dd>/rollout-<...>.jsonl`. The collector records the absolute roots in its data folder (`sources.json`).
- What the line holds: largest outputs and repeated payloads point at the tool result (repeats: the first occurrence that day); retry loops point at the first identical tool call; heaviest sessions point at the session's first model request that day.
- To read the span, open only that one line with a line-offset reader; do not load the whole transcript. `obs verify-pointers --date D` re-opens a random sample and checks that each line exists and hashes to the reported `payload_hash` or `input_hash`.
- A pointer of `null` means the event predates pointers; treat the item as valid but unlocated.

## Read by file type

The report has a "Read by file type" section with the same data as `read_by_file_type`: a table per file kind (reads, bytes, re-read tokens, images) and the top 15 files by re-read tokens. Use it to say which kind of file fills the context. Typical advice, already computed into `what_to_do`: transcripts (`jsonl-transcript`) should be queried with a JSON tool, not read whole; images should be viewed only by a subagent after shrinking to 1024 px; logs should be searched for the latest error; code and markdown should be read by section with an offset and a limit. An older report without this field simply has no file-type data: say so, do not guess.

## Excerpts

Every ranked item (largest outputs, repeated payloads, retry loops, heaviest sessions) carries `excerpt`: a path relative to the report folder, `YYYY-MM-DD/excerpts/<item>.txt` (`largest-01`, `repeat-01`, `loop-01`, `session-01`, ...), or `null` when the source line could not be read. The file holds the first 4 KB of the span the pointer names (tool result text, tool call input, or message text), with images shown as `[image N bytes]`. The collector scans each excerpt for personal data and secrets and replaces findings with `[redacted:<kind>]` before writing it. Read the excerpt instead of the transcript.

## Per-call ledgers

CSV with a header row, one row per event, no payload text. `file:line` uses the same pointer layout as above; `ts` is UTC.

- `tool-calls.csv`: `ts, session, agent_kind, tool, input_sha12, result_bytes, est_tokens, duration_ms, is_error, file:line`. `duration_ms` = tool result time minus tool call time, empty when either is missing. `agent_kind` is `claude`, `claude-subagent`, `claude-sidechain`, `codex` or `codex-subagent`.
- `bash-commands.csv`: `ts, session, command, duration_ms, result_bytes, exit, file:line`. `command` is the first 80 characters with whitespace collapsed and secret-looking values (tokens, passwords, keys, URL credentials, long random strings) replaced by `***`. `exit` is `ok`, `exit N`, `error` or `no result`. Codex shell calls are included.
- `subagents.csv`: `ts, parent_session, subagent_session, subagent_type, model_requested, model_used, context_tokens, output_tokens, tool_calls, duration_ms, file:line, linked_by`. `linked_by` says how the spawn was matched to its subagent: `spawn_key` (subagent meta / Codex task name), `result` (agent id in the spawn result), `lineage` (spawn hook log: same parent session and type, nearest within 120 s) or empty. One row per spawn call; `model_requested` is the model named in the spawn call or `default`; the remaining columns come from the subagent transcript it started (empty session when it was not recorded).
- `skills.csv`: `ts, session, skill, agent_kind, file:line`. Claude: the Skill tool; Codex: a shell call that reads a `skills/<name>/SKILL.md` file.
- `model-requests.csv`: `ts, session, model, provider, input, cache_read, cache_write, output, latency_ms, file:line`. `latency_ms` is empty when the transcript does not state it.

## report.html

`report.html` is self-contained: styles, chart library and the day's data are inside the file, so it opens from any file browser with no server and no network. It shows the dashboard panels for that one day, with charts by hour.

## Data coverage

The data may start later than the range a view is named for: a 30-day view of a host whose transcripts begin 2026-10-17 has no data before that day, which is different from zero use. State coverage whenever you quote a range.

- `report.md` data quality section has a `data coverage` line: first and last event day (UTC), where transcripts on this host start, and per source the file count and transcript modification range.
- `summary-30d.md` has a `Coverage:` line with the same facts plus how many days of the window have data.
- The dashboard `/api/meta` row has `coverage`: `earliest_event` and `latest_event` (UTC timestamps) and `sources`, a list of `{name, files, earliest, latest}` where `earliest` and `latest` are the oldest and newest transcript modification times of that source root. It also has `days_with_data` for the selected range.
- Days before `earliest_event` are marked `no_data` and have empty values in the chart panels. Never read them as zero and never compute an average or a trend across them.

## Rules for reading

- A missing file, or a missing or corrupt JSON block (truncated, closing fence gone, nothing parses), means no usable report for that day: say so, list the nearest days and stop.
- A block that parses but has some broken fields (wrong types, `date` not matching the file name, a missing field) is partial: use only fields that parse and name the broken ones.
- An empty day has `totals` of zero and empty lists; report no usage.
- Hashes identify repeated content without revealing it. Never ask for the original content; a pointer is for the user to open, not for you to request.
