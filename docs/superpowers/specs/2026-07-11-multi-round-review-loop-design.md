# 多轮审查循环 — 设计文档

> **Status:** Approved (2026-07-11)
> **Scope:** OpenCode Symphony — 实现"实现 agent → 审查 agent → 修复 agent → 再审查"的多轮审查循环
> **Out of scope:** CI 监控（PR 后轮询 `gh pr checks`）— 后续独立 spec

## 1. 背景与目标

设计文档（`docs/design/2026-06-14-opencode-symphony-design.md`）Section 5.4 描述了多轮审查循环状态机，但当前代码（`symphony_oc/orchestrator.py:153 process_completed()`）在 implementer 完成后直接调用 `reconcile()`，跳过了审查环节。

本设计实现审查循环：implementer 完成 → reviewer agent 审查 → fixer 修复 → reviewer 再审查 → 至少 3 轮 PASS → reconcile → PR。

**目标：**
- 引入 `reviewing` 状态与状态机路由
- 新增 `reviewer.py` 模块与 `agents/symphony-reviewer.md` agent
- 记录全量审查历史供人工复盘（不完全信任 agent 判断）
- 失败兜底：reviewer 自身坏的场合必须有界，不能无限循环

## 2. 状态机

```
                        ┌─────────────────────────────┐
                        │                             │
                        ▼                             │
queued ──► running ──► reviewing ──┬── PASS + N≥3 ──► reconcile ──► succeeded
   (worker =          (reviewer     │
    implementer        agent)       ├── PASS + N<3 ──► reviewing (再审查)
    或 fixer)                        │
                                      ├── FAIL + N<5 ──► running (fixer agent)
                                      │
                                      └── FAIL + N≥5 ──► failed

任何阶段异常 → retrying → （退避后）重投
```

`running` 状态被 implementer 和 fixer 复用（设计文档明确允许），统一称"worker"。区分依据：
- `review_count == 0` → implementer 首次执行
- `review_count > 0` → fixer 在执行

> **命名约定：** orchestrator 中处理 `running` 完成的函数命名为 `_on_worker_done`（不区分 implementer/fixer），处理 `reviewing` 完成的命名为 `_on_reviewer_done`。

## 3. 数据模型变更（`state.py`）

### 3.1 新增 `ReviewRecord` 数据类

```python
@dataclass
class ReviewRecord:
    """单轮审查的完整记录 — 用于人工复盘。"""
    iteration: int                              # 1-indexed 审查轮次
    verdict: str                                # "PASS" | "FAIL"
    timestamp: datetime                         # 审查完成时间
    files_affected: list[str]                   # JSON 中的受影响文件列表
    summary: str                                # JSON 中的概述
    feedback: list[dict]                        # JSON 中的结构化问题列表
    reviewer_pid: Optional[int] = None
    reviewer_started_at: Optional[datetime] = None
    reviewer_finished_at: Optional[datetime] = None
    review_file: Optional[str] = None           # .san/review/review-{N}.json 路径
```

### 3.2 `Run` 新增字段

```python
@dataclass
class Run:
    # ... 现有字段 ...
    review_count: int = 0                       # = len(review_history)，冗余便于查询
    review_passed: bool = False                 # 最新一次 verdict，冗余
    review_feedback: Optional[str] = None       # 最新反馈文本（fixer prompt 用），冗余
    review_history: list[ReviewRecord] = field(default_factory=list)  # 全量审查记录
```

**冗余字段理由：** 路由决策（`if review_count >= 3`）比从 history 派生更直观；fixer prompt 直接用 `review_feedback`，避免每次格式化历史。派生逻辑放在 `_on_reviewer_done()` 里：解析完后同时 `review_history.append(rec)` 并更新三个冗余字段。

### 3.3 状态字符串新增

`"reviewing"`。**不**新增 `"reconciling"` / `"ci_monitoring"`（后者属 CI 监控，不在本次范围）。

### 3.4 JSONC 向后兼容

旧 `state/runs.jsonc` 不含新字段。dataclass 默认值机制使 `Run(**d)` 对缺失字段自动用默认值（`review_count=0`、`review_history=[]` 等），向后兼容自动满足。

但现有 `_dict_to_run()` / `_run_to_dict()` 不处理嵌套数据类。需补充：

```python
def _dict_to_run(d: dict) -> Run:
    for key in ['started_at', 'finished_at', 'next_retry_at']:
        if key in d and d[key]:
            d[key] = datetime.fromisoformat(d[key])
    if 'review_history' in d and d['review_history']:
        d['review_history'] = [_dict_to_review_record(r) for r in d['review_history']]
    return Run(**d)


def _dict_to_review_record(d: dict) -> ReviewRecord:
    for key in ['timestamp', 'reviewer_started_at', 'reviewer_finished_at']:
        if key in d and d[key]:
            d[key] = datetime.fromisoformat(d[key])
    return ReviewRecord(**d)


def _run_to_dict(run: Run) -> dict:
    """Serialize Run to dict for JSON.

    Note: dataclasses.asdict() recursively converts nested dataclasses to dicts,
    so review_history (list[ReviewRecord]) becomes list[dict] automatically.
    The subsequent loop then converts datetime fields inside those dicts to
    ISO strings. This relies on asdict's recursive behavior — if ReviewRecord
    ever nests another dataclass, that nested dataclass would also be flattened
    to dict, which is fine for JSON but worth knowing.
    """
    d = asdict(run)
    for key, value in d.items():
        if isinstance(value, datetime):
            d[key] = value.isoformat()
    # review_history 内部的 datetime 也要转（asdict 已把 ReviewRecord 展平为 dict）
    for rec in d.get('review_history', []):
        for key in ['timestamp', 'reviewer_started_at', 'reviewer_finished_at']:
            if rec.get(key) and isinstance(rec[key], datetime):
                rec[key] = rec[key].isoformat()
    return d
```

## 4. Reviewer Agent（`agents/symphony-reviewer.md`）

### 4.1 Agent 文件

```markdown
---
description: Symphony Reviewer — 只读代码审查 agent，输出结构化 JSON 报告
mode: primary
permission:
  webfetch: deny
  websearch: deny
  task: deny
  todowrite: deny
  lsp: deny
  skill: deny
  read: allow
  edit:
    ".san/review/*": allow
    "*": deny
  glob: allow
  grep: allow
  bash:
    "*": deny
    "git status": allow
    "git diff *": allow
    "git log *": allow
    "git show *": allow
  external_directory: deny
  doom_loop: deny
---

你是 Symphony Reviewer Agent。你的任务是审查当前 worktree 内未合并到 base 的代码改动，
输出结构化 JSON 报告。

## 工作流程

1. 执行 `git log upstream/main..HEAD --oneline` 查看本次改动列表
2. 执行 `git diff upstream/main..HEAD` 审查完整 diff
3. 针对每处改动评估：
   - 正确性（逻辑错误、边界条件、异常处理）
   - 安全性（注入、权限、敏感信息泄露）
   - 可维护性（命名、复杂度、注释）
   - 与现有代码风格一致性
4. 如有前一轮审查反馈（在 prompt 中给出），核对是否已修复

## 输出规范

严格按照以下 JSON schema 输出到 `.san/review/review-{N}.json`（{N} 在 prompt 中给出）：

{
  "verdict": "PASS" | "FAIL",
  "iteration": <int>,
  "timestamp": "<ISO 8601 datetime>",
  "files_affected": ["<file path>", ...],
  "summary": "<一句话概述本次审查结论>",
  "feedback": [
    {
      "file": "<file path>",
      "line": <int or null>,
      "severity": "critical" | "major" | "minor" | "style",
      "issue": "<问题描述>",
      "suggestion": "<修复建议>"
    }
  ]
}

## 关键约束

- 必须输出合法 JSON（不要 markdown 代码块包裹，不要尾随逗号）
- PASS 表示代码可以进入 reconcile（CI + PR）；任何 critical/major 问题都必须 FAIL
- 审查完成后立即退出，不要进入交互模式
- 不要修改除 `.san/review/*.json` 外的任何文件
```

### 4.2 输出文件位置

`{worktree}/.san/review/review-{N}.json`（N 为审查轮次，从 1 起）。

### 4.3 模型选择

- **Reviewer**: 强模型（用户在 WORKFLOW.md 通过 `agent.reviewer.extra_args` 显式指定 `--model`）
- **Fixer / Implementer**: 复用 `symphony-worker`（便宜模型）

## 5. 新模块 `reviewer.py`

### 5.1 公开接口

```python
@dataclass
class ReviewResult:
    passed: bool                           # verdict == "PASS"
    feedback_text: str                     # 格式化给 fixer 看的反馈（多行文本）
    record: ReviewRecord                   # 完整记录，准备 append 到 run.review_history
    raw_json: dict                         # 原始解析后的 JSON，调试用


def dispatch_review(run: Run, cfg, base_ref: str, previous_feedback: Optional[str]) -> None:
    """启动 reviewer agent，将 run 状态置为 reviewing。
    
    生成 prompt 文件（含 issue 上下文 + 上轮反馈 + diff 范围），
    调 agent_runner._spawn_agent 启动 symphony-reviewer。
    修改 run in-place: status='reviewing', pid, started_at, error=None。
    异常抛出由调用方处理（orchestrator 中 mark_failed）。
    """


def dispatch_fix(run: Run, cfg, review_feedback: str) -> None:
    """启动 fixer agent（symphony-worker），将 run 状态置为 running。
    
    生成 fix prompt（含 issue 上下文 + 审查反馈），调 agent_runner._spawn_agent。
    修改 run in-place: status='running', pid, started_at, error=None。
    Fixer 完成后 review_count 不变（fix 不算一轮审查）。
    """


def parse_review_result(wt_path: str, iteration: int) -> ReviewResult:
    """读取 .san/review/review-{iteration}.json，验证并返回 ReviewResult。
    
    失败情况一律返回 ReviewResult(passed=False, ...)，不抛异常：
    - 文件不存在 → feedback="reviewer did not produce report"
    - JSON parse 失败 → feedback="malformed JSON: ..."，保留原文件
    - verdict 缺失/非法 → feedback="verdict invalid"
    
    这些"审查自身失败"都计入 review_count（防止无限循环）。
    """
```

### 5.2 内部 helper

- `_build_review_prompt(run, base_ref, previous_feedback, iteration) -> str` — Jinja2 渲染
- `_build_fix_prompt(run, feedback_text) -> str` — Jinja2 渲染
- `_format_feedback_text(items, summary) -> str` — JSON feedback 数组 → 多行文本（格式见下）
- `_malformed_result(iteration, msg) -> ReviewResult` — 构造 FAIL fallback

#### `_format_feedback_text` 输出格式

按 severity（critical → major → minor → style）排序，按文件分组：

```
## 审查反馈摘要
<summary 字段内容>

## 问题列表

### foo.py:42 [critical]
问题：except 子句过宽，会吞掉 KeyboardInterrupt
建议：拆分为 except (subprocess.CalledProcessError, OSError) 单独处理

### bar.py:7 [major]
问题：未处理磁盘满场景
建议：os.replace 失败时 fallback 到保留 tmp 文件

### baz.py:100 [minor]
问题：变量名 `d` 不够语义化
建议：改为 `run_dict`

### qux.py:200 [style]
问题：缺少尾随逗号
建议：补充尾随逗号便于 diff
```

无 feedback items 时返回 `## 审查反馈摘要\n<summary>\n\n## 问题列表\n（无具体问题，但 verdict=FAIL）`。

### 5.3 Fixer 复用 `symphony-worker`

不新建 fixer agent。fixer 是 `symphony-worker` + 不同 prompt（带审查反馈）。同一个 agent 文件，不同的 prompt 文件。

**命名约定（reviewer 与 fixer 的 prompt/log 文件 N 都对应触发的 review 轮次）：**

| 文件类型 | 路径 | N 含义 |
|---------|------|--------|
| Reviewer prompt | `.san/review/review-{N}.prompt` | 即将执行的审查轮次（review_count + 1） |
| Reviewer 输出 | `.san/review/review-{N}.json` | 已完成的审查轮次 |
| Reviewer log | `log/{issue_id}.review-{N}.log` | 已完成的审查轮次 |
| Fixer prompt | `.san/skills/review-{N}-fix.md` | 触发此修复的 FAIL 审查轮次 |
| Fixer log | `log/{issue_id}.review-{N}-fix.log` | 触发此修复的 FAIL 审查轮次 |

`dispatch_fix` 内部取 `triggering_iter = run.review_count`（即刚 FAIL 的那轮），用于命名 prompt/log。这样 `review-2-fix.log` 一眼能看出是"第 2 轮审查后的修复"。

## 6. `_spawn_agent` 抽到新模块 `agent_runner.py`

### 6.1 动机

`executor.py` 已含 `should_dispatch` / `can_dispatch` / `slugify` / `dispatch`，职责混杂（决策 + 子进程机械启动）。抽出 `_spawn_agent` 让 executor 专注 dispatch 工作流，agent_runner 专注子进程启动。

### 6.2 接口

```python
# agent_runner.py

def _spawn_agent(agent: str, wt_path: str, extra_args: list[str],
                 prompt_path: str, log_path: str) -> subprocess.Popen:
    """启动 opencode run 子进程，返回 Popen 对象（调用方取 .pid）。
    
    - start_new_session=True（独立 process group，便于 SIGKILL 整组）
    - stdout 重定向到 log_path
    - 不传 --dangerously-skip-permissions
    """
    cmd = ["opencode", "run", "--agent", agent, "--dir", wt_path, *extra_args, prompt_path]
    return subprocess.Popen(
        cmd, start_new_session=True,
        stdout=open(log_path, "wb"), stderr=subprocess.STDOUT,
    )
```

`executor.dispatch()` 改为内部调用 `_spawn_agent(...)`，行为不变。

## 7. Orchestrator 路由改造（`orchestrator.py`）

### 7.1 `process_completed()` 改造

```python
def process_completed(runs: list[Run], cfg) -> None:
    """检测 agent 退出（pid dead）并路由到下一阶段。"""
    for run in runs:
        if _pid_exists_simple(run.pid):
            continue  # agent 还在跑
        
        if run.status == "running":
            _on_worker_done(run, cfg)
        elif run.status == "reviewing":
            _on_reviewer_done(run, cfg)
```

### 7.2 `_on_worker_done(run, cfg)`

implementer 或 fixer 退出（`running` 状态） → 启动 reviewer。

```python
def _on_worker_done(run: Run, cfg) -> None:
    """Worker agent (implementer 或 fixer) 完成 → dispatch reviewer.
    
    不区分是 implementer 还是 fixer：两者完成后下一步都是审查。
    review_count == 0 表示首次 implementer 完成；> 0 表示 fixer 完成。
    """
    try:
        dispatch_review(run, cfg, cfg.git.base_branch, run.review_feedback)
    except Exception as e:
        logger.exception("dispatch_review failed for %s: %s", run.issue_id, e)
        mark_failed(run, f"dispatch_review error: {e}")
```

### 7.3 `_on_reviewer_done(run, cfg)` — 决策表

```python
def _on_reviewer_done(run: Run, cfg) -> None:
    iteration = run.review_count + 1
    try:
        result = parse_review_result(run.worktree, iteration)
    except Exception as e:
        # parse_review_result 内部已兜底常见失败，这里是保险
        logger.exception("parse_review_result crashed for %s: %s", run.issue_id, e)
        mark_failed(run, f"parse_review_result crash: {e}")
        return
    
    run.review_history.append(result.record)
    run.review_count = iteration
    run.review_passed = result.passed
    run.review_feedback = result.feedback_text
    
    min_iter = cfg.agent.reviewer.min_iterations   # 默认 3
    max_iter = cfg.agent.reviewer.max_iterations   # 默认 5
    
    if result.passed and iteration >= min_iter:
        logger.info("%s: review PASS iter=%d >= min=%d → reconcile",
                    run.issue_id, iteration, min_iter)
        try:
            reconcile(run, cfg)
        except Exception as e:
            logger.exception("%s: reconcile failed, scheduling retry", run.issue_id)
            schedule_retry(run, f"reconcile error: {e}",
                           backoff_ms=retry_delay(run.attempt))
    elif result.passed:  # iteration < min_iter
        logger.info("%s: review PASS iter=%d < min=%d → re-review for consistency",
                    run.issue_id, iteration, min_iter)
        try:
            dispatch_review(run, cfg, cfg.git.base_branch, run.review_feedback)
        except Exception as e:
            logger.exception("%s: re-review dispatch failed", run.issue_id)
            mark_failed(run, f"re-review dispatch error: {e}")
    elif iteration < max_iter:  # FAIL
        logger.info("%s: review FAIL iter=%d < max=%d → dispatch fixer",
                    run.issue_id, iteration, max_iter)
        try:
            dispatch_fix(run, cfg, run.review_feedback)
        except Exception as e:
            logger.exception("%s: dispatch_fix failed", run.issue_id)
            mark_failed(run, f"dispatch_fix error: {e}")
    else:  # FAIL + iteration >= max_iter
        logger.info("%s: review FAIL iter=%d >= max=%d → mark_failed",
                    run.issue_id, iteration, max_iter)
        mark_failed(run, f"review failed after {iteration} iterations: "
                          f"{result.feedback_text[:300]}")
```

### 7.4 决策表汇总

| 条件 | 动作 |
|------|------|
| PASS + N ≥ min_iter(3) | `reconcile()` → succeeded / retrying |
| PASS + N < min_iter(3) | `dispatch_review()` → reviewing（再审查） |
| FAIL + N < max_iter(5) | `dispatch_fix()` → running |
| FAIL + N ≥ max_iter(5) | `mark_failed()` |

### 7.5 主循环改动

`main_loop()` 不变 — 沿用现有 `save_run_atomic(state_path, runs)` 持久化。

## 8. 配置变更（`config.py` + `WORKFLOW.md`）

### 8.1 新增 `ReviewerConfig`

```python
@dataclass
class ReviewerConfig:
    name: str = "symphony-reviewer"
    min_iterations: int = 3       # PASS 也必须满 3 轮（一致性检查）
    max_iterations: int = 5       # FAIL 超 5 轮放弃
    extra_args: list[str] = field(default_factory=list)  # 用户在 WORKFLOW.md 配置


@dataclass
class AgentConfig:
    # ... 现有字段 ...
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)   # 嵌套
```

### 8.2 `_dict_to_config()` 解析

```python
if "agent" in d:
    a = d["agent"]
    cfg.agent = AgentConfig(**{k: a.get(k, getattr(cfg.agent, k)) 
                              for k in ["name", "max_concurrent", "stall_timeout_ms",
                                        "max_retries", "retry_backoff_ms", "extra_args"]})
    if "reviewer" in a:
        r = a["reviewer"]
        cfg.agent.reviewer = ReviewerConfig(**{
            k: r.get(k, getattr(cfg.agent.reviewer, k))
            for k in ["name", "min_iterations", "max_iterations", "extra_args"]
        })
```

### 8.3 WORKFLOW.md 新增 `reviewer` 子节点

```yaml
agent:
  name: "symphony-worker"
  # ...
  reviewer:
    name: "symphony-reviewer"
    min_iterations: 3
    max_iterations: 5
    # extra_args: 指定审查用的强模型，例如：
    #   ["--model", "bigmodel/coding"]       # GLM 5.1/5.2
    #   ["--model", "anthropic/claude-opus"] # Claude Opus
    # 留空则用 opencode 默认模型（不推荐 — 审查应用强模型）
    extra_args: []
```

旧 WORKFLOW.md 没有 `reviewer` 节点也能跑 — `ReviewerConfig` 有默认值。

### 8.4 `bootstrap.py` 新增 `check_reviewer_model()`

`check_reviewer_model()` 做两件事：

1. **硬校验（失败则 BootError）**：`assert cfg.agent.reviewer.min_iterations <= cfg.agent.reviewer.max_iterations`。否则 `_on_reviewer_done` 决策表会陷入悖论（PASS + N≥min 永不满足，因为 N永远到不了 min），用户配置错误应 fail-fast。

2. **Soft warning（不阻止启动）**：扫 `~/.config/opencode/opencode.jsonc`，若 `cfg.agent.reviewer.extra_args` 不含 `--model`，列出可用 provider/model 并建议补到 WORKFLOW.md。

```python
def check_reviewer_model() -> None:
    """Validate reviewer config + warn about missing --model."""
    from symphony_oc.config import load_config
    cfg = load_config(REPO_ROOT / "WORKFLOW.md")
    
    # 硬校验
    if cfg.agent.reviewer.min_iterations > cfg.agent.reviewer.max_iterations:
        raise BootError(
            f"agent.reviewer.min_iterations ({cfg.agent.reviewer.min_iterations}) "
            f"> max_iterations ({cfg.agent.reviewer.max_iterations}). "
            f"决策表无法收敛 — 请调整 WORKFLOW.md。"
        )
    
    # Soft warning
    args = cfg.agent.reviewer.extra_args
    if any(a == "--model" for i, a in enumerate(args) for _ in [0] if i + 1 < len(args)):
        return  # 已显式指定 --model <value>
    if "--model" in args:
        return
    
    available = _list_opencode_models()
    if available:
        print(
            f"  ⚠ reviewer extra_args 未指定 --model。可用 provider/model: "
            f"{', '.join(available[:5])}{' ...' if len(available) > 5 else ''}\n"
            f"    建议在 WORKFLOW.md 的 agent.reviewer.extra_args 加 "
            f"[\"--model\", \"<strong-model>\"]"
        )
    else:
        print(
            "  ⚠ reviewer extra_args 未指定 --model，且未在 ~/.config/opencode/opencode.jsonc "
            "找到已配置 provider。审查将用 opencode 默认模型（可能偏弱）。"
        )
```

> **注：** `--model` 验证仅检查 flag 是否存在，不验证 model 名是否在 opencode 中可用（opencode 自身会在启动 agent 时校验，失败会立即 exit non-zero，orchestrator 下一轮 poll 会发现 pid dead 走 mark_failed 路径）。

## 9. 错误处理汇总

| 错误源 | 触发条件 | 处理 | 计入 review_count? |
|--------|---------|------|-------------------|
| `dispatch_review` 异常 | worktree 不存在/opencode 启动失败 | `mark_failed` | 否（reviewer 没启动） |
| `dispatch_fix` 异常 | 同上 | `mark_failed` | 否 |
| `parse_review_result` 未捕获异常 | OSError 等罕见情况 | `mark_failed` | 否 |
| Reviewer 未写文件 | pid dead 但 `.san/review/review-N.json` 不存在 | 内部 fallback 为 FAIL | ✅ 是（防止无限循环） |
| JSON parse 失败 | 文件存在但格式错 | 内部 fallback 为 FAIL；保留原文件供人工查看 | ✅ 是 |
| `verdict` 字段非法 | 如 `"MAYBE"` | 内部 fallback 为 FAIL | ✅ 是 |
| `reconcile` 异常 | CI flaky / gh rate limit | `schedule_retry`（保留 worktree） | N/A |
| Reviewer 输出但所有反馈都是 minor | verdict=PASS | 视为 PASS（信任 reviewer 判断） | ✅ 是 |

**设计原则：** reviewer 自身坏的场合（写坏 JSON、未写文件、verdict 非法）一律计入 `review_count`，让 `max_iterations` 兜底 — 5 轮全坏 → `mark_failed` → 人工介入。dispatch 启动失败的异常**不**计入（reviewer 没真正运行），直接 `mark_failed` 避免重试一个永久坏掉的 agent。

**已知限制（非本 spec 引入）：** `subprocess.Popen` 启动成功后立即返回，但 agent 进程可能在数毫秒内 crash（如 `--model` 写错导致 opencode 立即 exit）。这种"启动了但立刻死"的情况要等下一轮 poll（默认 30 秒）才能通过 `_pid_exists_simple` 发现。这是现有架构的固有限制，所有状态都受影响（不限于审查循环）。缓解：bootstrap 的 `smoke_test_agent` 已检测 silent fall-back；reviewer 的 `--model` 错误也会在 smoke 阶段暴露。

## 10. 测试策略

### 10.1 单元测试矩阵（mock subprocess，不跑真 opencode）

| 测试文件 | 覆盖 |
|---------|------|
| `tests/test_state.py` (扩展) | `Run` 含新字段 round-trip JSONC；旧 JSONC 不含新字段时用默认值；`review_history` 嵌套 datetime 序列化往返 |
| `tests/test_reviewer.py` (新) | `parse_review_result` 5 路径：valid PASS / valid FAIL / 文件缺失 / JSON parse 失败 / verdict 非法；`_format_feedback_text` 按 severity 排序 + 按文件分组 |
| `tests/test_orchestrator.py` (扩展) | `_on_reviewer_done` 决策表 4 分支（PASS+N≥3 / PASS+N<3 / FAIL+N<5 / FAIL+N≥5）；**reconcile 异常 → schedule_retry 分支**；`_on_worker_done` dispatch_review 成功/失败；`process_completed` 路由（status=running / status=reviewing） |
| `tests/test_agent_runner.py` (新) | `_spawn_agent`（mock `subprocess.Popen`）参数构造、log 路径、start_new_session=True |
| `tests/test_executor.py` (扩展) | `dispatch()` 改为调 `_spawn_agent` 后行为不变 |
| `tests/test_config.py` (扩展) | `_dict_to_config` 解析 `agent.reviewer` 子节点；**缺 `reviewer` 节点用默认值**；**部分字段（只给 min_iterations 不给 max_iterations）**；**完整 reviewer 节点** |
| `tests/test_bootstrap.py` (扩展) | `check_reviewer_model`：min > max 抛 BootError；有 `--model` 静默；无 `--model` 有 provider 警告；无 `--model` 无 provider 警告 |

### 10.2 集成测试场景（mock subprocess）

| 场景 | 步骤 |
|------|------|
| 正常 3 轮 PASS | implementer → reviewer(PASS,N=1) → reviewer(PASS,N=2) → reviewer(PASS,N=3) → reconcile(succeeded) |
| 中间 FAIL 后修复 | implementer → reviewer(FAIL,N=1) → fixer → reviewer(FAIL,N=2) → fixer → reviewer(PASS,N=3) → reconcile |
| 5 轮全 FAIL | implementer → reviewer(FAIL×5) → mark_failed |
| Reviewer 写坏 JSON | implementer → reviewer(坏 JSON,N=1) → ... → N=5 mark_failed |
| dispatch_review 抛异常 | implementer 完成 → dispatch_review 抛 → mark_failed |
| 旧 state 文件加载 | 写一份不含新字段的 runs.jsonc，load_all 应正常返回带默认值的 Run |

## 11. 观测性

人工复盘入口：
- `state/runs.jsonc` → `runs[].review_history`（结构化，含所有审查轮次）
- `worktrees/{issue_id}/.san/review/review-{N}.json`（reviewer 原始输出）
- `log/{issue_id}.review-{N}.log`（reviewer 子进程 stdout/stderr）
- `log/{issue_id}.fix-{N}.log`（fixer 子进程 stdout/stderr）

`jq` 查询示例：
```bash
# 看所有 Run 的审查轮次分布
jq '.runs[] | {issue_id, status, review_count}' state/runs.jsonc

# 导出某 Run 的完整审查历史
jq '.runs[] | select(.issue_id=="local-001") | .review_history' state/runs.jsonc

# 找所有 FAIL 轮次
jq '.runs[].review_history[] | select(.verdict=="FAIL")' state/runs.jsonc
```

## 12. 明确排除

- ❌ CI 监控（`ci_monitoring` 状态、`ci_monitor.py` 模块、PR 后轮询 `gh pr checks`）— 独立 spec
- ❌ `dispatch_ci_fix`
- ❌ `reconciling` 中间状态（reconcile 内部直接 `mark_succeeded`/`mark_failed`）
- ❌ 多 issue 冲突检测
- ❌ Slack/通知

## 13. 文件变更清单

**新增：**
- `symphony_oc/reviewer.py`
- `symphony_oc/agent_runner.py`
- `agents/symphony-reviewer.md`
- `tests/test_reviewer.py`
- `tests/test_agent_runner.py`

**修改：**
- `symphony_oc/state.py` — `Run` 新字段 + `ReviewRecord` + JSONC 兼容
- `symphony_oc/orchestrator.py` — `process_completed` 路由 + 2 个新 helper
- `symphony_oc/executor.py` — `dispatch()` 改调 `_spawn_agent`
- `symphony_oc/config.py` — `ReviewerConfig` + 解析逻辑
- `symphony_oc/bootstrap.py` — `check_reviewer_model` soft warning
- `WORKFLOW.md` — 新增 `agent.reviewer` 节点（注释示例）
- `tests/test_state.py` — 新字段 round-trip + 旧 JSONC 兼容
- `tests/test_orchestrator.py` — 决策表 + 路由
- `tests/test_executor.py` — `_spawn_agent` 抽出后行为不变
- `tests/test_config.py` — `reviewer` 子节点解析
- `tests/test_bootstrap.py` — `check_reviewer_model`
