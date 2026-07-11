# OpenCode Symphony

个人开发者用的 Issue-Driven Agent 编排系统 — 基于 OpenCode + git worktree + gh CLI。

## 架构

```
issues/ → orchestrator → worktree → opencode agent → reviewer → (fix → reviewer)* → CI → PR → done
```

### 状态机

```
queued → running → reviewing → succeeded/failed
                      │
                      ├─ PASS + N<min → reviewing（继续审查）
                      ├─ PASS + N≥min → reconcile → succeeded
                      ├─ FAIL + N<max → running（fixer 修复）
                      └─ FAIL + N≥max → failed
                  ↘ (stall/crash) → retrying → running/reviewing
```

### 工作流

| 阶段 | 说明 |
|------|------|
| dispatch | 创建 worktree，分支，生成 prompt，启动 worker agent |
| monitor | 检测 agent 是否完成（PID 存活检测） |
| review | worker 完成后启动只读 reviewer agent，输出结构化 JSON 报告 |
| fix | reviewer FAIL 时启动 fixer（复用 worker agent）按反馈修复 |
| reconcile | 连续 `min_iterations` 轮 PASS 后运行 CI → push → 创建 PR |
| retry | stall/crash/reconcile 失败后指数退避重试（最多 `max_retries` 次） |

### 审查循环（multi-round review loop）

worker 完成后，orchestrator 自动启动 `symphony-reviewer` agent 对改动做只读审查。
reviewer 输出 `.san/review/review-{N}.json`，包含 `verdict`（PASS/FAIL）+ 结构化 `feedback` 数组。

orchestrator 按决策表路由：

| verdict | iteration | 动作 |
|---------|-----------|------|
| PASS | < `min_iterations` | 再启动一轮 reviewer（一致性复查） |
| PASS | ≥ `min_iterations` | 进入 reconcile（CI + PR） |
| FAIL | < `max_iterations` | 启动 fixer（复用 worker agent）按反馈修复 |
| FAIL | ≥ `max_iterations` | `mark_failed` 放弃 |

每轮审查记录（含 reviewer pid、起止时间戳、完整 feedback）追加到 `Run.review_history`，
持久化到 `state/runs.jsonc`，用于人工复盘。reviewer 自身故障（未产出 JSON / JSON 非法 /
verdict 非法）也计入 `review_count`，避免死循环。

## 前置条件

### 1. 安装 opencode (≥ 1.17.7)

```bash
curl -fsSL https://opencode.ai/install.sh | sh
```

### 2. 配置 API keys

```bash
# Worker agent 用
export DEEPSEEK_API_KEY=sk-...

# 也可用 Anthropic
export ANTHROPIC_API_KEY=sk-...
```

### 3. 配置 gh

```bash
gh auth login
```

### 4. 设置 upstream remote

```bash
git remote add upstream <fork-target-url>
```

## 快速开始

```bash
# 运行 bootstrap（安装 agent + 预检）
python -m symphony_oc.bootstrap

# 启动 orchestrator
python -m symphony_oc.orchestrator
```

预期 bootstrap 输出：

```
  ✓ check_opencode_version
  ✓ check_external_tools
  ✓ check_git_remote
  ✓ install_agent
  ✓ install_reviewer_agent
  ✓ verify_agent_discoverable
  ✓ verify_reviewer_discoverable
  ✓ check_providers
  ✓ init_workspace
  ✓ check_reviewer_model
  ✓ smoke_test_agent
bootstrap complete
```

`check_reviewer_model` 会在 `agent.reviewer.extra_args` 缺少 `--model` 时打印软警告
（审查建议用强模型），在 `min_iterations > max_iterations` 时硬失败。

## 配置

编辑 `WORKFLOW.md` 的 YAML frontmatter：

```yaml
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
polling_interval_ms: 30000
```

### 配置说明

| 字段 | 说明 |
|------|------|
| `tracker.local_dir` | 本地 issues 目录 |
| `tracker.github` | GitHub issues 源（可选） |
| `git.remote` | upstream remote 名称 |
| `git.base_branch` | 工作分支基于哪个分支（通常是 `upstream/main`） |
| `git.worktree_root` | worktree 存放目录 |
| `git.two_commit_pr` | 是否启用两 commit PR 规则 |
| `agent.max_concurrent` | 最大并发 agent 数量 |
| `agent.stall_timeout_ms` | agent 无响应超时（默认 30 分钟） |
| `agent.max_retries` | 最大重试次数 |
| `agent.reviewer.name` | reviewer agent 名称（默认 `symphony-reviewer`） |
| `agent.reviewer.min_iterations` | PASS 也必须达到的轮数（默认 3） |
| `agent.reviewer.max_iterations` | FAIL 放弃前的最大轮数（默认 5） |
| `agent.reviewer.extra_args` | reviewer 的 opencode 参数，推荐 `["--model", "<strong-model>"]` |
| `ci.command` | CI 命令 |
| `ci.timeout_ms` | CI 超时 |

## 文件结构

```
symphony_oc/
├── orchestrator.py    # 主循环：dispatch + monitor + review route + retry
├── executor.py         # 创建 worktree + 启动 worker agent
├── reviewer.py         # 启动 reviewer / fixer，解析 review JSON
├── agent_runner.py     # 通用 opencode agent 启动器（_spawn_agent）
├── reconciler.py       # CI 验证 + push + PR 创建
├── config.py           # WORKFLOW.md 解析（含 ReviewerConfig）
├── state.py            # Run/Issue/ReviewRecord 数据模型 + 持久化
├── subproc.py          # run_bash 封装
├── bootstrap.py        # 预检 + agent 安装 + reviewer 配置校验
└── issue_source/
    ├── local.py        # 本地 issues 源
    └── github.py       # GitHub issues 源

agents/
├── symphony-worker.md    # 编码 / fixer agent（可写）
└── symphony-reviewer.md  # 只读审查 agent（bash 限 git status/diff/log/show）
```

### Reviewer agent 权限

`symphony-reviewer` 是只读 agent：

- **bash**: 仅允许 `git status` / `git diff` / `git log` / `git show`，其它一律 deny
- **edit**: 仅允许写 `.san/review/*`（输出 JSON 报告），其它一律 deny
- **其它**（webfetch / websearch / task / lsp / skill / external_directory / doom_loop）: 全部 deny

reviewer 在独立进程中运行，输出 `.san/review/review-{N}.json`，由 orchestrator 解析后按决策表路由。

## 详细了解

详细设计文档见 `2026-06-14-opencode-symphony-design.md`。