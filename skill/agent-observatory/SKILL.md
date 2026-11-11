---
name: agent-observatory
description: Private black-box recorder analysis for AI agents; use when the user asks what burned their AI agent quota or tokens yesterday, which tool or model wasted the most, why usage spiked between two days, or wants fixes saved as a recommendations note.
---

# Goal

Answer "what burned my quota and how do I fix it" from daily telemetry reports that an outside collector already wrote into a NAS folder the user chooses. The Skill finds the report for the requested day, extracts the aggregates, ranks waste sources (largest tool outputs, repeated payloads, error and retry loops, heavy sessions and models), explains a spike by reconciling two days, and saves a recommendations note back into the same folder after approval. Privacy: only aggregates are read or written, never raw prompts. The report layout is in [report format](references/report-format.md); read it before extracting.

# Required input

- The report folder the user chose (ask once if not given; do not guess a location).
- The day to analyse, such as yesterday or a date. For a spike explanation, the comparison day; default to the previous day with a report.
- Optionally, whether to save a recommendations note.

# Generation-time capability evidence

- `ug-filesystem` → `file-agent`
- `ug-document` → `document-agent`
- `ug-knowledge` → `knowledge-agent`

# Workflow

1. Locate reports. Run: `delegate --target file-agent --instruction 'Use ug-filesystem find with pattern *.md, type file and max-depth 2 in the report folder the user named, then keep only names made of four digits, two digits, two digits and .md, and leave out latest.md, README.md and summary files. If the requested day is not in that list, do not run stat; return the full date list instead. Otherwise use ug-filesystem stat on the file for the requested day and, when a spike explanation is requested, on the file for the comparison day or the nearest earlier day that exists. Return the full date list and the paths for the requested and comparison days; if the requested day is absent say so and return no paths for it.'`
2. Extract aggregates from each path returned by step 1, one run per day (two runs for a spike). Run: `delegate --target document-agent --instruction 'Use ug-document extract with schema auto, content-scope full and query return the json block named aggregates verbatim on one report file returned by the previous step. Return totals, per_model, per_tool, per_session, largest_tool_outputs, repeated_payloads and retry_loops as a table, plus read_by_file_type and compactions when present, quote values verbatim, never repair or complete cut-off JSON, and name any field that is missing or malformed.'`
3. Rank waste. Using the real result of step 2 only, group by payload_hash first: rank repeated_payloads by reread_tokens, then the single largest_tool_outputs, then retry_loops, and add image views as their own class from read_by_file_type, image_count and image_bytes (image views: count and estimated image tokens at about 1,500 per image). For each source name the tool, occurrences, bytes_each and the per_tool output_tokens. Say that re-read tokens count re-reads until the next compaction (mostly cache reads), not fresh input. If more detail is needed, run: `delegate --target document-agent --instruction 'Use ug-document qa with query name the top five waste sources with their token counts, intent comparison, answer-mode table and top-k 10 on the same report file.'`
4. Explain a spike when asked. Run: `delegate --target document-agent --instruction 'Use ug-document reconcile with max-files 2 and query compare totals, per_model, per_tool and per_session; name the tool, session and model behind the input_tokens difference, with numbers from each file, on the two report files returned by step 1 for the requested day and the comparison day.'`
5. Optional evidence across many days. Run: `delegate --target knowledge-agent --instruction 'Use ug-knowledge process-materials with query days that mention repeated payloads or retry loops for the tool identified earlier and extension md on the report folder the user named.'`
6. Save the note after the user agrees. Run: `delegate --target knowledge-agent --instruction 'Use ug-knowledge save-text in the report folder the user named with filename-hint recommendations for the requested day, content being the ranked sources from step 3 with their numbers and the concrete fixes (for the tool output with the highest re-read tokens: wrap the command so it prints one summary line (pass/fail + counts); read the full output only on failure, then grep for the latest error; cache repeated payloads; stop retry loops after three attempts; for image views: a subagent views the image and reports in one line, shrink to 1024 px, at most 5 views per session).'`

Every step after the first uses only the real result of the step before it. Never invent numbers; quote the values the experts returned. Fixes must follow from the ranked data, for example a command that dumps the same large output repeatedly: wrap the command so it prints one summary line (pass/fail + counts); read the full output only on failure, then grep for the latest error. Never recommend capping or truncating output after it is read.

# Completion and failure

Handle each delegation status explicitly:

- `completed`: continue to the next step and use the real result.
- `failed`: stop dependent steps and tell the user which step failed and its error text. If the folder is unreadable because of permissions, say so and ask the user to grant access; do not try another folder.
- `cancelled`: stop and confirm nothing was changed.
- `waiting_user`: ask the user for the missing folder, date or choice, then resume.
- `waiting_approval`: wait for the user to approve the write in step 6; never report the note as saved until the result confirms it.

Report no results honestly: if step 1 finds no report for the day, say so, list the nearest days that exist, and stop. A stat or find error for an absent file is a no-report answer, not a failed step. An empty day (totals of zero) is a valid report: say there was no usage rather than an error. If the whole JSON block is missing or corrupt, there is no usable report for that day: list the nearest days and stop. If only some fields are broken or missing, answer partial and say which fields parsed and which did not. Do not read prompt text; if a report appears to contain raw prompts, tell the user and do not repeat them.
