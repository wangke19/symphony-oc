---
tracker:
  local_dir: "./issues"
  github:
    repo: "owner/repo"
    labels: ["symphony"]
    active_states: ["open"]
git:
  remote: "upstream"
  base_branch: "upstream/main"
  worktree_root: "./worktrees"
  two_commit_pr: true
  two_commit_exclude:
    - "*.lock"
    - "requirements*.txt"
    - "*.generated.*"
    - "vendor/"
    - "zz_generated*"
ci:
  command: "pytest -q"
  timeout_ms: 120000
agent:
  name: "symphony-worker"
  max_concurrent: 3
  stall_timeout_ms: 1800000
  max_retries: 3
  retry_backoff_ms: 10000
  extra_args: ["--pure"]
local_issue:
  retrigger: "hash"
polling_interval_ms: 30000
---

# Agent system prompt

你是一个编码 Agent，正在处理来自 Issue Tracker 的任务。

## 任务描述

{{issue.description}}

## 编码规则

- 保持现有代码风格
- 修改后必须确保 CI 通过
- 每个 commit 附带清晰的 message
