---
name: worklog
description: Writes Korean work reports from file changes in the user's work folder - daily work logs (일일 업무 일지) and weekly reports for a senior manager (주간 업무 보고). Use for /daily-report and /weekly-report, when the user asks what they worked on, asks for 일일보고/주간보고/업무일지, or mentions work (meetings, calls) that should be recorded.
---

# worklog: 업무 일지 · 주간 업무 보고

## How it works

- The `worklog` MCP server tracks the work folder. An hourly scan notes which files changed (no content is read). A close-out (`close`) extracts each changed document's text (through Word/Excel/PowerPoint for DRM-protected files), diffs it against the previous version, and stores dated change records.
- Changes may come from the user or from an agent. Treat both as the user's work.
- Facts come from the tools; you interpret and write. Never invent work that the changes, notes or earlier reports do not support.
- Never modify, move or delete the user's files in this workflow. Reports are written only through `daily_save` / `weekly_save`.

Tools (opencode lists them as `worklog_<name>`):

| Tool | Use |
|---|---|
| `status` | Start here: pending close-out, days needing a log, next weekly period, active topics, hints |
| `close` | Extract text and record diffs; repeat while `remaining` > 0 |
| `changes(day)` | The change list of one day |
| `diff(change_id)` | What changed in one file |
| `read_file(path)` | Current text of a document (works for DRM files) |
| `related(path)` | Same-folder files, similar names, files that change together, earlier mentions |
| `search(query)` | Search file names and document text |
| `overview`, `folder_map_get`, `folder_map_save` | Folder orientation and your saved notes about folders |
| `notes_get(day)`, `note_add` | User-provided context for a day (meetings, calls, decisions) |
| `topics_list`, `topic_upsert` | Ongoing work topics with stable IDs |
| `daily_save`, `weekly_save` | Validate and save reports; fix the returned errors and retry |
| `reports_list`, `report_get`, `render` | Read saved reports |

## Daily work log (일일 업무 일지)

1. `status`. If `pending_close.changed` > 0, call `close` until `remaining` is 0 (each call is time-limited).
2. Choose days. A date from the user/arguments wins (for today: close first; today's log can be rewritten later). Otherwise every day in `days_needing_report`, oldest first. If none, say so and stop.
3. Load context once: `topics_list`, `folder_map_get`, and the last ~5 daily logs (`reports_list`, then `report_get`).
4. For each day:
   1. `changes(day)` and `notes_get(day)`.
   2. Triage. Usually noise, put in `excluded` without reading diffs: `resaved` (bytes changed, text did not), pure `renamed`, temp or backup copies, downloads/installers, folder clean-ups. A `touched_only` entry belongs to the day where its net change is recorded.
   3. For meaningful changes call `diff`. For large diffs read the first page and page further only if needed. For `no_baseline` or `added`, the diff is the whole current content: summarize what the document is and its state. Do not claim all of it was written that day.
   4. Need purpose or background? Use `read_file` (short `max_chars`), `related`, `search`, earlier logs.
   5. If text extraction failed (`error`): try `read_file`. If that fails and you have another tool that can read DRM-protected documents, use it. Otherwise describe the change from the file name and folder only, with `confidence: "low"`.
   6. Group changes into work items, **one item per topic**. Reuse an existing topic for the same stream of work (compare names, `paths`, recent logs). Otherwise create one with `topic_upsert` (see Topics).
   7. `daily_save`. Fix every error and retry. Warnings are informational.
5. If you learned what a folder is for, update the folder map (`folder_map_save`, one short line per folder, keep existing lines).
6. Reply briefly in Korean: which days were written and their main items.

Unattended runs (a scheduled prompt, `opencode run`): nobody is there to answer. Never ask questions or wait for confirmation; decide, lower `confidence` where unsure, and finish. A run that ends without calling `daily_save` writes nothing.

Daily item fields:
- `topic_id`
- `summary`: one line, 개조식/명사형, e.g. "A사 제안서 3장 신규 작성 및 예산 단가 수정"
- `details`: concrete facts (section/sheet/slide, numbers before → after, counts, dates)
- `files`: relative paths
- `context`: link to earlier work the evidence shows; mark inferences with "~로 보임". Leave it out rather than guess a reason or cause
- `status`: started | in_progress | reviewing | done | on_hold
- `confidence`: `high` clear from content · `medium` partly inferred · `low` file names only

```json
{"date": "2026-11-04",
 "items": [{"topic_id": "T003", "summary": "A사 제안서 3장 '추진 일정' 신규 작성 및 인건비 단가 수정",
            "details": ["3장 추진 일정 약 2쪽 신규 작성", "인건비 단가 1,200만 원 → 1,350만 원"],
            "files": ["프로젝트/A사/제안서_v3.docx", "프로젝트/A사/예산.xlsx"],
            "context": "10/28 착수한 제안서 작업의 연속. 10/30 회의록 요구사항이 반영된 것으로 보임",
            "status": "in_progress", "confidence": "high"}],
 "excluded": [{"summary": "다운로드 폴더 정리(12개 삭제)", "reason": "보고 대상 아님"}]}
```

## Weekly report (주간 업무 보고)

The weekly report goes to a senior manager: one document, one titled paragraph per topic.

1. `status`: the period is `weekly.next_period_from` to `weekly.suggested_period_to` (normally 1-2 weeks). A period from the user/arguments wins. If days in the period still need daily logs, write them first.
2. Load every daily log in the period (`reports_list(kind="daily", date_from, date_to)`, then `report_get` each) and the period's notes where relevant.
3. Group items by `topic_id` across days. Keep real work outcomes a senior manager cares about; drop noise and trivial housekeeping; merge closely related small topics into one entry (`topic_ids: [...]`).
4. For each entry choose `status` (the state at the end of the period) and write:
   - `subject`: the object of the status verb, a Korean noun phrase **without** 을/를. The tool builds the title `{subject}을/를 {status phrase}.`:
     `A사 제안서 작성` + in_progress → "A사 제안서 작성을 진행중입니다."
     For `reviewing` use `예산안` → "예산안을 검토중입니다." (not `예산안 검토`).
   - `paragraph`: follow the writing rules below.
5. `weekly_save`; fix all errors and retry. Show the user the final text (the `text` field of the result, or `render(kind="weekly", key=..., format="text")`) and ask them to review it. **Do not send it anywhere** (email, Confluence, messenger) unless the user explicitly asks.

Statuses: `started` 착수하였습니다 · `in_progress` 진행중입니다 · `reviewing` 검토중입니다 · `done` 완료하였습니다 · `on_hold` 보류중입니다

### Writing rules for weekly paragraphs

- Exactly one paragraph per topic: no line breaks, bullets, numbering or markdown.
- 존댓말 보고체: every sentence ends with "~니다.", e.g. "~하였습니다.", "~진행하였습니다.", "~예정입니다.", "~중입니다.". Never "~함.", "~임.", "~했다.", "~해요.".
- 2-6 sentences, in this order where the evidence covers it: 배경/목적 → 이번 기간 수행 내용 (산출물, 수치, 날짜) → 현재 상태 → 향후 계획.
- Only 수행 내용 and 현재 상태 are required. Write 배경/목적, causes and 향후 계획 only when a daily log, a note or a document states them (a schedule, a deadline, an agreed next step). Never invent a plan, a reason or a cause: a paragraph without a 향후 계획 sentence is normal.
- Facts only from daily logs and notes. Daily-log inferences ("~로 보임") are not facts: leave them out or keep them hedged. Leave out what you would have to guess.
- Name documents by what they are ("A사 제안서", "3분기 실적 분석 자료"), not by file path.
- Dates as "11월 4일", amounts as "1,350만 원", percentages as "12.5%".
- Do not repeat the title sentence inside the paragraph.

```json
{"period_from": "2026-11-02", "period_to": "2026-11-13",
 "topics": [{"topic_ids": ["T003"], "subject": "A사 제안서 작성", "status": "in_progress",
             "paragraph": "10월 28일 착수한 A사 제안서 초안 작성을 이어서 진행하였습니다. 10월 30일 회의에서 제기된 요구사항을 반영하여 추진 일정(3장)을 신규로 작성하였고, 인건비 단가를 1,200만 원에서 1,350만 원으로 조정하였습니다. 현재 전체 5개 장 중 4개 장의 작성을 완료하였습니다."}]}
```

Rendered:

```
주간 업무 보고 (2026.11.02 ~ 2026.11.13)

1. A사 제안서 작성을 진행중입니다.
10월 28일 착수한 A사 제안서 초안 작성을 이어서 진행하였습니다. 10월 30일 회의에서 ...
```

## Topics

- A topic is one stream of work a manager would recognize: a project, a deliverable, a recurring duty. Its ID (`T001`, ...) is reused across days and weeks.
- `name`: a specific Korean noun phrase such as "A사 제안서 작성", "3분기 실적 분석", "신규 입사자 교육 자료 준비". Avoid vague names like "문서 작업".
- `paths`: subfolders or files (relative to the work folder) that belong to the topic, e.g. `프로젝트/A사`; they help match future changes. For files directly in the work folder, list the files: `.` is the whole work folder and matches every file.
- Keep `status` current (`topic_upsert(topic_id=..., status=...)`). `weekly_save` records the reported status automatically.

## Notes

When the user mentions work that leaves no trace in files (meetings, calls, reviews, decisions), save it with `note_add` (the day defaults to today) and use it as evidence in reports.

## First run

With no topics and no folder map yet: run `overview(depth=2)`, read a few representative files (`read_file` with a short `max_chars`), save a folder map, and create topics for clearly ongoing work. The first close-outs after installation may show many `no_baseline` changes: describe them as the current state, not as new work.
