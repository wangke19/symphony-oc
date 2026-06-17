import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
import hashlib
from typing import Optional


@dataclass
class Issue:
    id: str
    title: str
    description: str
    labels: list[str]
    source: str                    # "local" | "github"
    created_at: datetime


@dataclass
class Run:
    issue_id: str
    title: str
    branch: str
    worktree: str
    content_hash: str
    status: str                    # "queued" | "running" | "succeeded" | "failed" | "retrying"
    attempt: int
    pid: Optional[int] = None
    error: Optional[str] = None
    pr_url: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None


def hash_issue(issue: Issue) -> str:
    """SHA256[:12] of issue description for dedup (see Section 5.7)."""
    return hashlib.sha256(issue.description.encode()).hexdigest()[:12]


def _run_to_dict(run: Run) -> dict:
    """Convert Run dataclass to dict for JSON serialization."""
    d = asdict(run)
    # Convert datetime objects to ISO strings
    for key, value in d.items():
        if isinstance(value, datetime):
            d[key] = value.isoformat()
    return d


def _dict_to_run(d: dict) -> Run:
    """Convert dict back to Run dataclass."""
    # Convert ISO strings back to datetime objects
    for key in ['started_at', 'finished_at', 'next_retry_at']:
        if key in d and d[key]:
            d[key] = datetime.fromisoformat(d[key])
    return Run(**d)


def save_run_atomic(path: str, runs: list[Run]) -> None:
    """Save runs to JSON file atomically via tmp file + rename.

    Atomic write ensures that concurrent reads never see partial data.
    Uses tempfile.mkstemp for tmp file creation, then os.rename for atomic swap.
    """
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else '.', exist_ok=True)

    # Convert runs to serializable format
    data = [_run_to_dict(r) for r in runs]

    # Write to temp file in same directory (for atomic rename)
    dir_name = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.json.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
        # Atomic rename
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_all(path: str) -> list[Run]:
    """Load all runs from JSON file.

    Returns empty list if file doesn't exist.
    Raises ValueError for invalid JSON or malformed data.
    """
    if not os.path.exists(path):
        return []

    with open(path, 'r') as f:
        data = json.load(f)

    return [_dict_to_run(d) for d in data]


def load_running(runs: list[Run]) -> dict[str, Run]:
    """Filter runs to only those in active states (running/queued/retrying).

    Returns dict keyed by issue_id for easy lookup.
    """
    active_statuses = {'running', 'queued', 'retrying'}
    return {r.issue_id: r for r in runs if r.status in active_statuses}


def schedule_retry(run: Run, error: str, backoff_ms: int = 10_000) -> None:
    """Schedule a retry for a failed run with exponential backoff.

    Sets run status to 'retrying', updates error message,
    and calculates next retry time based on backoff_ms.

    Args:
        run: The Run instance to update (modified in place)
        error: Error message to record
        backoff_ms: Backoff time in milliseconds (default: 10000ms = 10s)
    """
    run.status = 'retrying'
    run.error = error
    run.next_retry_at = datetime.now() + timedelta(milliseconds=backoff_ms)


def mark_failed(run: Run, error: str) -> None:
    """Mark a run as failed with the given error.

    Sets status to 'failed', records error, and sets finished_at.

    Args:
        run: The Run instance to update (modified in place)
        error: Error message to record
    """
    run.status = 'failed'
    run.error = error
    run.finished_at = datetime.now()


def mark_succeeded(run: Run, pr_url: str) -> None:
    """Mark a run as succeeded with the PR URL.

    Sets status to 'succeeded', records PR URL, and sets finished_at.

    Args:
        run: The Run instance to update (modified in place)
        pr_url: URL of the created pull request
    """
    run.status = 'succeeded'
    run.pr_url = pr_url
    run.finished_at = datetime.now()
