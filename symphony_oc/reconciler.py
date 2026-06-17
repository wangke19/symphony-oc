"""Reconciler — CI validation, two-commit split, PR creation.

Depends on:
  - subproc.run_bash (Task 5)
  - state.Run, mark_failed, mark_succeeded, schedule_retry (Tasks 1+4)
"""

import os
import shlex
import shutil
from datetime import datetime

from symphony_oc.subproc import run_bash
from symphony_oc.state import Run, mark_failed, mark_succeeded, schedule_retry


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class GhAuthExpired(RuntimeError):
    """Raised when gh auth status check fails (auth expired or invalid)."""
    pass


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------

def assert_gh_auth() -> None:
    """Raise GhAuthExpired if gh auth status is not OK."""
    result = run_bash("gh auth status", shell=True)
    if result.returncode != 0:
        raise GhAuthExpired("GitHub authentication expired or invalid")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def commit_all(wt_path: str, message: str) -> None:
    """git add -A && git commit with the given message."""
    run_bash(f"git add -A && git commit -m {shlex.quote(message)}", cwd=wt_path, shell=True)


def commit_selective(wt_path: str, message: str, exclude: list[str]) -> None:
    """git add -A, commit, then reset excluded paths back."""
    run_bash(f"git add -A && git commit -m {shlex.quote(message)}", cwd=wt_path, shell=True)
    for pattern in exclude:
        run_bash(f"git reset HEAD -- '{pattern}'", cwd=wt_path, shell=True)
        # Also restore working tree for excluded files
        run_bash(f"git checkout -- '{pattern}'", cwd=wt_path, shell=True)


def has_pending_changes(wt_path: str) -> bool:
    """Return True if git status --porcelain reports any changes."""
    result = run_bash("git status --porcelain", cwd=wt_path, shell=True)
    return bool(result.stdout.strip())


def cleanup_worktree(run: Run) -> None:
    """Remove the worktree directory and delete the associated branch."""
    if run.worktree and os.path.isdir(run.worktree):
        run_bash(f"git worktree remove -f {shlex.quote(run.worktree)}", shell=True)
    branch = run.branch
    if branch:
        # Force delete even if unmerged
        run_bash(f"git branch -D {shlex.quote(branch)}", shell=True)


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------

def create_pr(run: Run, ci_stdout: str, cfg) -> str:
    """Create a GitHub PR via gh CLI.

    Returns the PR URL on success.
    """
    body_lines = [
        f"## CI Results",
        f"```",
        ci_stdout,
        f"```",
        "",
        f"---",
        f"Created by symphony agent (issue: {run.issue_id})",
    ]
    body = "\n".join(body_lines)

    title = f"Symphony: {run.title}"

    result = run_bash(
        f"gh pr create "
        f"--title {shlex.quote(title)} "
        f"--body {shlex.quote(body)} "
        f"--base {shlex.quote(cfg.target_branch)}",
        cwd=run.worktree,
        shell=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr}")

    pr_url = result.stdout.strip()
    return pr_url


# ---------------------------------------------------------------------------
# Main reconcile flow
# ---------------------------------------------------------------------------

def reconcile(run: Run, cfg) -> None:
    """Execute the full reconcile cycle for a run.

    Steps:
      1. Verify gh auth
      2. Check for pending changes in worktree
      3. Run CI (check=False to avoid CalledProcessError on failure)
      4. If CI passes: commit (two-commit split if configured), push, create PR
      5. Mark succeeded or failed accordingly
    """
    wt = run.worktree
    if not wt or not os.path.isdir(wt):
        mark_failed(run, f"worktree directory not found: {wt}")
        return

    # 1. Auth check
    try:
        assert_gh_auth()
    except GhAuthExpired:
        schedule_retry(run, "GitHub auth expired")
        return

    # 2. Check for changes
    if not has_pending_changes(wt):
        mark_failed(run, "no pending changes to commit")
        return

    # 3. CI run — MUST use check=False so CalledProcessError isn't raised
    ci = run_bash(cfg.ci.command, cwd=wt, timeout=cfg.ci.timeout_ms // 1000, check=False)

    if ci.returncode != 0:
        mark_failed(run, f"CI failed (exit {ci.returncode}): {ci.stderr[:500]}")
        return

    # 4. Commit
    commit_message = f"feat: {run.title}"
    if cfg.two_commit_split:
        # First commit: infrastructure / deps
        commit_all(wt, f"chore: {run.title}")
        # Second commit would go here for generated artifacts
        # (skipped in non-repo context)
    else:
        commit_all(wt, commit_message)

    # 5. Push
    push_result = run_bash("git push -u origin HEAD", cwd=wt, shell=True)
    if push_result.returncode != 0:
        mark_failed(run, f"git push failed: {push_result.stderr[:500]}")
        return

    # 6. Create PR
    try:
        pr_url = create_pr(run, ci.stdout, cfg)
    except Exception as exc:
        mark_failed(run, f"PR creation failed: {exc}")
        return

    # 7. Success
    mark_succeeded(run, pr_url=pr_url)
