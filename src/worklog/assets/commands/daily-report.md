---
description: 파일 변경 내역으로 일일 업무 일지를 작성합니다 (worklog)
---
Write Korean daily work logs with the worklog MCP tools, following the "Daily work log" workflow of the `worklog` skill. Load that skill first if it is not loaded yet.

Arguments: "$ARGUMENTS"
- empty or "auto": write a log for every day in `status.days_needing_report`, oldest first (today is excluded).
- "오늘" or "today": call `close` until `remaining` is 0, then write (or rewrite) today's log.
- a date (YYYY-MM-DD) or "어제": write (or rewrite) that day's log.

Essentials:
1. `status`; if changes are pending, call `close` until `remaining` is 0.
2. For each day: `changes` and `notes_get` → set noise aside (`excluded`) → `diff` the meaningful changes → context from `read_file`, `related` and earlier logs → one item per topic (`topic_upsert` for new topics) → `daily_save`, fixing errors until it succeeds.
3. Never modify the user's files. Never invent work the evidence does not show; lower `confidence` when you infer. In an unattended run, do not ask questions: decide and finish.
4. Finish with a short Korean summary of what was written, or one line saying nothing needed writing.
