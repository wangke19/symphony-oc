import hashlib
import re
from symphony_oc.state import Issue, Run


def slugify(title: str, max_len: int = 60) -> str:
    """Convert title to URL-safe branch segment."""
    s = title.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    s = s.strip("-")
    return s[:max_len].rstrip("-")


def can_dispatch(runs: list[Run], max_concurrent: int) -> bool:
    """True if running+queued+retrying count is under the limit."""
    active = sum(1 for r in runs if r.status in {"running", "queued", "retrying"})
    return active < max_concurrent


def should_dispatch(issue: Issue, runs: list[Run]) -> bool:
    matching = [r for r in runs if r.issue_id == issue.id]
    if issue.source == "github":
        return not any(r.status in {"running", "succeeded", "queued"} for r in matching)
    content_hash = hashlib.sha256(issue.description.encode()).hexdigest()[:12]
    for r in matching:
        if r.content_hash == content_hash:
            return False
        if r.status == "running":
            return False
    return True


import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from jinja2 import Template
from symphony_oc.state import Issue, Run, hash_issue
from symphony_oc.subproc import run_bash


PROMPT_TEMPLATE = Template("""你是 OpenCode Agent，正在处理一个来自 Issue Tracker 的开发任务。

## Issue 信息
- ID: {{ issue.id }}
- 标题: {{ issue.title }}
- 来源: {{ issue.source }}

## 任务描述
{{ issue.description }}

## 执行要求
1. 你已被切到独立 worktree（branch: symphony/{{ issue.id }}/{{ slug }}），cwd 即工作目录
2. 实现 Issue 描述的功能
3. 确保 CI 命令 "{{ ci_command }}" 通过
4. **不要** 执行 git push / git reset --hard / git rebase / git checkout（agent 权限已 deny）
5. 完成后退出，不要进入交互模式
""")


def generate_prompt(issue: Issue, ci_command: str) -> str:
    return PROMPT_TEMPLATE.render(
        issue=issue,
        ci_command=ci_command,
        slug=slugify(issue.title),
    )


def dispatch(issue: Issue, cfg) -> Run | None:
    branch = f"symphony/{issue.id}/{slugify(issue.title)}"
    wt_path = f"{cfg.git.worktree_root}/{issue.id}"
    prompt_path = f"issues/{issue.id}.prompt"

    try:
        run_bash(f"git fetch {cfg.git.remote}")
        run_bash(f"git worktree add -b {branch} {wt_path} {cfg.git.base_branch}")

        prompt = generate_prompt(issue, cfg.ci.command)
        Path(prompt_path).write_text(prompt)

        cmd = [
            "opencode", "run",
            "--agent", cfg.agent.name,
            "--dir", wt_path,
            *cfg.agent.extra_args,
            prompt,
        ]
        proc = subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=open(f"log/{issue.id}.log", "wb"),
            stderr=subprocess.STDOUT,
        )

        run = Run(
            issue_id=issue.id,
            title=issue.title,
            branch=branch,
            worktree=wt_path,
            content_hash=hash_issue(issue),
            status="running",
            attempt=1,
            pid=proc.pid,
            started_at=datetime.now(),
        )
        return run

    except (subprocess.CalledProcessError, OSError, Exception) as e:
        run_bash(f"git worktree remove {wt_path} --force", check=False)
        failed_run = Run(
            issue_id=issue.id,
            title=issue.title,
            branch=branch,
            worktree=wt_path,
            content_hash=hash_issue(issue),
            status="failed",
            attempt=1,
            error=f"infra: {e}",
            started_at=datetime.now(),
            finished_at=datetime.now(),
        )
        return None
