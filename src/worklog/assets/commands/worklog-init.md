---
description: 이 작업 폴더의 worklog 추적을 시작하고 문서 읽기를 점검합니다 (worklog)
---
Set up worklog for this work folder with the worklog MCP tools, following the "Setup, busy answers and background work" section of the `worklog` skill. Load that skill first if it is not loaded yet.

Arguments: "$ARGUMENTS" (optional: "점검 생략" or "no doctor" skips step 2)

1. `status`. If `initialized` is false, call `init`. While the inventory is still running, call `status` again until `initialized` is true (stop after 10 tries and tell the user it continues in the background).
2. `doctor`. While it answers `running`, call it again (stop after 10 tries). Summarize its `verdicts` per file type: readable directly, only through Office (DRM), or not readable.
3. Only if `folder_map_get` has no content: `overview(depth=2)`, read at most 3 representative files (`read_file` with `max_chars` 2000), save a folder map with `folder_map_save` (one short line per folder), and create topics for clearly ongoing work with `topic_upsert`.
4. Reply in Korean:
   - how many files are tracked, and that changes are recorded from now on: the first daily log can be written once files change (usually the next day)
   - the doctor results
   - the baseline: `baseline_remaining` files have no baseline yet, so their first change will show the whole content. Ask whether to start it now with `baseline` (it runs in the background and keeps hidden Word/Excel busy for a while); start it only if the user says yes. If its answer has `survives_session` false or null, add its message's point: it may stop when this session ends, nothing is lost, and it continues later.
   - if `scheduled_tasks` is empty: the hourly scan and the morning close-out are not installed; give the install command from the status hint.

Never modify, move or delete the user's files.
