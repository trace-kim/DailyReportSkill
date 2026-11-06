---
description: worklog 추적 상태와 다음 할 일을 확인합니다 (worklog)
---
Report worklog's state for this work folder in Korean, using the worklog MCP tools. Change nothing.

1. `status`. If `initialized` is false: say that tracking has not started and that `/worklog-init` starts it, then stop.
2. Summarize briefly:
   - last scan and close-out; files waiting for close-out (`pending_close`)
   - days that need a daily log (`days_needing_report`, with the reason) and the next weekly period (`weekly`)
   - baseline progress (`baseline_remaining`; `baseline_skipped_old` files are old and deliberately skipped)
   - background work (`background`, `scan_running`)
   - scheduled tasks (`scheduled_tasks`: found or not, last run and result) and a failed unattended report run (`last_report_run`)
3. The next steps from `hints`, in plain Korean (for example "/daily-report로 2일치 일지를 작성할 수 있습니다").
