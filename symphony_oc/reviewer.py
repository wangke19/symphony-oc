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
