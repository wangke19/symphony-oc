import fcntl
import json
import logging
import sys
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from symphony_oc.config import load_config
from symphony_oc.state import Run, schedule_retry, mark_failed, load_all, load_running, save_run_atomic
from symphony_oc.subproc import interrupt_process
from symphony_oc.executor import dispatch
from symphony_oc.reconciler import reconcile
from symphony_oc.issue_source.local import LocalIssueSource
from symphony_oc.reviewer import dispatch_review, dispatch_fix, parse_review_result


def retry_delay(attempt: int, backoff_ms: int = 10_000, max_backoff_ms: int = 60_000) -> int:
    """Calculate exponential backoff delay for retry attempts.

    Args:
        attempt: The attempt number (1-indexed)
        backoff_ms: Base backoff time in milliseconds
        max_backoff_ms: Maximum backoff cap in milliseconds

    Returns:
        Delay in milliseconds (exponential backoff, capped at max)
    """
    return min(backoff_ms * (2 ** (attempt - 1)), max_backoff_ms)


def check_stalls(runs: list[Run], stall_timeout_ms: int = 1_800_000) -> None:
    """Detect stalled runs and move them to retry queue.

    A run is considered stalled if:
    - Status is "running"
    - Elapsed time since start exceeds stall_timeout_ms

    Stalled runs are interrupted and scheduled for retry.

    Args:
        runs: List of Run instances to check
        stall_timeout_ms: Timeout in milliseconds before a run is considered stalled
    """
    now = datetime.now()
    for run in runs:
        if run.status != "running":
            continue
        elapsed = (now - run.started_at).total_seconds() * 1000
        if elapsed > stall_timeout_ms:
            interrupt_process(run.pid)
            schedule_retry(run, f"stalled: exceeded {stall_timeout_ms}ms", backoff_ms=retry_delay(run.attempt))


def process_retry_queue(runs: list[Run], max_retries: int = 3,
                        now: Optional[datetime] = None) -> None:
    """Process runs that are waiting to be retried.

    For each run in "retrying" status:
    - If next_retry_at has passed, dispatch for retry
    - If max retries exhausted, mark as failed

    Args:
        runs: List of Run instances to process
        max_retries: Maximum number of retry attempts allowed
        now: Current datetime (for testing, defaults to datetime.now())
    """
    if now is None:
        now = datetime.now()
    for run in runs:
        if run.status != "retrying":
            continue
        if run.next_retry_at and now < run.next_retry_at:
            continue
        if run.attempt >= max_retries:
            mark_failed(run, f"exhausted {max_retries} retries (last: {run.error})")
            continue
        _re_dispatch(run)


def _re_dispatch(run: Run) -> None:
    """Re-dispatch a run for retry.

    Increments attempt counter, resets status to running,
    and clears retry scheduling fields.

    Args:
        run: The Run instance to re-dispatch (modified in place)
    """
    run.attempt += 1
    run.status = "running"
    run.started_at = datetime.now()
    run.finished_at = None
    run.next_retry_at = None
    run.error = None


def cleanup_orphans(runs: list[Run], stall_timeout_ms: int = 1_800_000,
                    worktree_root: str = "./worktrees") -> list[tuple[str, str]]:
    """Detect and handle orphan/stale processes.

    For running runs with dead PIDs: interrupt and schedule retry.
    For queued/retrying runs that have exceeded stall_timeout_ms: mark as failed.

    Args:
        runs: List of Run instances to check
        stall_timeout_ms: Timeout in milliseconds before a run is considered orphaned
        worktree_root: Root directory for worktrees (reserved for future use)

    Returns:
        List of (issue_id, action) tuples describing actions taken
    """
    actions = []
    now = datetime.now()
    for r in runs:
        if r.status == "running" and not _pid_exists_simple(r.pid):
            schedule_retry(r, "stalled: pid dead after restart")
            actions.append((r.issue_id, "schedule_retry"))
        elif r.status in ("queued", "retrying"):
            elapsed = (now - r.started_at).total_seconds() * 1000
            if elapsed > stall_timeout_ms:
                mark_failed(r, f"orphan after crash (status={r.status})")
                actions.append((r.issue_id, "mark_failed"))
    return actions


def _pid_exists_simple(pid: int | None) -> bool:
    """Check if a process with the given PID exists, without signal overhead.

    Also reaps child zombie processes via waitpid, so that completed
    agents are properly detected as dead rather than lingering as defunct.
    """
    if pid is None or pid <= 0:
        return False
    try:
        import os
        # Try to reap child zombie first
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False  # Successfully reaped, process is gone
        # waitpid returned (0, 0) — still running, confirm with kill
        os.kill(pid, 0)
        return True
    except ChildProcessError:
        return False  # Not our child or already reaped
    except (OSError, ProcessLookupError):
        return False


logger = logging.getLogger("symphony-oc")


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


def main_loop(cfg) -> None:
    """Main orchestrator loop with file locking for safe concurrent state access."""
    logger.info("orchestrator starting")
    issues_sources = [LocalIssueSource(cfg.tracker.local_dir)]
    state_path = Path("state/runs.jsonc")
    state_path.parent.mkdir(exist_ok=True)
    lock_path = Path("state/.lock")
    while True:
        try:
            with open(lock_path, "w") as lock_f:
                fcntl.flock(lock_f, fcntl.LOCK_EX)
                issues = []
                for src in issues_sources:
                    issues.extend(src.fetch_issues())
                runs = load_all(str(state_path))
                running = load_running(runs)
                for issue in issues:
                    from symphony_oc.executor import should_dispatch, can_dispatch
                    if should_dispatch(issue, runs) and can_dispatch(runs, cfg.agent.max_concurrent):
                        new_run = dispatch(issue, cfg)
                        if new_run:
                            runs.append(new_run)
                            logger.info("dispatched %s", issue.id)
                check_stalls(runs, cfg.agent.stall_timeout_ms)
                process_completed(runs, cfg)
                process_retry_queue(runs, cfg.agent.max_retries)
                save_run_atomic(str(state_path), runs)
        except Exception as e:
            logger.exception("main loop error: %s", e)
        time_module.sleep(cfg.polling_interval_ms / 1000.0)


def main() -> None:
    """Entry point for the orchestrator CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    cfg = load_config("WORKFLOW.md")
    logger.info("config loaded: ci=%s, agent=%s", cfg.ci.command, cfg.agent.name)
    main_loop(cfg)


if __name__ == "__main__":
    main()
