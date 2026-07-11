# OpenCode Symphony — 实施方案

> 基于 OpenAI Symphony 方法论，用 OpenCode 能力实现最小化 Issue-Driven Agent 编排系统。

## 1. 项目概述

### 1.1 目标

把 OpenAI Symphony 的"Issue → Agent 执行 → CI 验证 → PR"循环，用 OpenCode 自身能力（Agent、Permissions、opencode run、git）重新实现一个**个人开发者可用**的最小化版本。

### 1.2 核心差异 vs OpenAI Symphony

| 维度 | OpenAI Symphony | OpenCode Symphony（本方案） |
|------|----------------|---------------------------|
| 语言 | Elixir/OTP | Python |
| 编排器 | 独立守护进程 | Python 守护进程 + systemd |
| Agent 运行时 | OpenAI Codex App Server | OpenCode（opencode run） |
| Issue 源 | Linear（仅） | GitHub Issues + 本地 issues/ 目录 |
| 隔离 | 文件系统 workspace 目录 | Git worktree（每 Run 独立工作目录） |
| 并发 | OTP 进程树 | Python 子进程 + asyncio |
| 权限 | Sandbox 策略 | OpenCode permission 系统 |
| 验证 | CI 状态 + PR review | CI 命令 + gh pr create |
| 定位 | 团队级 | 个人开发者 |

### 1.3 用户选择汇总

| 决策项 | 选择 |
|--------|------|
| 使用场景 | 个人开发工作流，Issue 驱动 |
| Issue 源 | 本地目录 + GitHub Issues（两者都要） |
| 隔离方式 | Git branch 隔离，独立分支 + 自动 PR |
| 权限 | Workspace Write（可写当前分支 + 跑 bash，不能删文件/改 git history/上外网） |
| 验证 | CI 验证（跑测试/lint/build，失败修复） |
| 并发 | 有限并发（最多 3 个并行，代码变更类互斥） |
| Prompt 注入 | 生成临时 prompt 文件 |
| PR 提交 | `gh pr create` CLI |
| 状态持久化 | JSONC（可读可编辑） |
| **代码审查** | **多轮审查循环：实现（便宜模型）→ 审查（GLM 5.1/5.2）→ 修复 → 再审查，至少 3 轮** |
| **CI 监控** | **PR 创建后自动轮询 CI 状态，失败自动获取日志并修复推送** |

---

## 2. 架构设计

### 2.1 整体流程图

```
orchestrator（守护进程，systemd 管理）
  │
  ├── poll_issues()              ← 轮询本地目录 + GitHub API
  │
  ├── dispatch(issue)            ← 认领 → 建 worktree → 生成 prompt → opencode run
  │     │
  │     └── implementer 子进程（symphony-worker，便宜模型）
  │           ├── 读 Issue prompt → 写代码 → 提交
  │           └── 退出（成功/超时）
  │
  ├── [审查循环] dispatch_review(run)  ← implementer 完成
  │     │
  │     ├── reviewer 子进程（symphony-reviewer，GLM 5.1/5.2，只读）
  │     │     ├── git diff 审查代码 → 写 review 文件
  │     │     └── 退出
  │     │
  │     ├── PASS + ≥3 轮 → 进入 reconcile
  │     ├── FAIL + <5 轮 → dispatch_fix → 再次 dispatch_review
  │     └── FAIL + ≥5 轮 → mark_failed
  │
  ├── reconcile(run)             ← 审查通过后在 worktree 内 CI 验证 + 创建 PR
  │     ├── CI 通过 → push → gh pr create → 进入 CI 监控
  │     └── CI 失败 → mark_failed
  │
  ├── [CI 监控] process_ci_monitoring()  ← PR 创建后持续轮询
  │     ├── gh pr checks → all passed → mark_succeeded
  │     ├── any failure → get_failed_logs → dispatch_ci_fix → push → 继续监控
  │     └── timeout → mark_failed
  │
  └── loop()                     ← 控制并发上限，检测卡死，重试队列
```

### 2.2 核心组件

#### orchestrator.py — 主循环

- 启动后常驻运行
- 每 30 秒轮询一次 Issue 源
- 维护运行态 map：`{issue_id: run_state}`
- 并发控制：最多 3 个 `opencode run` 并行
- 卡死检测：子进程运行时长超过 stall_timeout_ms（wall clock）→ 中断

#### issue_source/ — Issue 适配器

统一接口 `fetch_issues() → list[Issue]`，两个实现：

- `local.py` — 读 `issues/` 目录，每个 `.md` 文件一个 Issue
- `github.py` — 用 `gh` CLI 拉取 GitHub Issues（过滤 label）

#### executor.py — Agent 启动器

- 为每个 Issue 创建 git branch：`symphony/{issue_id}/{short_title}`
- 生成临时 prompt 文件：`issues/{issue_id}.prompt`
- 启动子进程：`opencode run --agent symphony-worker --dir {worktree} "{prompt}"`（prompt 作为 positional message 传入；无 `--max-steps`，靠 `doom_loop: deny` + `stall_timeout_ms` 兜底）
- 等待完成，记录 stdout/stderr 到 `log/{issue_id}.log`

#### reconciler.py — 验证器

- 检查 `git diff --stat` — 有变更继续，无变更标记失败
- 跑 CI 命令（从 `WORKFLOW.md` 读取 `ci.command`）
- 成功 → `git push` → `gh pr create` → 进入 CI 监控
- 失败 → 记录错误

#### reviewer.py — 多轮审查模块

- `dispatch_review(run, cfg, base_ref, previous_feedback) -> pid` — 启动 reviewer agent
- `dispatch_fix(run, cfg, review_feedback) -> pid` — 启动 worker agent 修复问题
- `dispatch_ci_fix(run, cfg, ci_failed_log) -> pid` — 启动 worker agent 修复 CI 失败
- `parse_review_result(wt_path, review_count) -> ReviewResult` — 解析审查结论

#### ci_monitor.py — CI 监控模块

- `poll_pr_checks(pr_url) -> CiCheckStatus` — 通过 `gh pr checks --json` 查询 PR 检查状态
- `get_failed_logs(failures) -> str` — 获取失败 job 的日志

#### config.py — 配置加载

- 解析 `WORKFLOW.md` 的 YAML frontmatter
- 提供类型化 getter

### 2.3 文件结构

```
symphony-oc/
├── orchestrator.py        # 主循环 + 并发管理 + 审查路由 + CI 监控
├── issue_source/
│   ├── __init__.py        # Issue 数据模型
│   ├── local.py           # 本地目录适配器
│   └── github.py          # GitHub API 适配器
├── executor.py            # opencode run 启动器 + _spawn_agent 通用 helper
├── reviewer.py            # 审查/修复 prompt 生成 + dispatch 函数
├── ci_monitor.py          # PR checks 轮询 + 失败日志获取
├── reconciler.py          # CI 验证 + PR 提交 + try_reconcile(返回 pr_url)
├── config.py              # 配置加载
├── state.py               # Run/Issue 数据模型 + 原子持久化 + 状态转移 helper
├── subproc.py             # run_bash 封装
├── bootstrap.py           # 预检：worker + reviewer agent 安装，bigmodel 检查
├── WORKFLOW.md            # 用户声明式配置
├── agents/
│   ├── symphony-worker.md # 编码 agent（受限权限，写 + build + commit）
│   └── symphony-reviewer.md # 审查 agent（只读权限，仅 git diff/log）
├── state/
│   ├── .gitignore
│   └── runs.jsonc         # 运行状态持久化
├── log/
│   ├── *.log              # 结构化日志
│   └── *.review.log       # 审查日志
├── issues/
│   └── *.prompt           # 生成的临时 prompt 文件
├── worktrees/             # git worktree 工作目录
├── tests/                 # 单元测试
└── README.md
```

### 2.4 Worktree 隔离与生命周期

每个 Run 在独立的 git worktree 内执行，避免并发 Run 互相污染工作树（单工作树无法同时 checkout 多个分支，必须靠 worktree 实现真并发）。

**创建（dispatch 时）：**

```bash
git fetch {REMOTE}
git worktree add -b symphony/{issue_id}/{slug} \
    {WORKTREE_ROOT}/{issue_id} \
    {BASE_BRANCH}
```

- 每个 worktree 目录独立，opencode 子进程的 `cwd` 设为该目录
- 并发 Run 之间互不影响

**销毁（reconcile 成功后）：**

```bash
git worktree remove {WORKTREE_ROOT}/{issue_id} --force
git branch -D symphony/{issue_id}/{slug}
```

**失败保留：CI 失败时不删 worktree/分支，已 push 为 `[wip]` 分支，便于 `git log` / `gh pr view` 排查。**

**孤儿清理（orchestrator 启动时，三层扫描）：**

1. **僵尸 Run** — `state/runs.jsonc` 中 `status ∈ {running, queued, retrying}` 但进程已不在：
   - `running` + pid 不存活（`os.kill(pid, 0)` 抛 OSError）→ 调 `schedule_retry(run, "stalled: pid dead")`
   - `queued` / `retrying` 停留超过 `STALL_TIMEOUT_MS` → `mark_failed(run, "orphan after crash")`（attempt 不累加，避免重启循环刷爆重试上限）
2. **孤儿 worktree** — `./worktrees/` 出现但 `runs.jsonc` 无对应 `issue_id` 的目录 → `git worktree remove --force`
3. **git 元数据** — `git worktree prune` 清理物理删除但元数据残留的条目

```python
def cleanup_orphans():
    runs = state.load_all()
    active_ids = {r.issue_id for r in runs}
    active_wts = {Path(r.worktree).name for r in runs if r.worktree}

    for r in runs:
        if r.status == "running" and not is_pid_alive(r.pid):
            schedule_retry(r, "stalled: pid dead after restart")
        elif r.status in ("queued", "retrying") and stale_for(r) > STALL_TIMEOUT_MS:
            mark_failed(r, f"orphan after crash (status={r.status})")

    for wt_dir in Path(WORKTREE_ROOT).iterdir():
        if wt_dir.name not in active_wts:
            run_bash(f"git worktree remove {wt_dir} --force", check=False)

    run_bash("git worktree prune")
```

**关键约束：**

- `worktrees/` 必须加入 `.gitignore`
- 所有 git 操作显式 `git -C {worktree}` 或在子进程设 `cwd`
- 不在 worktree 内执行跨分支 `git checkout`

### 2.5 权限模型与 Agent 定义

opencode 1.17.7 的权限系统通过 **agent 文件**（markdown + YAML frontmatter）声明，不靠 CLI flag。本方案定义一个受限的 `symphony-worker` agent。

**安装位置：** `~/.config/opencode/agents/symphony-worker.md`（全局共享，因为每个 worktree 是独立 cwd，找不到主仓的 `agents/`）。

**`agents/symphony-worker.md` 模板：**

```markdown
---
description: Symphony Worker — Issue 驱动的受限编码 agent
mode: primary
permission:
  # 默认拒绝网络
  webfetch: deny
  websearch: deny
  # 不允许派生子 agent / 写 todo（避免失控）
  task: deny
  todowrite: deny
  # 禁用 LSP（启动开销大）和 skill（避免副作用）
  lsp: deny
  skill: deny
  # 文件操作允许（限定在 worktree 内，由 external_directory 兜底）
  read: allow
  edit: allow
  glob: allow
  grep: allow
  # bash 按命令白名单
  bash:
    "*": ask                      # 默认每次询问（被 --dangerously-skip-permissions 跳过时变 deny）
    "pytest *": allow
    "go test *": allow
    "make *": allow
    "gofmt *": allow
    "golangci-lint *": allow
    "git status": allow
    "git diff *": allow
    "git add *": allow
    "git commit *": allow
    "rm *": deny                  # 禁删文件
    "git push *": deny            # push 由 reconciler 负责
    "git reset --hard *": deny    # 禁改 history
    "git rebase *": deny
    "git checkout *": deny        # 禁切分支（隔离在 worktree 内）
  # 跨目录访问：worktree 内允许，外部一律拒绝
  external_directory: deny
  # 防止 agent 无限循环（替代 max-steps）
  doom_loop: deny
---

你是 Symphony Worker Agent。在当前 git worktree 内处理来自 Issue Tracker 的开发任务。

## 约束

- **不要** 执行 `git push`、`git reset --hard`、`git rebase`、`git checkout`（由 orchestrator 处理）
- **不要** 删除文件（`rm`）
- **不要** 访问 worktree 之外的目录
- **不要** 联网（webfetch / websearch 已禁用）
- 修改后确保 CI 命令通过（具体命令在每次任务的 prompt 里给出）
- 完成后退出，**不要** 进入交互模式
```

> **注：** agent 文件是**静态**系统 prompt，不含任务相关变量（如 `{{issue.description}}`）。任务内容作为 `opencode run` 的 positional message 传入（见 4.2 / 5.2）。无需 Jinja2 渲染 agent 文件本身。

**关键决策：**

| 决策 | 理由 |
|------|------|
| `doom_loop: deny` | opencode 没有原生 `--max-steps` flag；这是 turn-limit 的等价机制，触发后 agent 自动停止 |
| `external_directory: deny` | 把 agent 锁在 worktree 内，配合 `--dir {wt_path}` 实现路径隔离 |
| `bash` 白名单 | 只放行测试/lint/build/只读 git；破坏性操作（push/reset/rebase/checkout/rm）一律 deny |
| 全局安装 | 每个 worktree 是独立 cwd，找不到主仓 `agents/`；全局 `~/.config/opencode/agents/` 是唯一可靠位置 |
| `--dangerously-skip-permissions` 不使用 | 该 flag 把 `ask` 当 `allow`，会绕过 bash 白名单的 `*` 默认；本方案**不传**该 flag，让 `ask` 走交互式 prompt（agent 子进程没 TTY 时，等同 deny） |

**安装方式：** 见 6.3 `install_agent()`（直接复制 bundled 文件，hash 比对判定是否需要覆盖）。

**Reviewer Agent（`symphony-reviewer.md`）：**

审查 agent 只有**只读**权限，无法修改任何文件：

```yaml
permission:
  reads: allow
  edit: deny
  bash:
    "*": deny
    "git status": allow
    "git diff *": allow
    "git log *": allow
  external_directory: deny
```

System prompt 引导 reviewer 执行 `git log` / `git diff` 审查，将结果写入 `.san/review/review-N.md`，格式为 `## 审查结论` PASS/FAIL + `## 详细反馈`。

**模型选择：** 实现用便宜模型（如 deepseek-v4-flash），审查用强模型（`bigmodel/coding`，GLM 5.1/5.2）。通过 `opencode run --model` 指定，在 `ReviewerConfig.extra_args` 和 `CiMonitorConfig.fixer_extra_args` 中配置。

**opencode 版本要求：** ≥ 1.17.7（permission schema 可能随版本变化，已在 Section 9.1 列为约束）。

---

## 3. 数据模型

### 3.1 Issue

```python
@dataclass
class Issue:
    id: str              # 如 "local-001" 或 "GH-42"
    title: str           # Issue 标题
    description: str     # Issue 正文
    labels: list[str]    # 标签
    source: str          # "local" | "github"
    created_at: datetime
```

### 3.2 Run

```python
@dataclass
class Run:
    issue_id: str
    title: str
    branch: str          # git 分支名，如 "symphony/local-001/add-auth"
    worktree: str        # worktree 绝对路径，如 "./worktrees/local-001"
    content_hash: str    # issue 描述的 sha256[:12]，用于本地 issue 重触发判定（见 5.7）
    status: str          # "queued" | "running" | "reviewing" | "reconciling" | "ci_monitoring" | "succeeded" | "failed" | "retrying"
    attempt: int         # 第几次尝试
    pid: int | None      # opencode run 子进程 PID
    error: str | None    # 错误信息
    pr_url: str | None   # 创建 PR 后的链接
    next_retry_at: datetime | None  # retrying 状态下的下次重试时间
    started_at: datetime
    finished_at: datetime | None
    # 审查循环字段
    review_count: int = 0        # 已执行的审查轮次
    review_passed: bool = False  # 最新一次审查是否通过
    review_feedback: Optional[str] = None  # 最新审查反馈
    # CI 监控字段
    ci_poll_count: int = 0       # CI 轮询次数
    ci_failed_log: Optional[str] = None  # CI 失败日志（触发 CI 修复用）
```

### 3.3 JSONC 状态文件

> **格式见 5.8（扁平化设计）。** 下面是字段示意（旧三段式已废弃）：

```jsonc
// state/runs.jsonc
{
  "running": {
    "local-001": {
      "issue_id": "local-001",
      "title": "添加用户认证接口",
      "branch": "symphony/local-001/add-auth",
      "worktree": "./worktrees/local-001",
      "status": "running",
      "attempt": 1,
      "pid": 12345,
      "error": null,
      "started_at": "2026-06-14T10:00:00",
      "finished_at": null,
      "pr_url": null
    }
  },
  "history": [
    {
      "issue_id": "local-000",
      "title": "初始化项目结构",
      "branch": "symphony/local-000/init",
      "worktree": null,
      "status": "succeeded",
      "attempt": 1,
      "pid": 12340,
      "error": null,
      "started_at": "2026-06-14T08:00:00",
      "finished_at": "2026-06-14T08:15:00",
      "pr_url": "https://github.com/.../pull/1"
    }
  ],
  "retry_queue": [],
  "last_poll": "2026-06-14T10:30:00"
}
```

---

## 4. 配置规范

### 4.1 WORKFLOW.md

```markdown
---
tracker:
  local_dir: "./issues"
  github:
    repo: "owner/repo"
    labels: ["symphony"]
    active_states: ["open"]
git:
  remote: "upstream"             # push 目标（CLAUDE.md 要求 upstream/main）
  base_branch: "upstream/main"   # 新分支基线，dispatch 时 git fetch + worktree add 锁定
  worktree_root: "./worktrees"   # worktree 根目录（必须 .gitignore）
  two_commit_pr: true            # 遵循 CLAUDE.md 两 commit 规则：代码 / 依赖+生成物
  two_commit_exclude:            # Commit 1 排除 glob，剩余归 Commit 2（按目标仓库语言调整）
    - "*.lock"                   # Python: poetry.lock / uv.lock / pipenv.lock
    - "requirements*.txt"        # Python: requirements.txt / requirements-dev.txt
    - "*.generated.*"            # 通用生成物
    - "vendor/"                  # Go / 通用 vendored deps
    - "zz_generated*"            # Kubernetes 风格 codegen
ci:
  command: "pytest -q"           # 全局引用名 CI_COMMAND
  timeout_ms: 120000             # 全局引用名 CI_TIMEOUT_MS
agent:
  name: "symphony-worker"        # opencode agent 名（必须已安装到 ~/.config/opencode/agents/）
  max_concurrent: 3
  stall_timeout_ms: 1800000      # 30 分钟 wall clock；opencode 无原生 max-steps，靠 doom_loop + 此超时
  max_retries: 3
  retry_backoff_ms: 10000        # 指数退避基数；上限 MAX_RETRY_BACKOFF_MS = 60000
  extra_args: ["--pure"]         # 透传给 opencode run（如 --format json 调试时用）
  reviewer:
    name: "symphony-reviewer"
    min_iterations: 3
    max_iterations: 5
    extra_args: ["--model", "bigmodel/coding"]
  ci_monitor:
    enabled: true
    polling_interval_ms: 30000
    timeout_ms: 600000
    fixer_extra_args: ["--model", "bigmodel/coding"]
local_issue:
  retrigger: "hash"              # "hash"（内容变才重跑）| "never"（一次性）| "mtime"
polling_interval_ms: 30000
---

# Agent 系统提示

你是一个编码 Agent，正在处理来自 Issue Tracker 的任务。

## 任务描述

{{issue.description}}

## 编码规则

- 保持现有代码风格
- 修改后必须确保 CI 通过
- 每个 commit 附带清晰的 message
```

### 4.2 Prompt 文件格式

临时生成的 prompt 文件（归档 + 重放用）；实际通过 `opencode run` 的 **positional message** 参数传入（opencode 1.17.7 无 `--prompt-file` flag）。模板引擎用 **Jinja2**（`{{ var }}` 语法），未列出的 `agent.max_turns` 等已删除（opencode 无对应 CLI 参数）。

```
你是 OpenCode Agent，正在处理一个来自 Issue Tracker 的开发任务。

## Issue 信息
- ID: {{ issue.id }}
- 标题: {{ issue.title }}
- 来源: {{ issue.source }}

## 任务描述
{{ issue.description }}

## 执行要求
1. 你已被切到独立 worktree（branch: symphony/{{ issue.id }}/{{ issue.slug }}），cwd 即工作目录
2. 实现 Issue 描述的功能
3. 确保 CI 命令 "{{ ci.command }}" 通过
4. **不要** 执行 git push / git reset --hard / git rebase / git checkout（agent 权限已 deny）
5. 完成后退出，不要进入交互模式
```

---

## 5. 核心循环逻辑

### 5.1 主循环

> **代码中全局变量绑定（Minor #11 落实）**
> - `REMOTE`, `BASE_BRANCH`, `WORKTREE_ROOT`, `TWO_COMMIT_PR`, `TWO_COMMIT_EXCLUDE` ← `config.git.*`
> - `CI_COMMAND`, `CI_TIMEOUT_MS` ← `config.ci.*`
> - `AGENT_NAME`, `AGENT_EXTRA_ARGS`, `STALL_TIMEOUT_MS`, `MAX_CONCURRENT`, `MAX_RETRIES` ← `config.agent.*`
> - `MAX_RETRY_BACKOFF_MS` ← 常量 `60_000`（固定上限）
> - `POLLING_INTERVAL_MS`, `STATE_DIR` ← `config.*`
>
> 配置加载在启动时一次性完成（`config.load()` 返回 dataclass）；不要在循环内重新读 YAML。

```python
def loop():
    """主循环：轮询 → 调度 → 审查路由 → CI 监控 → 检查卡死 → 清理"""
    while True:
        with open(STATE_DIR / ".lock", "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)

            issues = issue_source.fetch_issues()
            runs = state.load_all()

            # 1. 调度新 Issue
            for issue in issues:
                if should_dispatch(issue, runs) and can_dispatch(runs):
                    dispatch(issue)

            # 2. 检查完成 agent 并路由到下一阶段
            process_completed(runs, cfg, issues_map)
            #    - implementer 完成 → dispatch_review (reviewing)
            #    - reviewer 完成 → PASS: reconcile / FAIL: dispatch_fix
            #    - ci-fixer 完成 → push + CI monitoring

            # 3. CI 监控（PR 创建后轮询）
            process_ci_monitoring(runs, cfg)
            #    - all checks passed → mark_succeeded
            #    - any failure → dispatch_ci_fix
            #    - timeout → mark_failed

            # 4. 检查卡死
            check_stalls(runs)

            # 5. 重试队列
            process_retry_queue(runs)

        sleep(POLLING_INTERVAL_MS)
```

### 5.2 dispatch(issue)

```python
def dispatch(issue: Issue) -> Run | None:
    """返回 Run 表示成功启动；返回 None 表示未启动（已记日志，主循环继续）。
    任何子步骤失败都不应抛出 —— 单个 issue 调度失败不能拖垮整个 orchestrator。"""
    branch = f"symphony/{issue.id}/{slugify(issue.title)}"
    wt_path = f"{WORKTREE_ROOT}/{issue.id}"
    prompt_path = f"issues/{issue.id}.prompt"

    try:
        # 1. 锁定基线到 upstream/main（CLAUDE.md 要求）
        run_bash(f"git fetch {REMOTE}")

        # 2. 创建独立 worktree + 分支（解决并发隔离）
        run_bash(f"git worktree add -b {branch} {wt_path} {BASE_BRANCH}")

        # 3. 生成 prompt 文件（写到主仓 issues/，仅作归档/重放）
        write_file(prompt_path, generate_prompt(issue))

        # 4. 启动 opencode run
        #    - 没有 --prompt-file flag：prompt 作为 positional message 传入
        #    - 没有 --max-steps flag：靠 agent 配置里的 doom_loop: deny + stall_timeout_ms
        #    - --dir 指向 worktree；agent 文件从 ~/.config/opencode/agents/ 全局加载
        #    - 不传 --dangerously-skip-permissions（让 ask 行为生效，无 TTY 时等同 deny）
        prompt_text = Path(prompt_path).read_text()
        cmd = [
            "opencode", "run",
            "--agent", AGENT_NAME,          # symphony-worker
            "--dir", wt_path,
            *AGENT_EXTRA_ARGS,              # 如 ["--pure"]；调试时加 "--format", "json"
            prompt_text,                    # positional message（多行也安全，subprocess 不经 shell）
        ]
        proc = subprocess.Popen(
            cmd, start_new_session=True,    # 独立 process group，便于 SIGKILL 整组
            stdout=open(f"log/{issue.id}.log", "wb"),
            stderr=subprocess.STDOUT,
        )

        # 5. 原子持久化状态（tmp + os.rename，避免 check-then-save 竞争）
        run = Run(issue_id=issue.id, title=issue.title,
                  branch=branch, worktree=wt_path,
                  content_hash=hash_issue(issue),
                  status="running", pid=proc.pid, attempt=1,
                  next_retry_at=None,
                  started_at=datetime.now())
        state.save_run_atomic(run)
        return run

    except (subprocess.CalledProcessError, OSError) as e:
        # git fetch / worktree add / Popen 失败：清理半成品 worktree，
        # 记一个 failed Run（attempt=1，不通过 schedule_retry —— 基础设施错误重试无意义）
        log.error(f"dispatch infra failed for {issue.id}: {e}")
        run_bash(f"git worktree remove {wt_path} --force", check=False)
        state.save_run_atomic(Run(
            issue_id=issue.id, title=issue.title, branch=branch,
            worktree=wt_path, content_hash=hash_issue(issue),
            status="failed", attempt=1, pid=None,
            error=f"infra: {e}", next_retry_at=None,
            started_at=datetime.now(), finished_at=datetime.now(),
        ))
        return None
```

### 5.3 reconcile(run)

```python
def reconcile(run: Run) -> None:
    """任何异常都通过 schedule_retry / mark_failed 转换为 Run 状态，
    不让 reconcile 抛出拖垮主循环。失败路径均保留 worktree 便于排查。"""
    wt = run.worktree
    try:
        # 1. 有变更才继续
        diff = run_bash(f"git -C {wt} diff --stat")
        if not diff.stdout.strip():
            mark_failed(run, "No changes detected")
            cleanup_worktree(run)
            return

        # 2. 跑 CI（在 worktree 内）
        ci = run_bash(CI_COMMAND, cwd=wt, timeout=CI_TIMEOUT_MS)
        if ci.returncode != 0:
            # 失败也提交 + push WIP 分支（push 失败不抛，本地仍可排查）
            commit_all(wt, f"wip: {run.title} (CI failed)")
            run_bash(f"git -C {wt} push {REMOTE} {run.branch}", check=False)
            schedule_retry(run, f"CI failed: {ci.stderr[-500:]}")
            return  # 不删 worktree/分支

        # 3. 两 commit 拆分（exclude 从 config 读，便于按目标仓库语言调整）
        if TWO_COMMIT_PR:
            commit_selective(wt, f"feat: {run.title}", exclude=TWO_COMMIT_EXCLUDE)
            if has_pending_changes(wt):
                commit_all(wt, "chore: update dependencies and generated artifacts")
        else:
            commit_all(wt, f"feat: {run.title}")

        # 4. push（失败 → 保留 worktree + 进重试队列）
        run_bash(f"git -C {wt} push {REMOTE} {run.branch}")

        # 5. gh auth 临门一脚检查（token 可能跨 bootstrap 期过期，见 9.1 #5）
        assert_gh_auth()

        # 6. 创建 PR
        pr_url = create_pr(run, ci_stdout=ci.stdout)

        mark_succeeded(run, pr_url)
        cleanup_worktree(run)

    except subprocess.CalledProcessError as e:
        # git push / commit 失败：保留 worktree，下一轮重试
        schedule_retry(run, f"reconcile subprocess failed: {e}")
    except GhAuthExpired as e:
        # gh token 过期：不删 worktree，不累加 attempt（等用户重登后下一轮自动续上）
        run.status = "retrying"
        run.error = f"gh auth expired: {e}"
        run.next_retry_at = datetime.now() + timedelta(minutes=5)
        state.save_run_atomic(run)


class GhAuthExpired(RuntimeError):
    """gh CLI token 失效。不删 worktree，等用户重登。"""


def assert_gh_auth() -> None:
    """创建 PR 前验证 gh token；过期则抛 GhAuthExpired。
    bootstrap 检查过一次，但 token 可能在长时间运行后过期。"""
    result = run_bash("gh auth status", check=False, timeout=10_000)
    if result.returncode != 0:
        raise GhAuthExpired("gh auth token expired or revoked. Run: gh auth login")


def create_pr(run: Run, ci_stdout: str) -> str:
    body = f"""## Summary
Implements {run.issue_id}: {run.title}

## Test plan
- [x] CI passed locally (`{CI_COMMAND}`)
- [ ] Reviewer approves

Closes #{run.issue_id.removeprefix("GH-")}
"""
    return run_bash([
        "gh", "pr", "create",
        "--base", BASE_BRANCH.split("/")[-1],
        "--head", run.branch,
        "--title", run.title,
        "--body", body,
    ])


def cleanup_worktree(run: Run) -> None:
    """PR 创建后清理本地 worktree；失败时由调用方决定是否调用。"""
    run_bash(f"git worktree remove {run.worktree} --force", check=False)
    run_bash(f"git branch -D {run.branch}", check=False)
```

### 5.4 多轮审查循环

实现 agent（symphony-worker，便宜模型如 deepseek-v4-flash）完成后，进入审查循环：

```
running (implementer done) → reviewing (reviewer agent) → running (fix agent) → reviewing → ...
  → PASS + ≥3 轮 → reconcile → ci_monitoring
  → FAIL + ≥5 轮 → mark_failed
```

**`_on_implementer_done(run)`** — implementer 退出 → dispatch reviewer：
- 设置 `run.status = STATUS_REVIEWING`
- 调用 `dispatch_review(run, cfg, base_ref, previous_feedback)` 启动 symphony-reviewer agent
- Reviewer agent 执行 `git log` / `git diff` 审查
- 将结论写入 `.san/review/review-{N}.md`

**`_on_reviewer_done(run)`** — reviewer 退出 → 解析结果 + 决策：

| 条件 | 动作 |
|------|------|
| PASS + review_count ≥ min_iterations(3) | `try_reconcile()` → PR → CI 监控 |
| PASS + review_count < min_iterations(3) | re-review（一致性检查） |
| FAIL + review_count < max_iterations(5) | `dispatch_fix()` → worker 修复 → 重新审查 |
| FAIL + review_count ≥ max_iterations(5) | `mark_failed()` |

**`parse_review_result(wt_path, review_count)`**:
- 读取 `.san/review/review-{N}.md`
- 正则提取 `## 审查结论` 后的 PASS/FAIL
- 提取 `## 详细反馈` 内容
- 返回 `ReviewResult(passed, feedback)`

### 5.5 CI 监控（PR 后）

PR 创建后，进入 `ci_monitoring` 状态，轮询 GitHub Actions 结果：

```python
def process_ci_monitoring(runs, cfg):
    for run in runs:
        if run.status != STATUS_CI_MONITORING:
            continue
        # 超时检查
        elapsed = (now - run.started_at) * 1000
        if elapsed > cfg.ci_monitor.timeout_ms:
            mark_failed(run, "CI timeout")
            continue

        status = poll_pr_checks(run.pr_url)   # gh pr checks --json
        if status.conclusion == "success":
            mark_succeeded(run, run.pr_url)
        elif status.conclusion == "failure":
            logs = get_failed_logs(status.failures)  # gh run view --log-failed
            run.ci_failed_log = logs
            dispatch_ci_fix(run, cfg, logs)   # worker 修复 CI 问题
        # else: still running, wait
```

### 5.6 并发控制

```python
def can_dispatch(running: dict) -> bool:
    return len([r for r in running.values() if r.status == "running"]) < MAX_CONCURRENT
```

### 5.7 卡死检测

```python
def check_stalls(running: dict) -> None:
    now = time.now()
    for run in running.values():
        if run.status != "running":
            continue
        elapsed = (now - run.started_at).total_seconds() * 1000
        if elapsed > STALL_TIMEOUT_MS:
            interrupt_process(run.pid)            # SIGKILL 整个 process group
            schedule_retry(run, "stalled: exceeded stall_timeout_ms")  # 见 5.11
```

> `mark_stalled` 已合并到 `schedule_retry`（5.11）—— stall 只是 retry 的一个原因，状态统一走 `retrying`，避免多一个查询态。

### 5.8 重试退避

```python
def retry_delay(attempt: int) -> int:
    """指数退避：10s, 20s, 40s, ..."""
    return min(10000 * (2 ** (attempt - 1)), MAX_RETRY_BACKOFF_MS)
```

### 5.9 本地 Issue 重触发

本地 `issues/*.md` 没有"已关闭"状态，每次 poll 都会重新发现。靠内容 hash 判定是否重跑。

> **⚠ 并发安全（review #1）：** `should_dispatch` 必须在主循环的 `state.lock` 内调用（见 5.1）。否则两个 poll 周期可能同时通过 `runs` 检查、各自创建 Run —— `save_run_atomic` 只保证单次写不撕裂，**不防御 check-then-act 跨周期窗口**。锁住整个 dispatch sweep 是最简单的修复。

```python
def should_dispatch(issue: Issue, runs: list[Run]) -> bool:
    if issue.source == "github":
        return not any(r.issue_id == issue.id and r.status in {
            "running", "succeeded", "queued",
        } for r in runs)

    # 本地 issue：hash 变了才算新任务
    content_hash = sha256(issue.description.encode()).hexdigest()[:12]
    for r in runs:
        if r.issue_id != issue.id:
            continue
        if r.content_hash == content_hash:
            return False              # 已跑过这个版本
        if r.status == "running":
            return False              # 老版本还在跑，等它结束
    return True
```

`Run` dataclass 新增 `content_hash: str` 字段；GitHub issue 也写入（取 issue body 的 hash），便于统一判定。

### 5.10 状态文件扁平化

原设计的 `running` / `history` / `retry_queue` 三结构需要同步，易出 bug。改为**单一 list + status 字段**：

```jsonc
// state/runs.jsonc（扁平结构）
{
  "runs": [
    {
      "issue_id": "local-001",
      "title": "...",
      "branch": "symphony/local-001/add-auth",
      "worktree": "./worktrees/local-001",
      "content_hash": "a3f9b2c1d4e5",
      "status": "running",            // 单一真相
      "attempt": 1,
      "pid": 12345,
      "started_at": "2026-06-14T10:00:00",
      "finished_at": null,
      "error": null,
      "pr_url": null
    },
    {
      "issue_id": "local-000",
      "status": "succeeded",
      "finished_at": "2026-06-14T08:15:00",
      "pr_url": "https://github.com/.../pull/1"
      // ...
    }
  ],
  "last_poll": "2026-06-14T10:30:00"
}
```

查询时按 status 过滤即可（内存中建索引）：

```python
def load_running(runs: list[Run]) -> dict[str, Run]:
    return {r.issue_id: r for r in runs if r.status in {"running", "queued", "retrying"}}

def load_retry_queue(runs: list[Run]) -> list[Run]:
    return [r for r in runs if r.status == "retrying"]
```

写盘仍走 `save_runs_atomic`（tmp + `os.rename`）。

### 5.11 重试队列处理

`process_retry_queue` 在主循环每轮 poll 中调用一次。来源：`schedule_retry()` 写入的 Run（CI 失败、git push 失败、stall、infra 错误等）。

```python
def process_retry_queue(runs: list[Run]) -> None:
    """处理 status=retrying 的 Run：到点重投或最终放弃。"""
    now = datetime.now()
    for run in runs:
        if run.status != "retrying":
            continue
        if run.next_retry_at and now < run.next_retry_at:
            continue                                      # 退避未到，跳过

        if run.attempt >= MAX_RETRIES:
            mark_failed(run, f"exhausted {MAX_RETRIES} retries (last: {run.error})")
            # worktree 保留供排查；不调用 cleanup_worktree
            continue

        # 到点重投：旧 Run 标记 failed（保留历史），重新 dispatch 一个新 Run（attempt+1）
        run.status = "failed"
        run.error = (run.error or "") + " → retried"
        run.finished_at = now
        state.save_run_atomic(run)

        # 重新构造 Issue 并 dispatch（dispatch 内部会自增 attempt）
        issue = issue_source.fetch_one(run.issue_id)
        if issue:
            issue._force_attempt = run.attempt + 1        # 透传给新 Run
            dispatch(issue)


def schedule_retry(run: Run, error: str) -> None:
    """mark_failed 的"软"版：状态置 retrying，next_retry_at = now + 退避。
    被 reconcile / check_stalls / cleanup_orphans 调用。"""
    run.status = "retrying"
    run.error = error
    run.next_retry_at = datetime.now() + timedelta(
        milliseconds=retry_delay(run.attempt)             # 见 5.6
    )
    state.save_run_atomic(run)
```

**关键决策：**

- 重试不"原地复用" Run —— 旧 Run 标 `failed`（保留 pid/started_at/分支信息），新 Run 用 `attempt+1` 重新建。状态机更简单，`runs.jsonc` 自带历史。
- `next_retry_at` 决定本轮 poll 是否到期；未到期直接跳过，不浪费 dispatch 配额。
- `assert_gh_auth` 失败的 Run **不累加 attempt**（5.3 中特殊处理），用固定 5 分钟重试，等用户重登。
- 重试上限到了 → `mark_failed` + **保留 worktree**（用户可手动接管或排查）。

---

## 6. 部署方式

### 6.1 systemd 守护

```ini
# ~/.config/systemd/user/symphony-oc.service
[Unit]
Description=OpenCode Symphony Orchestrator
After=network.target

[Service]
ExecStart=/path/to/symphony-oc/orchestrator.py
Restart=on-failure
RestartSec=5
Environment=HOME=%h
Environment=PATH=/path/to/venv/bin:%h/.cargo/bin:/usr/bin

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable symphony-oc.service
systemctl --user start symphony-oc.service

# 让 user service 在用户未登录时也常驻（真守护进程必需）
loginctl enable-linger $USER
```

### 6.2 前置条件

- `opencode` ≥ **1.17.7** 已安装（`opencode --version` 验证；permission schema 随版本变化）
- `gh` CLI 已认证（`gh auth login`）
- Python 3.11+，依赖：`jinja2`（prompt 模板）、`json5`（解析 `.jsonc` 状态文件）
- `symphony-worker` agent 已安装（由 bootstrap 自动写入 `~/.config/opencode/agents/`，见 6.3）
- 项目仓库已初始化 git，且 `upstream` remote 已配置（`git remote -v` 验证）
- 本地 `issues/` 目录或 GitHub Token 配置

### 6.3 Bootstrap 脚本（agent 安装 + 首次自检）

`bootstrap.py` 在 orchestrator 启动前运行一次；也可手动执行 `python bootstrap.py`。任一检查失败 → 非 0 退出，**orchestrator 拒绝启动**（避免 silently 跑全权限 agent，见 9.1 #4）。

```python
# bootstrap.py
"""Pre-flight checks + agent install. Idempotent. Safe to re-run."""

import hashlib, shutil, subprocess, sys
from pathlib import Path

MIN_OPENCODE_VERSION = (1, 17, 7)
AGENT_NAME = "symphony-worker"
AGENT_INSTALL_DIR = Path.home() / ".config/opencode/agents"
AGENT_INSTALL_PATH = AGENT_INSTALL_DIR / f"{AGENT_NAME}.md"
BUNDLED_AGENT = Path(__file__).parent / "agents/symphony-worker.md"
REPO_ROOT = Path(__file__).parent


class BootError(RuntimeError):
    """Pre-flight failure. Orchestrator must not start."""


def main() -> int:
    checks = [
        check_opencode_version,
        check_external_tools,         # gh, git
        check_git_remote,             # upstream configured
        install_agent,                # copy bundled symphony-worker.md
        verify_agent_discoverable,    # opencode agent list 包含
        init_workspace,               # state/ log/ worktrees/ issues/
        smoke_test_agent,             # 2s 启动探测，捕捉 silent fall-back
    ]
    for check in checks:
        try:
            check()
            print(f"  ✓ {check.__name__}")
        except BootError as e:
            print(f"  ✗ {check.__name__}: {e}", file=sys.stderr)
            return 1
    print("bootstrap complete")
    return 0


def check_opencode_version():
    out = subprocess.run(["opencode", "--version"],
                         capture_output=True, text=True, check=True)
    version_str = out.stdout.strip().split()[-1]            # "opencode 1.17.7" → "1.17.7"
    parts = tuple(int(x) for x in version_str.split("."))
    if parts < MIN_OPENCODE_VERSION:
        raise BootError(f"opencode {version_str} < required {'.'.join(map(str, MIN_OPENCODE_VERSION))}")


def check_external_tools():
    for tool in ("gh", "git"):
        if not shutil.which(tool):
            raise BootError(f"{tool} not in PATH")


def check_git_remote():
    out = subprocess.run(["git", "remote"], capture_output=True,
                         text=True, check=True, cwd=REPO_ROOT)
    if "upstream" not in out.stdout.split():
        raise BootError("git remote 'upstream' missing — run: git remote add upstream <url>")


def install_agent():
    """Copy bundled agents/symphony-worker.md → ~/.config/opencode/agents/.
    Skip if installed copy's sha256 matches bundled file (idempotent)."""
    bundled_hash = hashlib.sha256(BUNDLED_AGENT.read_bytes()).hexdigest()

    AGENT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if AGENT_INSTALL_PATH.exists():
        installed_hash = hashlib.sha256(AGENT_INSTALL_PATH.read_bytes()).hexdigest()
        if installed_hash == bundled_hash:
            return                                  # 已是最新
    AGENT_INSTALL_PATH.write_bytes(BUNDLED_AGENT.read_bytes())


def verify_agent_discoverable():
    """关键检查 — opencode 找不到 agent 会 silently fall back 到全权限 default。
    若这一步失败，直接拒绝启动，不允许进入主循环。"""
    out = subprocess.run(["opencode", "agent", "list"],
                         capture_output=True, text=True, check=True)
    if AGENT_NAME not in out.stdout:
        raise BootError(
            f"agent '{AGENT_NAME}' not in `opencode agent list` output. "
            "Permission boundary would be bypassed — refusing to start."
        )


def init_workspace():
    for d, ignore in [("state", "*\n!.gitignore\n"),
                      ("log", "*\n!.gitignore\n"),
                      ("worktrees", "*\n!.gitignore\n"),
                      ("issues", "*.prompt\n!.gitignore\n")]:
        path = REPO_ROOT / d
        path.mkdir(exist_ok=True)
        gi = path / ".gitignore"
        if not gi.exists():
            gi.write_text(ignore)

    runs = REPO_ROOT / "state/runs.jsonc"
    if not runs.exists():
        runs.write_text('{\n  "runs": [],\n  "last_poll": null\n}\n')


def smoke_test_agent():
    """启动 opencode 2s，扫描输出里是否出现 'not found' / 'Falling back'。
    这是捕捉 silent fall-back 的最后一道闸（agent list 可能在某些 edge case 漏掉）。"""
    proc = subprocess.Popen(
        ["opencode", "run", "--agent", AGENT_NAME,
         "--dir", "/tmp", "--format", "json", "exit immediately"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    text = out.decode("utf-8", errors="replace").lower()
    if "not found" in text or "falling back" in text:
        raise BootError(
            f"smoke test detected agent fall-back. Output:\n{text[:500]}"
        )


if __name__ == "__main__":
    sys.exit(main())
```

**Orchestrator 集成：**

```python
# orchestrator.py 启动入口
def main():
    if not bootstrap_ok():
        log.error("bootstrap failed; refusing to start. Run `python bootstrap.py`.")
        sys.exit(2)
    # 进入主循环
```

**手动修复流程：**

```bash
$ python bootstrap.py
  ✓ check_opencode_version
  ✗ verify_agent_discoverable: agent 'symphony-worker' not in `opencode agent list` output.
# 用户排查 → 修好后再跑
$ python bootstrap.py
  ✓ check_opencode_version
  ✓ ...
bootstrap complete
```

---

## 7. 系统级验证

> 业务级 E2E 场景（"故意写坏代码"、"模拟卡死"等）依赖具体目标仓库，留到接入真实项目时再写。本节只列**编排器自身行为**可验证的属性。

### 7.1 自检（bootstrap 阶段，见 6.3）

- `opencode --version` ≥ 1.17.7
- `git remote` 含 `upstream`
- `~/.config/opencode/agents/symphony-worker.md` 存在且 hash 匹配 bundled 模板
- `opencode agent list` 输出包含 `symphony-worker`
- 2s smoke test 未检测到 "Falling back" / "not found"
- `state/ log/ worktrees/ issues/` 目录就位，`state/runs.jsonc` 初始化

### 7.2 状态可观测（运行时）

- `state/runs.jsonc` 单一 list，每轮 poll 后原子写入；`cat state/runs.jsonc | jq '.runs[] | {issue_id, status}'` 实时查看
- `log/{issue_id}.log` 含 opencode 子进程的 stdout/stderr 全量输出
- `git worktree list` 显示当前活跃的 Run 对应的 worktree（应 ≤ `max_concurrent`）

### 7.3 编排器不变量（unit test 友好）

不依赖业务代码即可验证：

- `should_dispatch()` 对相同 `content_hash` 的本地 issue 不重跑（5.7）
- `can_dispatch()` 在 N 个 running 时拒绝第 N+1 个（5.4）
- `retry_delay(attempt)` 单调递增、不超过 `MAX_RETRY_BACKOFF_MS`（5.6）
- `save_runs_atomic()` 写中途中断不破坏现有文件（tmp + rename）
- `cleanup_worktree()` 在 PR 创建后调用，失败路径不调用

---

## 8. 后续扩展（不在 MVP 范围内）

- [x] **多轮审查循环**（已实现，见 5.4）
- [x] **CI 监控与自动修复**（已实现，见 5.5）
- [ ] 多 Issue 并行冲突检测（两个 Issue 改了同一文件）
- [ ] PR 自动 merge（用户确认后）
- [ ] Web Dashboard（类似 Symphony 的 Phoenix 界面）
- [ ] 支持更多 Issue 源（GitLab、Jira）
- [ ] 增量上下文管理（Agent 轮次过多时自动压缩）
- [ ] 多仓库支持（一个 orchestrator 管理多个 repo）
- [ ] Slack/Discord/飞书通知（PR 创建/失败通知）

---

## 9. 风险与约束

### 9.1 已知风险

1. **OpenCode 版本兼容性** — 已验证 1.17.7 的 CLI 与 permission schema；如升级到不兼容版本（如重命名 `--agent` 或调整 permission category），需重跑 smoke test。锁定方式：在 README 标注 `opencode == 1.17.7`
2. **git worktree 并发局限** — 如果两个 Issue 改了同一模块，合并 PR 时会冲突，MVP 阶段不做冲突检测
3. **Agent 失控** — 靠 `doom_loop: deny` + `stall_timeout_ms`（wall clock）双闸；若 opencode 升级后 `doom_loop` 语义变化，wall clock 仍是兜底
4. **Agent 发现失败 / silent fall-back** — `opencode run --agent X` 找不到 agent 时会 "fall back to default"（已实测），silently 跑全权限 agent，permission 边界形同虚设。**三层防御（见 6.3）：**
    - **L1 启动前**：`verify_agent_discoverable()` 调 `opencode agent list` 确认 `symphony-worker` 在内；不在则 orchestrator 拒绝启动
    - **L2 启动时**：`smoke_test_agent()` 实跑 2s 扫描输出，捕捉 `agent list` 可能漏掉的 edge case
    - **L3 安装时**：`install_agent()` 写文件后立即被 L1 验证；hash 不匹配则覆盖重装
    - 残余风险：orchestrator 长期运行后 agent 文件被外部删除/修改 → 下一轮 dispatch 才暴露。MVP 不做 per-dispatch 检查（开销 ~500ms）；如观察到可加 `verify_agent_discoverable()` 到主循环每 N 轮一次
5. **`gh` CLI token 过期（review #3）** — bootstrap 只检查一次 `gh auth status`，token 可能在长时间运行后过期，下次 `gh pr create` 静默失败、Run 卡在 reconcile。**缓解（已实现，见 5.3）：**
    - reconcile 创建 PR 前 `assert_gh_auth()` 复检
    - 过期 → 抛 `GhAuthExpired` → Run 进 `retrying`，**不累加 attempt**，5 分钟后再试
    - worktree 保留（不删），用户 `gh auth login` 后下一轮自动续上
    - 可选增强（不在 MVP）：主循环每 N 轮 poll 一次 `gh auth status` 提前告警

### 9.2 约束

- 仅支持 Unix-like 系统（Linux/macOS）
- 依赖 `opencode`、`gh`、`git` 三个外部工具
- 不支持 Windows
- 每个 Run 创建一个 git worktree，磁盘占用随并发数线性增长（默认 3 个）
- 假设 `upstream` remote 已配置（CLAUDE.md 工作流要求）
- `symphony-worker` agent 必须安装到 `~/.config/opencode/agents/`（全局，非项目内）
