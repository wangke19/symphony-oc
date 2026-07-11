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
  # reviewer:
  #   name: "symphony-reviewer"
  #   min_iterations: 3       # PASS 也必须满 3 轮（一致性检查）
  #   max_iterations: 5       # FAIL 超 5 轮放弃
  #   # extra_args: 推荐指定强模型：
  #   #   ["--model", "anthropic/claude-opus"]   # Claude Opus
  #   #   ["--model", "bigmodel/coding"]         # GLM 5.x
  #   # 留空则用 opencode 默认模型（不推荐 — 审查应用强模型）
  #   extra_args: []
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
