import pytest
from datetime import datetime, timedelta
from symphony_oc.state import Run
from symphony_oc.orchestrator import check_stalls, process_retry_queue, retry_delay


class TestRetryDelay:
    def test_exponential_backoff(self):
        assert retry_delay(1, 10_000, 60_000) == 10_000
        assert retry_delay(2, 10_000, 60_000) == 20_000
        assert retry_delay(3, 10_000, 60_000) == 40_000

    def test_capped_at_max(self):
        assert retry_delay(4, 10_000, 60_000) == 60_000
        assert retry_delay(10, 10_000, 60_000) == 60_000


class TestCheckStalls:
    def test_stalled_run_is_retried(self):
        now = datetime.now()
        old = now - timedelta(hours=1)
        run = Run(issue_id="local-001", title="T", branch="b", worktree="w",
                  content_hash="abc", status="running", attempt=1, pid=99999,
                  started_at=old)
        runs = [run]
        check_stalls(runs, stall_timeout_ms=300_000)
        assert run.status == "retrying"
        assert "stalled" in (run.error or "")

    def test_recent_run_not_stalled(self):
        now = datetime.now()
        run = Run(issue_id="local-001", title="T", branch="b", worktree="w",
                  content_hash="abc", status="running", attempt=1, pid=99999,
                  started_at=now)
        runs = [run]
        check_stalls(runs, stall_timeout_ms=300_000)
        assert run.status == "running"

    def test_non_running_not_stalled(self):
        run = Run(issue_id="local-001", title="T", branch="b", worktree="w",
                  content_hash="abc", status="succeeded", attempt=1,
                  started_at=datetime.now() - timedelta(hours=2))
        runs = [run]
        check_stalls(runs, stall_timeout_ms=300_000)
        assert run.status == "succeeded"


class TestProcessRetryQueue:
    def test_retry_due_redispatched(self, monkeypatch):
        now = datetime.now()
        run = Run(issue_id="local-001", title="T", branch="b", worktree="w",
                  content_hash="abc", status="retrying", attempt=1,
                  error="CI failed", next_retry_at=now - timedelta(minutes=1),
                  started_at=now)
        dispatched = []
        monkeypatch.setattr("symphony_oc.orchestrator._re_dispatch", lambda r: dispatched.append(r))
        process_retry_queue([run], max_retries=3)
        assert len(dispatched) == 1

    def test_retry_not_due_skipped(self, monkeypatch):
        now = datetime.now()
        run = Run(issue_id="local-001", title="T", branch="b", worktree="w",
                  content_hash="abc", status="retrying", attempt=1,
                  next_retry_at=now + timedelta(minutes=30),
                  started_at=now)
        dispatched = []
        monkeypatch.setattr("symphony_oc.orchestrator._re_dispatch", lambda r: dispatched.append(r))
        process_retry_queue([run], max_retries=3)
        assert len(dispatched) == 0

    def test_exhausted_retries_marked_failed(self):
        now = datetime.now()
        run = Run(issue_id="local-001", title="T", branch="b", worktree="w",
                  content_hash="abc", status="retrying", attempt=3,
                  error="CI failed", next_retry_at=now - timedelta(minutes=1),
                  started_at=now)
        process_retry_queue([run], max_retries=3)
        assert run.status == "failed"
        assert "exhausted" in (run.error or "")


class TestCleanupOrphans:
    def test_zombie_running_marked_retry(self):
        from symphony_oc.orchestrator import cleanup_orphans
        now = datetime.now()
        run = Run(issue_id="local-001", title="T", branch="b", worktree="./worktrees/local-001",
                   content_hash="abc", status="running", attempt=1, pid=99999,
                   started_at=now - timedelta(hours=1))
        results = cleanup_orphans([run], stall_timeout_ms=5000)
        assert len(results) > 0
        assert results[0][1] == "schedule_retry"

    def test_queued_stale_marked_failed(self):
        from symphony_oc.orchestrator import cleanup_orphans
        now = datetime.now()
        run = Run(issue_id="local-002", title="T", branch="b", worktree="./worktrees/local-002",
                   content_hash="abc", status="queued", attempt=1,
                   started_at=now - timedelta(hours=2))
        results = cleanup_orphans([run], stall_timeout_ms=5000)
        assert len(results) > 0
        assert results[0][1] == "mark_failed"
