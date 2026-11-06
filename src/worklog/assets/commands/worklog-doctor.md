---
description: 문서 읽기(DRM·Office 자동화)를 점검합니다 (worklog)
---
Check how this work folder's documents can be read, with the worklog `doctor` tool. Change nothing.

Arguments: "$ARGUMENTS" ("다시" or "rerun": check again even if there is a recent result)

1. `doctor` (with `rerun=true` if the arguments ask to check again). While it answers `running`, call it again (it takes 1-3 minutes; stop after 10 tries and say it continues in the background).
2. Reply in Korean: when it was checked (`checked_at`), Office versions and start times (`office`), whether the session is interactive (`session`), and per file type whether files are readable directly, only through Office (DRM), or not at all. For types that are not readable, say that daily logs will describe those files by name and folder only, unless another tool available to you can read DRM-protected documents.
3. If a verdict says files written by Python change after writing (DRM auto-encryption), explain that reports should be stored outside the DRM area (`[reports] dir` in `.worklog\config.toml`).
