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


class TestOnWorkerDone:
    def test_calls_dispatch_review(self, monkeypatch):
        from symphony_oc.orchestrator import _on_worker_done
        from symphony_oc.state import Run
        from types import SimpleNamespace
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1, pid=1,
                  started_at=datetime.now())
        captured = {}
        monkeypatch.setattr("symphony_oc.orchestrator.dispatch_review",
                            lambda r, c, b, p: captured.setdefault("called", (r, b, p)))
        cfg = SimpleNamespace(git=SimpleNamespace(base_branch="upstream/main"))
        _on_worker_done(run, cfg=cfg)
        assert "called" in captured

    def test_dispatch_failure_marks_run_failed(self, monkeypatch):
        from symphony_oc.orchestrator import _on_worker_done
        from symphony_oc.state import Run
        from types import SimpleNamespace
        run = Run(issue_id="i", title="t", branch="b", worktree="w",
                  content_hash="h", status="running", attempt=1, pid=1,
                  started_at=datetime.now())
        def boom(*args, **kw):
            raise RuntimeError("spawn failed")
        monkeypatch.setattr("symphony_oc.orchestrator.dispatch_review", boom)
        cfg = SimpleNamespace(git=SimpleNamespace(base_branch="upstream/main"))
        _on_worker_done(run, cfg=cfg)
        assert run.status == "failed"
        assert "spawn failed" in (run.error or "")


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
