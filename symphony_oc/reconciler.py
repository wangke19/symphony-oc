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

def create_pr(run: Run, ci_stdout: str, cfg, target_branch: str = "main") -> str:
    """Create a GitHub PR via gh CLI with a rich description.

    Builds the PR body from the commit log, diff stat, CI results,
    and the issue context. Returns the PR URL on success.
    """
    wt = run.worktree

    # Gather commit details — compare against the upstream tracking branch
    base_ref = f"upstream/{target_branch}"
    commits = run_bash(f"git log --oneline {shlex.quote(base_ref)}..HEAD", cwd=wt, shell=True, check=False).stdout.strip()
    diff_stat = run_bash(f"git diff --stat {shlex.quote(base_ref)}..HEAD", cwd=wt, shell=True, check=False).stdout.strip()
    diff_files = run_bash(f"git diff --name-only {shlex.quote(base_ref)}..HEAD", cwd=wt, shell=True, check=False).stdout.strip()

    body_lines = [
        f"## Summary",
        f"{run.title}",
        "",
        f"**Files changed:**",
        f"```",
        diff_stat or "(no files changed)",
        f"```",
        "",
        f"**Modified files:**",
        f"```",
        diff_files or "(none)",
        f"```",
        "",
        f"## Commits",
        f"```",
        commits or "(no commits)",
        f"```",
        "",
    ]

    # Add CI results (handle empty stdout)
    ci_block = ci_stdout.strip() if ci_stdout.strip() else "(passed)"
    body_lines.extend([
        f"## CI Results (`{cfg.ci.command}`)",
        f"```",
        ci_block,
        f"```",
        "",
        f"---",
        f"Created by symphony agent (issue: {run.issue_id})",
    ])

    body = "\n".join(body_lines)
    # Use the first commit message as PR title with Symphony marker
    first_commit = commits.split("\n")[0] if commits else ""
    title = f"{first_commit} [Symphony]" if first_commit else run.title

    result = run_bash(
        f"gh pr create "
        f"--title {shlex.quote(title)} "
        f"--body {shlex.quote(body)} "
        f"--base {shlex.quote(target_branch)}",
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

def has_commits(wt_path: str, base_branch: str) -> bool:
    """Return True if the branch has new commits vs base_branch."""
    result = run_bash(f"git log --oneline {shlex.quote(base_branch)}..HEAD", cwd=wt_path, shell=True, check=False)
    return bool(result.stdout.strip())


def _base_ref(cfg) -> str:
    """Derive the local base branch ref from config (strip 'upstream/' prefix)."""
    base = cfg.git.base_branch
    if "/" in base:
        return base.split("/", 1)[1]
    return base


def reconcile(run: Run, cfg) -> None:
    """Execute the full reconcile cycle for a run.

    The agent has already implemented and committed changes. This function:
      1. Verifies gh auth
      2. Checks for new commits on the branch
      3. Runs CI on the committed code
      4. Pushes branch and creates PR
      5. Marks succeeded or failed accordingly
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

    # 2. Check for commits on the branch
    base = _base_ref(cfg)
    if not has_commits(wt, base):
        mark_failed(run, f"no new commits on branch vs {base}")
        return

    # 3. CI run — MUST use check=False so CalledProcessError isn't raised
    ci = run_bash(cfg.ci.command, cwd=wt, timeout=cfg.ci.timeout_ms // 1000, check=False)

    if ci.returncode != 0:
        mark_failed(run, f"CI failed (exit {ci.returncode}): {ci.stderr[:500]}")
        return

    # 4. Two-commit split: infrastructure vs dependencies/generated artifacts
    if cfg.git.two_commit_pr:
        commit_selective(wt, f"feat: {run.title}", exclude=cfg.git.two_commit_exclude)
        if has_pending_changes(wt):
            commit_all(wt, "chore: update dependencies and generated artifacts")
    else:
        commit_all(wt, f"feat: {run.title}")

    # 5. Push the agent's commits
    push_result = run_bash("git push -u origin HEAD", cwd=wt, shell=True)
    if push_result.returncode != 0:
        mark_failed(run, f"git push failed: {push_result.stderr[:500]}")
        return

    # 6. Create PR
    try:
        pr_url = create_pr(run, ci.stdout, cfg, target_branch=base)
    except Exception as exc:
        mark_failed(run, f"PR creation failed: {exc}")
        return

    # 7. Success
    mark_succeeded(run, pr_url=pr_url)
