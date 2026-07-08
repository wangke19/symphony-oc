# OpenCode Symphony

个人开发者用的 Issue-Driven Agent 编排系统 — 基于 OpenCode + git worktree + gh CLI。

## 架构

```
issues/ → orchestrator → worktree → opencode agent → CI → PR → done
```

### 状态机

```
queued → running → succeeded/failed
                ↘ (stall/crash) → retrying → running
```

### 工作流

| 阶段 | 说明 |
|------|------|
| dispatch | 创建 worktree，分支，生成 prompt，启动 opencode agent |
| monitor | 检测 agent 是否完成（PID 存活检测） |
| reconcile | 运行 CI → push → 创建 PR |
| retry | stall/crash 后指数退避重试（最多 3 次） |

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
  ✓ verify_agent_discoverable
  ✓ check_providers
  ✓ init_workspace
  ✓ smoke_test_agent
bootstrap complete
```

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
| `ci.command` | CI 命令 |
| `ci.timeout_ms` | CI 超时 |

## 文件结构

```
symphony_oc/
├── orchestrator.py    # 主循环：dispatch + monitor + retry
├── executor.py         # 创建 worktree + 启动 opencode agent
├── reconciler.py       # CI 验证 + push + PR 创建
├── config.py           # WORKFLOW.md 解析
├── state.py            # Run/Issue 数据模型 + 持久化
├── subproc.py          # run_bash 封装
├── bootstrap.py        # 预检 + agent 安装
└── issue_source/
    ├── local.py        # 本地 issues 源
    └── github.py       # GitHub issues 源

agents/
└── symphony-worker.md  # 编码 agent 配置（权限 + system prompt）
```

## 详细了解

详细设计文档见 `2026-06-14-opencode-symphony-design.md`。