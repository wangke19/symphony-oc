# Multi-Round Review Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the multi-round review loop (implementer → reviewer → fixer → reviewer → … → ≥3 PASS → reconcile → PR) in OpenCode Symphony, with full audit history for human post-review.

**Architecture:** Add a `reviewing` status and route completed worker agents to a new reviewer agent. The reviewer emits structured JSON; orchestrator parses it and routes to reconcile (PASS + N≥3), re-review (PASS + N<3), fixer (FAIL + N<5), or mark_failed (FAIL + N≥5). All review iterations are persisted to `state/runs.jsonc` for human post-review.

**Tech Stack:** Python 3.10+, dataclasses, Jinja2, subprocess, pytest, opencode CLI, gh CLI, git worktrees.

## Global Constraints

(Sourced verbatim from `docs/superpowers/specs/2026-07-11-multi-round-review-loop-design.md`.)

- Reviewer agent is **read-only**: bash whitelist is `git status` / `git diff` / `git log` / `git show` only; write only to `.san/review/*`; deny everything else.
- Fixer reuses `symphony-worker` (no new fixer agent) — only the prompt differs.
- `Run.status` vocabulary gains `"reviewing"`; no other new statuses.
- `min_iterations` default 3; `max_iterations` default 5; `min ≤ max` is a hard bootstrap check (`BootError`).
- Reviewer's "self-failures" (no file / malformed JSON / invalid verdict) count toward `review_count` (prevent infinite loop); dispatch failures (reviewer never started) do **not** count (call `mark_failed` directly).
- `ReviewRecord.from_dict` must tolerate unknown JSON keys (LLM emits extras); single source of truth shared by `state._dict_to_run` and `reviewer.parse_review_result`.
- `--model` flag in `agent.reviewer.extra_args` is **not** required (soft warning only); hard fail only on `min > max`.
- No new external dependencies (Jinja2 already used by `executor.py`).
- Two-commit PR rule (from global CLAUDE.md) is handled by existing `reconciler.py` — do **not** touch.

---

## File Structure

**New files:**
- `symphony_oc/agent_runner.py` — `_spawn_agent()` subprocess launcher (extracted from executor).
- `symphony_oc/reviewer.py` — `dispatch_review`, `dispatch_fix`, `parse_review_result`, prompt templates, `_format_feedback_text`.
- `agents/symphony-reviewer.md` — read-only reviewer agent definition.
- `tests/test_agent_runner.py` — `_spawn_agent` unit tests.
- `tests/test_reviewer.py` — `parse_review_result` + `_format_feedback_text` unit tests.

**Modified files:**
- `symphony_oc/state.py` — add `ReviewRecord` dataclass (with `from_dict`), extend `Run` with review fields, update `_dict_to_run` / `_run_to_dict`.
- `symphony_oc/config.py` — add `ReviewerConfig`, nest in `AgentConfig`, extend `_dict_to_config`.
- `symphony_oc/executor.py` — `dispatch()` calls `_spawn_agent` instead of inlining `subprocess.Popen`.
- `symphony_oc/orchestrator.py` — split `process_completed` into `_on_worker_done` + `_on_reviewer_done`, add `"reviewing"` status routing.
- `symphony_oc/bootstrap.py` — add `check_reviewer_model()` to the checks list.
- `WORKFLOW.md` — add `agent.reviewer` node (commented examples).
- `tests/test_state.py` — extend with `ReviewRecord` round-trip + `from_dict` filtering + backward-compat.
- `tests/test_config.py` — extend with `ReviewerConfig` parsing.
- `tests/test_executor.py` — update for `_spawn_agent` refactor (behavior unchanged).
- `tests/test_orchestrator.py` — add `_on_reviewer_done` decision table + `process_completed` routing.
- `tests/test_bootstrap.py` — add `check_reviewer_model` cases.

---

## Task 1: state.py — ReviewRecord dataclass + Run extensions

**Files:**
- Modify: `symphony_oc/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `ReviewRecord` dataclass (fields: `iteration`, `verdict`, `timestamp`, `files_affected`, `summary`, `feedback`, `reviewer_pid`, `reviewer_started_at`, `reviewer_finished_at`, `review_file`); classmethod `ReviewRecord.from_dict(d)`. `Run` gains fields `review_count: int`, `review_passed: bool`, `review_feedback: Optional[str]`, `review_history: list[ReviewRecord]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_state.py`:

```python
class TestReviewRecord:
    def test_from_dict_filters_unknown_keys(self):
        from symphony_oc.state import ReviewRecord
        d = {
            "iteration": 1,
            "verdict": "PASS",
            "timestamp": "2026-07-12T10:00:00",
            "files_affected": ["foo.py"],
            "summary": "OK",
            "feedback": [],
            "confidence": 0.95,         # extra — must be ignored
            "reviewer_name": "claude",  # extra — must be ignored
        }
        rec = ReviewRecord.from_dict(d)
        assert rec.iteration == 1
        assert rec.verdict == "PASS"
        assert rec.timestamp == datetime.fromisoformat("2026-07-12T10:00:00")
        assert rec.reviewer_pid is None  # not in input

    def test_from_dict_parses_nested_datetimes(self):
        from symphony_oc.state import ReviewRecord
        d = {
            "iteration": 2,
            "verdict": "FAIL",
            "timestamp": "2026-07-12T10:00:00",
            "files_affected": [],
            "summary": "bad",
            "feedback": [{"file": "a.py", "line": 1, "severity": "major",
                          "issue": "x", "suggestion": "y"}],
            "reviewer_started_at": "2026-07-12T09:55:00",
            "reviewer_finished_at": "2026-07-12T10:00:00",
        }
        rec = ReviewRecord.from_dict(d)
        assert rec.reviewer_started_at == datetime.fromisoformat("2026-07-12T09:55:00")
        assert rec.reviewer_finished_at == datetime.fromisoformat("2026-07-12T10:00:00")


class TestRunReviewFields:
    def test_run_has_review_defaults(self):
        run = Run(issue_id="x", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1,
                  started_at=datetime.now())
        assert run.review_count == 0
        assert run.review_passed is False
        assert run.review_feedback is None
        assert run.review_history == []

    def test_run_with_review_roundtrip(self, tmp_path):
        from symphony_oc.state import save_run_atomic, load_all, ReviewRecord
        now = datetime.now()
        run = Run(issue_id="x", title="t", branch="b", worktree="w",
                  content_hash="h", status="succeeded", attempt=1,
                  started_at=now, finished_at=now,
                  review_count=3, review_passed=True, review_feedback=None,
                  review_history=[
                      ReviewRecord(iteration=1, verdict="PASS",
                                   timestamp=now, files_affected=["a.py"],
                                   summary="ok", feedback=[]),
                  ])
        path = tmp_path / "state.json"
        save_run_atomic(str(path), [run])
        loaded = load_all(str(path))
        assert len(loaded) == 1
        assert loaded[0].review_count == 3
        assert loaded[0].review_passed is True
        assert len(loaded[0].review_history) == 1
        assert loaded[0].review_history[0].iteration == 1
        assert loaded[0].review_history[0].verdict == "PASS"

    def test_old_state_without_review_fields_loads(self, tmp_path):
        """runs.jsonc written before this feature must still load."""
        from symphony_oc.state import load_all
        path = tmp_path / "old.json"
        path.write_text(
            '[{"issue_id": "x", "title": "t", "branch": "b", '
            '"worktree": "w", "content_hash": "h", "status": "succeeded", '
            '"attempt": 1, "pid": null, "error": null, "pr_url": null, '
            '"next_retry_at": null, "started_at": "2026-07-12T10:00:00", '
            '"finished_at": "2026-07-12T11:00:00"}]'
        )
        loaded = load_all(str(path))
        assert len(loaded) == 1
        assert loaded[0].review_count == 0  # default
        assert loaded[0].review_history == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_state.py::TestReviewRecord tests/test_state.py::TestRunReviewFields -v`
Expected: FAIL with `ImportError: cannot import name 'ReviewRecord'` or `AttributeError`.

- [ ] **Step 3: Add `ReviewRecord` + extend `Run`**

Edit `symphony_oc/state.py`. Replace the `Run` dataclass block and add `ReviewRecord` immediately above it:

```python
@dataclass
class ReviewRecord:
    """单轮审查的完整记录 — 用于人工复盘。"""
    iteration: int                              # 1-indexed 审查轮次
    verdict: str                                # "PASS" | "FAIL"
    timestamp: datetime                         # reviewer 自填时间戳（来自 JSON）
    files_affected: list[str]                   # JSON 中的受影响文件列表
    summary: str                                # JSON 中的概述
    feedback: list[dict]                        # JSON 中的结构化问题列表
    reviewer_pid: Optional[int] = None
    reviewer_started_at: Optional[datetime] = None
    reviewer_finished_at: Optional[datetime] = None
    review_file: Optional[str] = None           # .san/review/review-{N}.json 路径

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewRecord":
        """Tolerant constructor — ignores unknown keys (LLM may emit extras).

        Used by both state._dict_to_run (loading persisted runs.jsonc)
        and reviewer.parse_review_result (parsing reviewer's raw JSON).
        Single source of truth for filter + datetime parsing.
        """
        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in d.items() if k in known}
        for key in ['timestamp', 'reviewer_started_at', 'reviewer_finished_at']:
            if key in filtered and filtered[key]:
                filtered[key] = datetime.fromisoformat(filtered[key])
        return cls(**filtered)


@dataclass
class Run:
    issue_id: str
    title: str
    branch: str
    worktree: str
    content_hash: str
    status: str                    # "queued" | "running" | "reviewing" | "succeeded" | "failed" | "retrying"
    attempt: int
    pid: Optional[int] = None
    error: Optional[str] = None
    pr_url: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    review_count: int = 0
    review_passed: bool = False
    review_feedback: Optional[str] = None
    review_history: list[ReviewRecord] = field(default_factory=list)
```

- [ ] **Step 4: Update `_dict_to_run` and `_run_to_dict`**

Replace the bodies of `_dict_to_run` and `_run_to_dict` in `symphony_oc/state.py`:

```python
def _run_to_dict(run: Run) -> dict:
    """Convert Run dataclass to dict for JSON serialization.

    asdict() recursively flattens nested dataclasses (ReviewRecord) to dicts,
    so review_history becomes list[dict]. The loops then convert datetime
    fields (top-level and nested) to ISO strings.
    """
    d = asdict(run)
    for key, value in d.items():
        if isinstance(value, datetime):
            d[key] = value.isoformat()
    for rec in d.get('review_history', []):
        for key in ['timestamp', 'reviewer_started_at', 'reviewer_finished_at']:
            if rec.get(key) and isinstance(rec[key], datetime):
                rec[key] = rec[key].isoformat()
    return d


def _dict_to_run(d: dict) -> Run:
    """Convert dict back to Run dataclass."""
    for key in ['started_at', 'finished_at', 'next_retry_at']:
        if key in d and d[key]:
            d[key] = datetime.fromisoformat(d[key])
    if 'review_history' in d and d['review_history']:
        d['review_history'] = [ReviewRecord.from_dict(r) for r in d['review_history']]
    return Run(**d)
```

- [ ] **Step 5: Run all state tests**

Run: `pytest tests/test_state.py -v`
Expected: All tests PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add symphony_oc/state.py tests/test_state.py
git commit -s -m "feat(state): add ReviewRecord and review fields to Run

- New ReviewRecord dataclass with from_dict classmethod that tolerates
  unknown JSON keys (LLM emits extras)
- Run gains review_count, review_passed, review_feedback, review_history
- _dict_to_run / _run_to_dict handle nested datetime + ReviewRecord
- Old state files (no review fields) load with defaults"
```

---

## Task 2: config.py — ReviewerConfig

**Files:**
- Modify: `symphony_oc/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ReviewerConfig` dataclass (fields: `name: str = "symphony-reviewer"`, `min_iterations: int = 3`, `max_iterations: int = 5`, `extra_args: list[str] = []`). `AgentConfig` gains nested `reviewer: ReviewerConfig`. `_dict_to_config` parses optional `agent.reviewer` subnode.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
class TestReviewerConfig:
    def test_default_reviewer_when_absent(self, tmp_path):
        from symphony_oc.config import load_config
        wf = tmp_path / "WF.md"
        wf.write_text("---\nagent:\n  name: symphony-worker\n---\n")
        cfg = load_config(wf)
        assert cfg.agent.reviewer.name == "symphony-reviewer"
        assert cfg.agent.reviewer.min_iterations == 3
        assert cfg.agent.reviewer.max_iterations == 5
        assert cfg.agent.reviewer.extra_args == []

    def test_full_reviewer_node_parsed(self, tmp_path):
        from symphony_oc.config import load_config
        wf = tmp_path / "WF.md"
        wf.write_text(
            "---\n"
            "agent:\n"
            "  name: symphony-worker\n"
            "  reviewer:\n"
            "    name: my-reviewer\n"
            "    min_iterations: 2\n"
            "    max_iterations: 4\n"
            "    extra_args: [\"--model\", \"anthropic/claude-opus\"]\n"
            "---\n"
        )
        cfg = load_config(wf)
        assert cfg.agent.reviewer.name == "my-reviewer"
        assert cfg.agent.reviewer.min_iterations == 2
        assert cfg.agent.reviewer.max_iterations == 4
        assert cfg.agent.reviewer.extra_args == ["--model", "anthropic/claude-opus"]

    def test_partial_reviewer_node_uses_defaults(self, tmp_path):
        from symphony_oc.config import load_config
        wf = tmp_path / "WF.md"
        wf.write_text(
            "---\n"
            "agent:\n"
            "  reviewer:\n"
            "    min_iterations: 4\n"
            "---\n"
        )
        cfg = load_config(wf)
        assert cfg.agent.reviewer.min_iterations == 4
        assert cfg.agent.reviewer.max_iterations == 5  # default
        assert cfg.agent.reviewer.name == "symphony-reviewer"  # default
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py::TestReviewerConfig -v`
Expected: FAIL with `AttributeError: 'AgentConfig' object has no attribute 'reviewer'`.

- [ ] **Step 3: Add `ReviewerConfig` and nest in `AgentConfig`**

Edit `symphony_oc/config.py`. Add the new dataclass after `AgentConfig`:

```python
@dataclass
class ReviewerConfig:
    name: str = "symphony-reviewer"
    min_iterations: int = 3
    max_iterations: int = 5
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AgentConfig:
    name: str = "symphony-worker"
    max_concurrent: int = 3
    stall_timeout_ms: int = 1_800_000
    max_retries: int = 3
    retry_backoff_ms: int = 10_000
    extra_args: list[str] = field(default_factory=lambda: ["--pure"])
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)
```

- [ ] **Step 4: Extend `_dict_to_config` to parse reviewer subnode**

In `symphony_oc/config.py`, replace the `if "agent" in d:` block in `_dict_to_config`:

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

- [ ] **Step 5: Run all config tests**

Run: `pytest tests/test_config.py -v`
Expected: All tests PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add symphony_oc/config.py tests/test_config.py
git commit -s -m "feat(config): add ReviewerConfig nested under AgentConfig

- New ReviewerConfig dataclass (name, min/max_iterations, extra_args)
- AgentConfig gains nested reviewer field with sensible defaults
- _dict_to_config parses optional agent.reviewer subnode
- Partial reviewer nodes inherit defaults for missing fields"
```

---

## Task 3: agent_runner.py — extract `_spawn_agent`

**Files:**
- Create: `symphony_oc/agent_runner.py`
- Test: `tests/test_agent_runner.py`

**Interfaces:**
- Produces: `_spawn_agent(agent: str, wt_path: str, extra_args: list[str], prompt_path: str, log_path: str) -> subprocess.Popen`. Starts `opencode run --agent <agent> --dir <wt_path> <extra_args> <prompt_path>` in a new session, stdout redirected to `log_path`. Does **not** pass `--dangerously-skip-permissions`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_runner.py`:

```python
import subprocess
from unittest.mock import MagicMock, patch
from symphony_oc.agent_runner import _spawn_agent


class TestSpawnAgent:
    def test_command_construction(self):
        """Verify the exact argv passed to subprocess.Popen."""
        with patch("symphony_oc.agent_runner.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=12345)
            _spawn_agent(
                agent="symphony-reviewer",
                wt_path="/tmp/wt",
                extra_args=["--model", "anthropic/claude-opus"],
                prompt_path="/tmp/wt/.san/review/review-1.prompt",
                log_path="/tmp/log/review-1.log",
            )
        args, kwargs = mock_popen.call_args
        cmd = args[0] if args else kwargs.get("cmd")
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--agent" in cmd
        assert "symphony-reviewer" in cmd
        assert "--dir" in cmd
        assert "/tmp/wt" in cmd
        assert "--model" in cmd
        assert "anthropic/claude-opus" in cmd
        assert "/tmp/wt/.san/review/review-1.prompt" in cmd
        # Must NOT include --dangerously-skip-permissions
        assert "--dangerously-skip-permissions" not in cmd

    def test_start_new_session_true(self):
        """Subprocess must be in its own session for clean SIGKILL of process group."""
        with patch("symphony_oc.agent_runner.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=1)
            _spawn_agent("a", "/w", [], "p", "l")
        _, kwargs = mock_popen.call_args
        assert kwargs.get("start_new_session") is True

    def test_returns_popen_with_pid(self):
        """Return value must expose .pid for the caller to record."""
        fake_proc = MagicMock(pid=99999)
        with patch("symphony_oc.agent_runner.subprocess.Popen", return_value=fake_proc):
            proc = _spawn_agent("a", "/w", [], "p", "l")
        assert proc.pid == 99999
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent_runner.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'symphony_oc.agent_runner'`.

- [ ] **Step 3: Create `agent_runner.py`**

Create `symphony_oc/agent_runner.py`:

```python
"""Subprocess launcher for opencode agents.

Extracted from executor.dispatch so reviewer.py can reuse the same
launch logic without duplicating subprocess plumbing.
"""

import subprocess


def _spawn_agent(agent: str, wt_path: str, extra_args: list[str],
                 prompt_path: str, log_path: str) -> subprocess.Popen:
    """Start `opencode run` for the given agent and return the Popen.

    - start_new_session=True so SIGKILL hits the whole process group
    - stdout redirected to log_path
    - Never passes --dangerously-skip-permissions (agent boundary is enforced
      by the agent definition itself)
    """
    cmd = ["opencode", "run", "--agent", agent, "--dir", wt_path,
           *extra_args, prompt_path]
    return subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=open(log_path, "wb"),
        stderr=subprocess.STDOUT,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_agent_runner.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add symphony_oc/agent_runner.py tests/test_agent_runner.py
git commit -s -m "feat(agent_runner): extract _spawn_agent helper

Pure extraction target — executor.dispatch will call this in Task 4,
reviewer.dispatch_review / dispatch_fix will call it in Task 5."
```

---

## Task 4: executor.py — `dispatch()` calls `_spawn_agent`

**Files:**
- Modify: `symphony_oc/execer.py`
- Test: `tests/test_executor.py`

**Interfaces:**
- Consumes: `_spawn_agent` from Task 3.
- Produces: unchanged `dispatch(issue, cfg) -> Run | None` signature; behavior identical (existing tests must still pass).

- [ ] **Step 1: Read existing test_executor.py to understand current coverage**

Run: `cat tests/test_executor.py`
Note: existing tests assert specific subprocess invocations. They may need adjustment if they inlined `subprocess.Popen` directly. Plan to update those tests only if they break.

- [ ] **Step 2: Refactor `dispatch()` to call `_spawn_agent`**

In `symphony_oc/executor.py`, replace the `subprocess.Popen(...)` block inside `dispatch()` (currently lines 105–110) with a call to `_spawn_agent`. Add the import at top of file (after the existing `import subprocess`):

```python
from symphony_oc.agent_runner import _spawn_agent
```

Then in `dispatch()`, replace:

```python
        cmd = [
            "opencode", "run",
            "--agent", cfg.agent.name,
            "--dir", wt_path,
            *cfg.agent.extra_args,
            str(wt_prompt),
        ]
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=open(f"log/{issue.id}.log", "wb"),
            stderr=subprocess.STDOUT,
        )
```

with:

```python
        proc = _spawn_agent(
            agent=cfg.agent.name,
            wt_path=wt_path,
            extra_args=cfg.agent.extra_args,
            prompt_path=str(wt_prompt),
            log_path=f"log/{issue.id}.log",
        )
```

- [ ] **Step 3: Run existing executor tests**

Run: `pytest tests/test_executor.py -v`
Expected: All previously-passing tests still PASS. If a test breaks because it asserted `subprocess.Popen` was called directly (in executor module namespace), update it to assert against `symphony_oc.agent_runner.subprocess.Popen` instead — the behavior is identical, only the call site moved.

- [ ] **Step 4: Commit**

```bash
git add symphony_oc/executor.py tests/test_executor.py
git commit -s -m "refactor(executor): call _spawn_agent instead of inline Popen

Behavior unchanged. Prepares for reviewer.py to reuse the same launcher."
```

---

## Task 5: reviewer.py — `dispatch_review`, `dispatch_fix`, `parse_review_result`

**Files:**
- Create: `symphony_oc/reviewer.py`
- Test: `tests/test_reviewer.py`

**Interfaces:**
- Consumes: `_spawn_agent` (Task 3); `Run`, `ReviewRecord` (Task 1).
- Produces:
  - `ReviewResult` dataclass: `passed: bool`, `feedback_text: str`, `record: ReviewRecord`, `raw_json: dict`.
  - `dispatch_review(run: Run, cfg, base_ref: str, previous_feedback: Optional[str]) -> None` — modifies `run` in place: `status='reviewing'`, `pid`, `started_at`, `error=None`. Writes prompt file to `<wt>/.san/review/review-{N}.prompt`, launches reviewer agent, log to `log/{issue_id}.review-{N}.log`.
  - `dispatch_fix(run: Run, cfg, review_feedback: str) -> None` — modifies `run` in place: `status='running'`, `pid`, `started_at`, `error=None`. Writes prompt to `<wt>/.san/skills/review-{N}-fix.md`, launches `symphony-worker`, log to `log/{issue_id}.review-{N}-fix.log` where N = `run.review_count` (the FAIL iteration that triggered this fix).
  - `parse_review_result(wt_path: str, iteration: int) -> ReviewResult` — reads `<wt>/.san/review/review-{iteration}.json`. Failure modes return `ReviewResult(passed=False, ...)` rather than raising.

- [ ] **Step 1: Write failing tests for `parse_review_result`**

Create `tests/test_reviewer.py`:

```python
import json
from datetime import datetime
import pytest
from symphony_oc.reviewer import parse_review_result, ReviewResult


class TestParseReviewResult:
    def test_valid_pass(self, tmp_path):
        review_dir = tmp_path / ".san" / "review"
        review_dir.mkdir(parents=True)
        review_file = review_dir / "review-1.json"
        review_file.write_text(json.dumps({
            "verdict": "PASS",
            "iteration": 1,
            "timestamp": "2026-07-12T10:00:00",
            "files_affected": ["a.py", "b.py"],
            "summary": "LGTM",
            "feedback": [],
        }))
        result = parse_review_result(str(tmp_path), 1)
        assert isinstance(result, ReviewResult)
        assert result.passed is True
        assert result.record.verdict == "PASS"
        assert result.record.iteration == 1

    def test_valid_fail(self, tmp_path):
        review_dir = tmp_path / ".san" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "review-2.json").write_text(json.dumps({
            "verdict": "FAIL",
            "iteration": 2,
            "timestamp": "2026-07-12T10:00:00",
            "files_affected": ["a.py"],
            "summary": "issues found",
            "feedback": [
                {"file": "a.py", "line": 42, "severity": "critical",
                 "issue": "bare except", "suggestion": "narrow it"},
            ],
        }))
        result = parse_review_result(str(tmp_path), 2)
        assert result.passed is False
        assert "critical" in result.feedback_text
        assert "bare except" in result.feedback_text

    def test_missing_file(self, tmp_path):
        """Reviewer did not produce a report — FAIL fallback."""
        result = parse_review_result(str(tmp_path), 3)
        assert result.passed is False
        assert "did not produce" in result.feedback_text.lower()

    def test_malformed_json(self, tmp_path):
        review_dir = tmp_path / ".san" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "review-1.json").write_text("{not valid json")
        result = parse_review_result(str(tmp_path), 1)
        assert result.passed is False
        # Original file must be preserved for human inspection
        assert (review_dir / "review-1.json").read_text() == "{not valid json"

    def test_invalid_verdict(self, tmp_path):
        review_dir = tmp_path / ".san" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "review-1.json").write_text(json.dumps({
            "verdict": "MAYBE",  # illegal value
            "iteration": 1,
            "timestamp": "2026-07-12T10:00:00",
            "files_affected": [],
            "summary": "??",
            "feedback": [],
        }))
        result = parse_review_result(str(tmp_path), 1)
        assert result.passed is False
        assert "verdict" in result.feedback_text.lower()

    def test_extra_unknown_fields_filtered(self, tmp_path):
        """LLM may add fields outside schema — must not crash."""
        review_dir = tmp_path / ".san" / "review"
        review_dir.mkdir(parents=True)
        (review_dir / "review-1.json").write_text(json.dumps({
            "verdict": "PASS",
            "iteration": 1,
            "timestamp": "2026-07-12T10:00:00",
            "files_affected": [],
            "summary": "ok",
            "feedback": [],
            "confidence": 0.95,         # extra
            "reviewer_name": "x",       # extra
        }))
        result = parse_review_result(str(tmp_path), 1)
        assert result.passed is True
        assert result.record.iteration == 1
```

- [ ] **Step 2: Write failing tests for `_format_feedback_text`**

Append to `tests/test_reviewer.py`:

```python
class TestFormatFeedbackText:
    def test_sorted_by_severity_then_file(self):
        from symphony_oc.reviewer import _format_feedback_text
        text = _format_feedback_text(
            items=[
                {"file": "b.py", "line": 1, "severity": "minor",
                 "issue": "b1", "suggestion": "b1s"},
                {"file": "a.py", "line": 2, "severity": "critical",
                 "issue": "a2", "suggestion": "a2s"},
                {"file": "a.py", "line": 1, "severity": "major",
                 "issue": "a1", "suggestion": "a1s"},
            ],
            summary="found issues",
        )
        # Critical must come first
        crit_pos = text.index("a2")
        major_pos = text.index("a1")
        minor_pos = text.index("b1")
        assert crit_pos < major_pos < minor_pos
        assert "found issues" in text

    def test_empty_items_with_fail_summary(self):
        from symphony_oc.reviewer import _format_feedback_text
        text = _format_feedback_text(items=[], summary="weird fail")
        assert "weird fail" in text
        assert "无具体问题" in text
```

- [ ] **Step 3: Write failing tests for `dispatch_review` and `dispatch_fix`**

Append to `tests/test_reviewer.py`:

```python
class TestDispatchReview:
    def test_dispatch_review_modifies_run_and_calls_spawn(self, monkeypatch, tmp_path):
        from symphony_oc.reviewer import dispatch_review
        from symphony_oc.state import Run

        # Mock _spawn_agent
        captured = {}
        class FakeProc:
            pid = 4242
        def fake_spawn(agent, wt_path, extra_args, prompt_path, log_path):
            captured["agent"] = agent
            captured["wt_path"] = wt_path
            captured["extra_args"] = extra_args
            captured["prompt_path"] = prompt_path
            captured["log_path"] = log_path
            return FakeProc()
        monkeypatch.setattr("symphony_oc.reviewer._spawn_agent", fake_spawn)

        # Build minimal Run + cfg
        run = Run(issue_id="i1", title="T", branch="b",
                  worktree=str(tmp_path), content_hash="h",
                  status="running", attempt=1, started_at=datetime.now(),
                  review_count=0)
        # Mock cfg
        class Cfg:
            class agent:
                class reviewer:
                    name = "symphony-reviewer"
                    extra_args = ["--model", "x"]
            class git:
                base_branch = "upstream/main"

        dispatch_review(run, Cfg(), "upstream/main", previous_feedback=None)
        assert run.status == "reviewing"
        assert run.pid == 4242
        assert run.error is None
        assert captured["agent"] == "symphony-reviewer"
        assert captured["extra_args"] == ["--model", "x"]
        assert "review-1.prompt" in captured["prompt_path"]
        assert "review-1.log" in captured["log_path"]


class TestDispatchFix:
    def test_dispatch_fix_uses_worker_agent_and_triggers_iter_in_log(self, monkeypatch, tmp_path):
        from symphony_oc.reviewer import dispatch_fix
        from symphony_oc.state import Run

        captured = {}
        class FakeProc:
            pid = 7777
        def fake_spawn(agent, wt_path, extra_args, prompt_path, log_path):
            captured["agent"] = agent
            captured["prompt_path"] = prompt_path
            captured["log_path"] = log_path
            return FakeProc()
        monkeypatch.setattr("symphony_oc.reviewer._spawn_agent", fake_spawn)

        # review_count=2 means the just-FAILed iteration is 2
        run = Run(issue_id="i1", title="T", branch="b",
                  worktree=str(tmp_path), content_hash="h",
                  status="reviewing", attempt=1, started_at=datetime.now(),
                  review_count=2)
        class Cfg:
            class agent:
                name = "symphony-worker"
                extra_args = ["--pure"]
                class reviewer:
                    pass

        dispatch_fix(run, Cfg(), "fix this and that")
        assert run.status == "running"
        assert run.pid == 7777
        assert captured["agent"] == "symphony-worker"
        # triggering_iter = run.review_count = 2
        assert "review-2-fix" in captured["prompt_path"]
        assert "review-2-fix" in captured["log_path"]
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_reviewer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'symphony_oc.reviewer'`.

- [ ] **Step 5: Create `reviewer.py`**

Create `symphony_oc/reviewer.py`:

```python
"""Reviewer agent dispatch + result parsing.

Reviewer agent (symphony-reviewer) reads code in a worktree and emits a
structured JSON report. This module:
  - dispatch_review: launch reviewer agent, transition run -> reviewing
  - dispatch_fix:    launch fixer (symphony-worker) with feedback, run -> running
  - parse_review_result: read .san/review/review-{N}.json into ReviewResult
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from jinja2 import Template

from symphony_oc.agent_runner import _spawn_agent
from symphony_oc.state import Run, ReviewRecord


logger = logging.getLogger("symphony-oc")


# ---------------------------------------------------------------------------
# Prompt templates (inline, matching executor.py style)
# ---------------------------------------------------------------------------

REVIEW_PROMPT_TEMPLATE = Template("""你是 Symphony Reviewer Agent。你的任务是审查当前 worktree 内未合并到 base 的代码改动，
输出结构化 JSON 报告。

## 上下文
- Issue ID: {{ run.issue_id }}
- Issue 标题: {{ run.title }}
- 审查轮次: {{ iteration }}
- Base ref: {{ base_ref }}

## 上轮审查反馈（如有）
{{ previous_feedback }}

## 工作流程
1. 执行 `git log {{ base_ref }}..HEAD --oneline` 查看本次改动列表
2. 执行 `git diff {{ base_ref }}..HEAD` 审查完整 diff
3. 针对每处改动评估正确性 / 安全性 / 可维护性 / 风格一致性
4. 如有前一轮反馈，核对是否已修复

## 输出规范
严格按照以下 JSON schema 输出到 `.san/review/review-{{ iteration }}.json`：

{
  "verdict": "PASS" | "FAIL",
  "iteration": {{ iteration }},
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
- PASS 表示代码可以进入 reconcile；任何 critical/major 问题都必须 FAIL
- 审查完成后立即退出，不要进入交互模式
- 不要修改除 `.san/review/*.json` 外的任何文件
""")


FIX_PROMPT_TEMPLATE = Template("""你是 Symphony Worker Agent（fixer 角色）。
你的同事（reviewer agent）刚审查了你的改动并提出了反馈，请按反馈修复。

## Issue 信息
- ID: {{ run.issue_id }}
- 标题: {{ run.title }}
- Branch: {{ branch }}

## 审查反馈
{{ feedback_text }}

## 执行要求
1. 你已被切到独立 worktree，cwd 即工作目录
2. 按上面的反馈逐条修复
3. 确保 CI 命令通过（具体命令见 issue 上下文 / 项目约定）
4. 不要执行 git push / git reset --hard / git rebase / git checkout
5. 修改后执行 `git add` 和 `git commit -s` 提交变更
6. 完成后退出，不要进入交互模式
""")


# ---------------------------------------------------------------------------
# ReviewResult
# ---------------------------------------------------------------------------

@dataclass
class ReviewResult:
    passed: bool                           # verdict == "PASS"
    feedback_text: str                     # formatted feedback for fixer
    record: ReviewRecord                   # full record, ready to append
    raw_json: dict                         # raw parsed JSON, for debugging


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "style": 3}


def _format_feedback_text(items: list[dict], summary: str) -> str:
    """Render feedback items as multi-line text for the fixer prompt.

    Sort by severity (critical first), preserve file grouping within each
    severity bucket.
    """
    sorted_items = sorted(
        items,
        key=lambda it: (_SEVERITY_ORDER.get(it.get("severity", "minor"), 99),
                        it.get("file", "")),
    )
    lines = ["## 审查反馈摘要", summary, "", "## 问题列表", ""]
    if not sorted_items:
        lines.append("（无具体问题，但 verdict=FAIL）")
        return "\n".join(lines)

    for it in sorted_items:
        sev = it.get("severity", "minor")
        file_ = it.get("file", "?")
        line = it.get("line")
        loc = f"{file_}:{line}" if line is not None else file_
        lines.append(f"### {loc} [{sev}]")
        lines.append(f"问题：{it.get('issue', '')}")
        lines.append(f"建议：{it.get('suggestion', '')}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _malformed_result(iteration: int, msg: str) -> ReviewResult:
    """Build a FAIL ReviewResult for self-failures (no file / bad JSON / etc.)."""
    now = datetime.now()
    record = ReviewRecord(
        iteration=iteration,
        verdict="FAIL",
        timestamp=now,
        files_affected=[],
        summary=f"reviewer self-failure: {msg}",
        feedback=[],
    )
    return ReviewResult(
        passed=False,
        feedback_text=f"## 审查反馈摘要\nreviewer self-failure: {msg}\n\n## 问题列表\n（reviewer 未产出可用反馈）",
        record=record,
        raw_json={},
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def dispatch_review(run: Run, cfg, base_ref: str,
                    previous_feedback: Optional[str]) -> None:
    """Launch reviewer agent, transition run -> reviewing.

    Modifies run in place: status='reviewing', pid, started_at=now, error=None.
    """
    iteration = run.review_count + 1
    wt_path = run.worktree
    prompt_dir = Path(wt_path) / ".san" / "review"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"review-{iteration}.prompt"

    prompt = REVIEW_PROMPT_TEMPLATE.render(
        run=run,
        base_ref=base_ref,
        previous_feedback=previous_feedback or "（首轮，无前次反馈）",
        iteration=iteration,
    )
    prompt_path.write_text(prompt)

    log_path = f"log/{run.issue_id}.review-{iteration}.log"
    proc = _spawn_agent(
        agent=cfg.agent.reviewer.name,
        wt_path=wt_path,
        extra_args=cfg.agent.reviewer.extra_args,
        prompt_path=str(prompt_path),
        log_path=log_path,
    )

    run.status = "reviewing"
    run.pid = proc.pid
    run.started_at = datetime.now()
    run.error = None


def dispatch_fix(run: Run, cfg, review_feedback: str) -> None:
    """Launch fixer agent (symphony-worker), transition run -> running.

    triggering_iter = run.review_count (the just-FAILed iteration).
    Modifies run in place: status='running', pid, started_at=now, error=None.
    """
    triggering_iter = run.review_count  # the FAIL that triggered this fix
    wt_path = run.worktree
    prompt_dir = Path(wt_path) / ".san" / "skills"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / f"review-{triggering_iter}-fix.md"

    prompt = FIX_PROMPT_TEMPLATE.render(
        run=run,
        feedback_text=review_feedback,
        branch=run.branch,
    )
    prompt_path.write_text(prompt)

    log_path = f"log/{run.issue_id}.review-{triggering_iter}-fix.log"
    proc = _spawn_agent(
        agent=cfg.agent.name,  # fixer reuses symphony-worker
        wt_path=wt_path,
        extra_args=cfg.agent.extra_args,
        prompt_path=str(prompt_path),
        log_path=log_path,
    )

    run.status = "running"
    run.pid = proc.pid
    run.started_at = datetime.now()
    run.error = None


def parse_review_result(wt_path: str, iteration: int) -> ReviewResult:
    """Read .san/review/review-{iteration}.json and return ReviewResult.

    Never raises — all reviewer self-failures return ReviewResult(passed=False).
    """
    review_file = Path(wt_path) / ".san" / "review" / f"review-{iteration}.json"
    if not review_file.exists():
        return _malformed_result(iteration, "reviewer did not produce report")

    try:
        raw = json.loads(review_file.read_text())
    except json.JSONDecodeError as e:
        return _malformed_result(iteration, f"malformed JSON: {e}")

    verdict = raw.get("verdict")
    if verdict not in ("PASS", "FAIL"):
        return _malformed_result(iteration, f"verdict invalid: {verdict!r}")

    record = ReviewRecord.from_dict(raw)  # tolerates extra keys
    passed = verdict == "PASS"
    feedback_text = _format_feedback_text(
        raw.get("feedback", []),
        raw.get("summary", ""),
    )
    return ReviewResult(
        passed=passed,
        feedback_text=feedback_text,
        record=record,
        raw_json=raw,
    )
```

- [ ] **Step 6: Run all reviewer tests**

Run: `pytest tests/test_reviewer.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add symphony_oc/reviewer.py tests/test_reviewer.py
git commit -s -m "feat(reviewer): add dispatch_review, dispatch_fix, parse_review_result

- Inline Jinja2 templates for review/fix prompts (matches executor style)
- parse_review_result tolerates LLM extra fields via ReviewRecord.from_dict
- Self-failure modes (no file / bad JSON / bad verdict) return FAIL
  ReviewResult instead of raising — counted toward review_count to
  prevent infinite loops
- _format_feedback_text sorts by severity, groups by file"
```

---

## Task 6: orchestrator.py — `process_completed` routing + `_on_worker_done` + `_on_reviewer_done`

**Files:**
- Modify: `symphony_oc/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `dispatch_review`, `dispatch_fix`, `parse_review_result` (Task 5); `reconcile` (existing).
- Produces: rewritten `process_completed(runs, cfg)` that routes both `"running"` and `"reviewing"` runs; new helpers `_on_worker_done(run, cfg)` and `_on_reviewer_done(run, cfg)`. `_pid_exists_simple` already exists — reused unchanged.

- [ ] **Step 1: Write failing tests for `process_completed` routing**

Append to `tests/test_orchestrator.py`:

```python
class TestProcessCompletedRouting:
    def test_running_run_with_dead_pid_routes_to_worker_done(self, monkeypatch):
        from symphony_oc.orchestrator import process_completed
        from symphony_oc.state import Run
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1, pid=1,
                  started_at=datetime.now())
        monkeypatch.setattr("symphony_oc.orchestrator._pid_exists_simple", lambda pid: False)
        called = {}
        monkeypatch.setattr("symphony_oc.orchestrator._on_worker_done",
                            lambda r, c: called.setdefault("worker", r))
        monkeypatch.setattr("symphony_oc.orchestrator._on_reviewer_done",
                            lambda r, c: called.setdefault("reviewer", r))
        process_completed([run], cfg=None)
        assert "worker" in called
        assert "reviewer" not in called

    def test_reviewing_run_with_dead_pid_routes_to_reviewer_done(self, monkeypatch):
        from symphony_oc.orchestrator import process_completed
        from symphony_oc.state import Run
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="reviewing", attempt=1, pid=1,
                  started_at=datetime.now())
        monkeypatch.setattr("symphony_oc.orchestrator._pid_exists_simple", lambda pid: False)
        called = {}
        monkeypatch.setattr("symphony_oc.orchestrator._on_worker_done",
                            lambda r, c: called.setdefault("worker", r))
        monkeypatch.setattr("symphony_oc.orchestrator._on_reviewer_done",
                            lambda r, c: called.setdefault("reviewer", r))
        process_completed([run], cfg=None)
        assert "reviewer" in called
        assert "worker" not in called

    def test_alive_pid_not_routed(self, monkeypatch):
        from symphony_oc.orchestrator import process_completed
        from symphony_oc.state import Run
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1, pid=1,
                  started_at=datetime.now())
        monkeypatch.setattr("symphony_oc.orchestrator._pid_exists_simple", lambda pid: True)
        called = []
        monkeypatch.setattr("symphony_oc.orchestrator._on_worker_done",
                            lambda r, c: called.append(r))
        process_completed([run], cfg=None)
        assert called == []
```

- [ ] **Step 2: Write failing tests for `_on_worker_done`**

Append to `tests/test_orchestrator.py`:

```python
class TestOnWorkerDone:
    def test_calls_dispatch_review(self, monkeypatch):
        from symphony_oc.orchestrator import _on_worker_done
        from symphony_oc.state import Run
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1, pid=1,
                  started_at=datetime.now())
        captured = {}
        monkeypatch.setattr("symphony_oc.orchestrator.dispatch_review",
                            lambda r, c, b, p: captured.setdefault("called", (r, b, p)))
        _on_worker_done(run, cfg=object())
        assert "called" in captured

    def test_dispatch_failure_marks_run_failed(self, monkeypatch):
        from symphony_oc.orchestrator import _on_worker_done
        from symphony_oc.state import Run
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1, pid=1,
                  started_at=datetime.now())
        def boom(*args, **kw):
            raise RuntimeError("spawn failed")
        monkeypatch.setattr("symphony_oc.orchestrator.dispatch_review", boom)
        _on_worker_done(run, cfg=object())
        assert run.status == "failed"
        assert "spawn failed" in (run.error or "")
```

- [ ] **Step 3: Write failing tests for `_on_reviewer_done` decision table**

Append to `tests/test_orchestrator.py`:

```python
class TestOnReviewerDoneDecisionTable:
    """Cover all 4 branches of the decision table + reconcile-retry path."""

    def _make_run(self, review_count=0, status="reviewing"):
        from symphony_oc.state import Run
        return Run(issue_id="i", title="t", branch="b", worktree="/wt",
                   content_hash="h", status=status, attempt=1, pid=99,
                   started_at=datetime.now(), review_count=review_count)

    def _make_cfg(self, min_iter=3, max_iter=5):
        from types import SimpleNamespace
        return SimpleNamespace(
            git=SimpleNamespace(base_branch="upstream/main"),
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=min_iter, max_iterations=max_iter,
                    name="r", extra_args=[],
                ),
            ),
        )

    def test_pass_n_ge_min_reconciles(self, monkeypatch):
        from symphony_oc.orchestrator import _on_reviewer_done
        run = self._make_run(review_count=2)  # next iter = 3
        monkeypatch.setattr("symphony_oc.orchestrator.parse_review_result",
                            lambda wt, it: _fake_result(passed=True))
        reconciled = []
        monkeypatch.setattr("symphony_oc.orchestrator.reconcile",
                            lambda r, c: reconciled.append(r))
        _on_reviewer_done(run, self._make_cfg())
        assert len(reconciled) == 1
        assert run.review_count == 3
        assert run.review_passed is True

    def test_pass_n_lt_min_dispatches_re_review(self, monkeypatch):
        from symphony_oc.orchestrator import _on_reviewer_done
        run = self._make_run(review_count=0)  # next iter = 1
        monkeypatch.setattr("symphony_oc.orchestrator.parse_review_result",
                            lambda wt, it: _fake_result(passed=True))
        reviewed = []
        monkeypatch.setattr("symphony_oc.orchestrator.dispatch_review",
                            lambda r, c, b, p: reviewed.append(r))
        _on_reviewer_done(run, self._make_cfg())
        assert len(reviewed) == 1
        assert run.review_count == 1

    def test_fail_n_lt_max_dispatches_fixer(self, monkeypatch):
        from symphony_oc.orchestrator import _on_reviewer_done
        run = self._make_run(review_count=0)  # next iter = 1
        monkeypatch.setattr("symphony_oc.orchestrator.parse_review_result",
                            lambda wt, it: _fake_result(passed=False))
        fixed = []
        monkeypatch.setattr("symphony_oc.orchestrator.dispatch_fix",
                            lambda r, c, f: fixed.append(r))
        _on_reviewer_done(run, self._make_cfg())
        assert len(fixed) == 1

    def test_fail_n_ge_max_marks_failed(self, monkeypatch):
        from symphony_oc.orchestrator import _on_reviewer_done
        run = self._make_run(review_count=4)  # next iter = 5
        monkeypatch.setattr("symphony_oc.orchestrator.parse_review_result",
                            lambda wt, it: _fake_result(passed=False))
        _on_reviewer_done(run, self._make_cfg())
        assert run.status == "failed"

    def test_reconcile_exception_schedules_retry(self, monkeypatch):
        from symphony_oc.orchestrator import _on_reviewer_done
        run = self._make_run(review_count=2)  # next iter = 3
        monkeypatch.setattr("symphony_oc.orchestrator.parse_review_result",
                            lambda wt, it: _fake_result(passed=True))
        def boom(r, c): raise RuntimeError("CI flaky")
        monkeypatch.setattr("symphony_oc.orchestrator.reconcile", boom)
        _on_reviewer_done(run, self._make_cfg())
        assert run.status == "retrying"
        assert "CI flaky" in (run.error or "")


def _fake_result(passed: bool):
    """Build a minimal ReviewResult-shaped object for tests."""
    from symphony_oc.state import ReviewRecord
    from symphony_oc.reviewer import ReviewResult
    from datetime import datetime
    return ReviewResult(
        passed=passed,
        feedback_text="fb",
        record=ReviewRecord(
            iteration=1, verdict="PASS" if passed else "FAIL",
            timestamp=datetime.now(), files_affected=[], summary="s",
            feedback=[],
        ),
        raw_json={},
    )


class TestOnReviewerDoneRecordFields:
    def test_record_gets_pid_and_timestamps(self, monkeypatch, tmp_path):
        """parse_review_result can't know pid/timing — _on_reviewer_done fills them."""
        from symphony_oc.orchestrator import _on_reviewer_done
        from symphony_oc.state import Run
        from types import SimpleNamespace
        run = Run(issue_id="i", title="t", branch="b", worktree=str(tmp_path),
                  content_hash="h", status="reviewing", attempt=1, pid=12345,
                  started_at=datetime(2026, 7, 12, 10, 0, 0),
                  review_count=2)
        cfg = SimpleNamespace(
            git=SimpleNamespace(base_branch="upstream/main"),
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(min_iterations=3, max_iterations=5,
                                         name="r", extra_args=[]),
            ),
        )
        monkeypatch.setattr("symphony_oc.orchestrator.parse_review_result",
                            lambda wt, it: _fake_result(passed=True))
        monkeypatch.setattr("symphony_oc.orchestrator.reconcile", lambda r, c: None)
        _on_reviewer_done(run, cfg)
        rec = run.review_history[-1]
        assert rec.reviewer_pid == 12345
        assert rec.reviewer_started_at == datetime(2026, 7, 12, 10, 0, 0)
        assert rec.reviewer_finished_at is not None
        assert "review-3.json" in (rec.review_file or "")
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator.py -v`
Expected: FAIL — `_on_worker_done` and `_on_reviewer_done` do not exist; `process_completed` doesn't route reviewing.

- [ ] **Step 5: Add imports to orchestrator.py**

Edit `symphony_oc/orchestrator.py`. Add these imports alongside the existing ones (after `from symphony_oc.executor import dispatch`):

```python
from symphony_oc.reviewer import dispatch_review, dispatch_fix, parse_review_result
```

- [ ] **Step 6: Replace `process_completed` with routing version**

In `symphony_oc/orchestrator.py`, replace the existing `process_completed` function (lines 153–169) with:

```python
def process_completed(runs: list[Run], cfg) -> None:
    """Detect completed agents (dead PID) and route to next phase.

    - status='running'   → _on_worker_done (launch reviewer)
    - status='reviewing' → _on_reviewer_done (parse JSON + decide)
    """
    for run in runs:
        if run.status not in ("running", "reviewing"):
            continue
        if _pid_exists_simple(run.pid):
            continue  # agent still alive
        logger.info("agent completed: %s (status=%s, pid=%s)",
                    run.issue_id, run.status, run.pid)
        if run.status == "running":
            _on_worker_done(run, cfg)
        else:  # reviewing
            _on_reviewer_done(run, cfg)


def _on_worker_done(run: Run, cfg) -> None:
    """Worker (implementer or fixer) exited → dispatch reviewer."""
    try:
        dispatch_review(run, cfg, cfg.git.base_branch, run.review_feedback)
    except Exception as e:
        logger.exception("dispatch_review failed for %s: %s", run.issue_id, e)
        mark_failed(run, f"dispatch_review error: {e}")


def _on_reviewer_done(run: Run, cfg) -> None:
    """Reviewer exited → parse JSON, append record, route by decision table."""
    iteration = run.review_count + 1
    # Capture reviewer process info BEFORE parse / dispatch overwrites run.
    reviewer_pid = run.pid
    reviewer_started_at = run.started_at
    reviewer_finished_at = datetime.now()

    try:
        result = parse_review_result(run.worktree, iteration)
    except Exception as e:
        logger.exception("parse_review_result crashed for %s: %s", run.issue_id, e)
        mark_failed(run, f"parse_review_result crash: {e}")
        return

    # Fields parse_review_result cannot derive from the JSON file alone.
    result.record.reviewer_pid = reviewer_pid
    result.record.reviewer_started_at = reviewer_started_at
    result.record.reviewer_finished_at = reviewer_finished_at
    result.record.review_file = f"{run.worktree}/.san/review/review-{iteration}.json"

    run.review_history.append(result.record)
    run.review_count = iteration
    run.review_passed = result.passed
    run.review_feedback = result.feedback_text

    min_iter = cfg.agent.reviewer.min_iterations
    max_iter = cfg.agent.reviewer.max_iterations

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
        logger.info("%s: review PASS iter=%d < min=%d → re-review",
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

- [ ] **Step 7: Run all orchestrator tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All tests PASS (new decision-table + routing tests, plus existing `TestRetryDelay` / `TestCheckStalls` / `TestProcessRetryQueue`).

- [ ] **Step 8: Commit**

```bash
git add symphony_oc/orchestrator.py tests/test_orchestrator.py
git commit -s -m "feat(orchestrator): route completed workers to reviewer + add _on_reviewer_done

- process_completed now routes both 'running' (→ _on_worker_done →
  dispatch_review) and 'reviewing' (→ _on_reviewer_done) statuses
- _on_reviewer_done implements the decision table: PASS+N>=min → reconcile,
  PASS+N<min → re-review, FAIL+N<max → dispatch_fix, FAIL+N>=max →
  mark_failed; reconcile exceptions schedule_retry
- Captures reviewer pid/started_at BEFORE dispatch overwrites them;
  sets finished_at = now; assigns to result.record before append"
```

---

## Task 7: bootstrap.py — `check_reviewer_model`

**Files:**
- Modify: `symphony_oc/bootstrap.py`
- Test: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `load_config` (existing); `ReviewerConfig` (Task 2).
- Produces: `check_reviewer_model()` function. Wired into `bootstrap.main()`'s checks list. Soft-warns when `agent.reviewer.extra_args` has no `--model`; raises `BootError` when `min_iterations > max_iterations`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bootstrap.py`:

```python
import pytest
from unittest.mock import patch
from symphony_oc.bootstrap import check_reviewer_model, BootError


class TestCheckReviewerModel:
    def test_min_gt_max_raises_boot_error(self, tmp_path):
        from types import SimpleNamespace
        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=5, max_iterations=3, extra_args=[],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap.REPO_ROOT", tmp_path):
                with patch("symphony_oc.bootstrap._list_opencode_models",
                           return_value=[]):
                    with pytest.raises(BootError):
                        check_reviewer_model()

    def test_min_eq_max_ok(self):
        from types import SimpleNamespace
        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=3, max_iterations=3,
                    extra_args=["--model", "x"],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap._list_opencode_models",
                       return_value=[]):
                check_reviewer_model()  # must not raise

    def test_missing_model_with_providers_warns_not_raises(self, capsys):
        from types import SimpleNamespace
        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=3, max_iterations=5, extra_args=[],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap._list_opencode_models",
                       return_value=["anthropic/claude-opus", "bigmodel/coding"]):
                check_reviewer_model()  # must not raise
            captured = capsys.readouterr()
            assert "未指定 --model" in captured.out

    def test_model_flag_present_silent(self, capsys):
        from types import SimpleNamespace
        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=3, max_iterations=5,
                    extra_args=["--model", "anthropic/claude-opus"],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap._list_opencode_models",
                       return_value=["anthropic/claude-opus"]):
                check_reviewer_model()
            captured = capsys.readouterr()
            assert "未指定 --model" not in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bootstrap.py::TestCheckReviewerModel -v`
Expected: FAIL with `ImportError: cannot import name 'check_reviewer_model'`.

- [ ] **Step 3: Add `check_reviewer_model` and helpers**

Edit `symphony_oc/bootstrap.py`. Add the function before the `if __name__ == "__main__":` line:

```python
def _list_opencode_models() -> list[str]:
    """Best-effort scan of opencode config for available provider/model ids.

    Returns [] if nothing parseable is found — caller treats as 'no providers'.
    """
    import re
    config_path = Path.home() / ".config/opencode/opencode.jsonc"
    if not config_path.exists():
        return []
    try:
        text = config_path.read_text()
        text = re.sub(r"//.*", "", text)  # strip JSONC comments
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return []
    providers = data.get("providers", {})
    out = []
    for provider_name, provider_cfg in providers.items():
        if isinstance(provider_cfg, dict):
            models = provider_cfg.get("models", {})
            if isinstance(models, dict):
                for model_id in models:
                    out.append(f"{provider_name}/{model_id}")
    return out


def check_reviewer_model() -> None:
    """Validate reviewer config + warn about missing --model."""
    cfg = load_config(REPO_ROOT / "WORKFLOW.md")

    if cfg.agent.reviewer.min_iterations > cfg.agent.reviewer.max_iterations:
        raise BootError(
            f"agent.reviewer.min_iterations ({cfg.agent.reviewer.min_iterations}) "
            f"> max_iterations ({cfg.agent.reviewer.max_iterations}). "
            f"决策表无法收敛 — 请调整 WORKFLOW.md。"
        )

    args = cfg.agent.reviewer.extra_args
    try:
        idx = args.index("--model")
        if idx + 1 < len(args):
            return  # explicit --model <value>
    except ValueError:
        pass

    available = _list_opencode_models()
    if available:
        preview = ", ".join(available[:5]) + (" ..." if len(available) > 5 else "")
        print(
            f"  ⚠ reviewer extra_args 未指定 --model。可用 provider/model: {preview}\n"
            f"    建议在 WORKFLOW.md 的 agent.reviewer.extra_args 加 "
            f"[\"--model\", \"<strong-model>\"]"
        )
    else:
        print(
            "  ⚠ reviewer extra_args 未指定 --model，且未在 ~/.config/opencode/opencode.jsonc "
            "找到已配置 provider。审查将用 opencode 默认模型（可能偏弱）。"
        )
```

- [ ] **Step 4: Wire `check_reviewer_model` into the checks list**

In `symphony_oc/bootstrap.py`, edit the `checks` list inside `main()` to add the new entry after `init_workspace` (and before `smoke_test_agent`):

```python
    checks = [
        ("check_opencode_version", check_opencode_version),
        ("check_external_tools", check_external_tools),
        ("check_git_remote", check_git_remote),
        ("install_agent", install_agent),
        ("install_reviewer_agent", install_reviewer_agent),
        ("verify_agent_discoverable", verify_agent_discoverable),
        ("verify_reviewer_discoverable", verify_reviewer_discoverable),
        ("check_providers", check_providers),
        ("init_workspace", init_workspace),
        ("check_reviewer_model", check_reviewer_model),
        ("smoke_test_agent", smoke_test_agent),
    ]
```

Also ensure `load_config` is importable in bootstrap.py. Check existing imports at top of `bootstrap.py` and add `from symphony_oc.config import load_config` if missing.

- [ ] **Step 5: Run all bootstrap tests**

Run: `pytest tests/test_bootstrap.py -v`
Expected: All tests PASS (new + existing).

- [ ] **Step 6: Commit**

```bash
git add symphony_oc/bootstrap.py tests/test_bootstrap.py
git commit -s -m "feat(bootstrap): add check_reviewer_model

- Hard fail when min_iterations > max_iterations (decision table cannot
  converge)
- Soft warn when extra_args lacks --model (reviewer should use a strong
  model); lists available providers if any
- Wired into bootstrap.main() after init_workspace"
```

---

## Task 8: agents/symphony-reviewer.md — reviewer agent file

**Files:**
- Create: `agents/symphony-reviewer.md`
- Test: bootstrap integration — `install_reviewer_agent` + `verify_reviewer_discoverable` will pick this up automatically (existing functions in `bootstrap.py` already handle the install path).

**Interfaces:**
- Produces: opencode agent definition with read-only permissions. Discoverable via `opencode agent list`.

> **Note on bash whitelist syntax:** Per spec §4.1, this file uses `"git diff": allow` (prefix-match form) rather than `"git diff *": allow`. The existing `symphony-worker.md` uses the `*` form, but opencode accepts both — prefix form is the safer documented contract. If smoke test fails (reviewer cannot run `git diff`), fall back to `"git diff *": allow` and document the deviation.

- [ ] **Step 1: Create the agent file**

Create `agents/symphony-reviewer.md`:

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
    "git diff": allow
    "git log": allow
    "git show": allow
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

- [ ] **Step 2: Verify bootstrap picks it up**

Run: `python -c "from symphony_oc.bootstrap import install_reviewer_agent, BUNDLED_REVIEWER, REVIEWER_INSTALL_PATH; print('bundled:', BUNDLED_REVIEWER); print('installed:', REVIEWER_INSTALL_PATH); install_reviewer_agent(); print('install OK')"`

Expected output: install OK with no exceptions.

- [ ] **Step 3: Verify agent is discoverable**

Run: `opencode agent list | grep symphony-reviewer`

Expected: outputs a line containing `symphony-reviewer`.

- [ ] **Step 4: Commit**

```bash
git add agents/symphony-reviewer.md
git commit -s -m "feat(agents): add read-only symphony-reviewer agent

- Read-only code review: bash whitelist git status/diff/log/show only;
  edit restricted to .san/review/*; everything else denied
- Emits structured JSON report (verdict, files_affected, feedback array)
- bootstrap.install_reviewer_agent already wired; this file makes
  verify_reviewer_discoverable pass"
```

---

## Task 9: WORKFLOW.md — document `agent.reviewer` node

**Files:**
- Modify: `WORKFLOW.md`

**Interfaces:**
- Produces: documented (commented) `reviewer` subnode under `agent`, so users can uncomment and customize. Default behavior unchanged (load_config uses `ReviewerConfig` defaults when node absent).

- [ ] **Step 1: Read current WORKFLOW.md agent section**

Run: `sed -n '/^agent:/,/^[a-z]/p' WORKFLOW.md`

Note the current `agent:` block (currently 7 keys: name, max_concurrent, stall_timeout_ms, max_retries, retry_backoff_ms, extra_args).

- [ ] **Step 2: Add `reviewer` subnode as commented examples**

In `WORKFLOW.md`, immediately after the `extra_args: ["--pure"]` line under `agent:`, add the commented reviewer node:

```yaml
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
```

- [ ] **Step 3: Verify load_config still accepts WORKFLOW.md**

Run: `python -c "from symphony_oc.config import load_config; cfg = load_config('WORKFLOW.md'); print('agent.reviewer:', cfg.agent.reviewer)"`

Expected: prints `agent.reviewer: ReviewerConfig(name='symphony-reviewer', min_iterations=3, max_iterations=5, extra_args=[])` (defaults — reviewer is commented out).

- [ ] **Step 4: Run full test suite to confirm no regressions**

Run: `pytest tests/ -v`
Expected: All tests PASS across all modules.

- [ ] **Step 5: Commit**

```bash
git add WORKFLOW.md
git commit -s -m "docs(workflow): document agent.reviewer subnode (commented examples)

Reviewer config defaults work without this node — these comments show
how to customize name/min/max_iterations/extra_args for users who want
to override defaults."
```

---

## Final Verification

After all 9 tasks land:

- [ ] **End-to-end smoke (manual):**

  1. Run `python -m symphony_oc.bootstrap` — verify all checks pass including `check_reviewer_model`.
  2. Drop a test issue in `issues/` and run orchestrator: `python -m symphony_oc.orchestrator`.
  3. Verify the run transitions through: `running` → `reviewing` → (FAIL → `running` fixer → `reviewing`) → PASS × 3 → `succeeded`.
  4. Inspect `state/runs.jsonc` — confirm `review_history` contains all iterations with `reviewer_pid`, `reviewer_started_at`, `reviewer_finished_at`, `review_file` populated.
  5. Inspect `log/{issue_id}.review-*.log` and `log/{issue_id}.review-*-fix.log` — verify they exist and contain opencode output.

- [ ] **Full test suite:**

  Run: `pytest tests/ -v`
  Expected: 100% PASS.
