---
description: 일일 업무 일지를 모아 주간 업무 보고를 작성합니다 (worklog)
---
Write the Korean weekly report (주간 업무 보고) with the worklog MCP tools, following the "Weekly report" workflow and writing rules of the `worklog` skill. Load that skill first if it is not loaded yet.

Arguments: "$ARGUMENTS"
- empty: the period is `status.weekly.next_period_from` to `status.weekly.suggested_period_to`.
- "FROM TO" (YYYY-MM-DD YYYY-MM-DD) or a phrase such as "지난 2주": use that period.

Essentials:
1. Every day in the period with recorded changes must have a daily log; write missing ones first.
2. Read all daily logs in the period, group the items by topic, keep what a senior manager should hear, and merge small related topics.
3. One entry per topic: `subject` (the object noun phrase, without 을/를), `status`, and one `paragraph` in 존댓말 보고체. Every sentence ends with "~니다.". Use 3-6 sentences: 배경 → 수행 내용 (구체적 수치·날짜·산출물) → 현재 상태 → 향후 계획.
4. `weekly_save`; fix every error and retry.
5. Show the final text to the user and ask for review. Do not send it anywhere unless explicitly asked.
