import pytest
from datetime import datetime
from symphony_oc.state import Issue, Run, hash_issue


class TestIssue:
    def test_issue_creation(self):
        now = datetime.now()
        issue = Issue(id="local-001", title="Add auth", description="Add login",
                       labels=["feature"], source="local", created_at=now)
        assert issue.id == "local-001"
        assert issue.source == "local"

    def test_hash_issue_stable(self):
        issue = Issue(id="local-001", title="Add auth", description="Add login",
                       labels=[], source="local", created_at=datetime.now())
        h1 = hash_issue(issue)
        h2 = hash_issue(issue)
        assert h1 == h2        # deterministic

    def test_hash_issue_changes_on_description_change(self):
        now = datetime.now()
        a = Issue(id="local-001", title="T", description="v1", labels=[], source="local", created_at=now)
        b = Issue(id="local-001", title="T", description="v2", labels=[], source="local", created_at=now)
        assert hash_issue(a) != hash_issue(b)


class TestRun:
    def test_run_fields_defaults(self):
        now = datetime.now()
        run = Run(issue_id="local-001", title="Add auth", branch="symphony/local-001/add-auth",
                   worktree="./worktrees/local-001", content_hash="abc123",
                   status="running", attempt=1, pid=12345, started_at=now)
        assert run.status == "running"
        assert run.finished_at is None
        assert run.next_retry_at is None

    def test_run_serialization_roundtrip(self):
        run = Run(issue_id="local-001", title="Test", branch="b", worktree="w",
                   content_hash="abc", status="failed", attempt=2,
                   error="CI failed", started_at=datetime.now(),
                   finished_at=datetime.now())
        assert run.error == "CI failed"


class TestAtomicPersistence:
    """Test atomic file operations for state persistence."""

    def test_save_and_load_single_run(self, tmp_path):
        """Test saving a single run and loading it back."""
        from symphony_oc.state import save_run_atomic, load_all

        now = datetime.now()
        run = Run(issue_id="local-001", title="Add auth", branch="b", worktree="w",
                   content_hash="abc", status="running", attempt=1, pid=12345, started_at=now)
        runs = [run]
        path = tmp_path / "state.json"

        save_run_atomic(str(path), runs)
        loaded = load_all(str(path))

        assert len(loaded) == 1
        assert loaded[0].issue_id == "local-001"
        assert loaded[0].status == "running"
        assert loaded[0].attempt == 1

    def test_save_and_load_multiple_runs(self, tmp_path):
        """Test saving multiple runs and loading them back."""
        from symphony_oc.state import save_run_atomic, load_all

        now = datetime.now()
        runs = [
            Run(issue_id="local-001", title="Task 1", branch="b1", worktree="w1",
                content_hash="h1", status="succeeded", attempt=1, started_at=now),
            Run(issue_id="local-002", title="Task 2", branch="b2", worktree="w2",
                content_hash="h2", status="running", attempt=1, started_at=now),
            Run(issue_id="local-003", title="Task 3", branch="b3", worktree="w3",
                content_hash="h3", status="failed", attempt=3, error="timeout", started_at=now),
        ]
        path = tmp_path / "state.json"

        save_run_atomic(str(path), runs)
        loaded = load_all(str(path))

        assert len(loaded) == 3
        assert all(isinstance(r, Run) for r in loaded)

    def test_load_running_filters_correctly(self, tmp_path):
        """Test that load_running returns only running/queued/retrying runs."""
        from symphony_oc.state import save_run_atomic, load_all, load_running

        now = datetime.now()
        runs = [
            Run(issue_id="local-001", title="Running task", branch="b1", worktree="w1",
                content_hash="h1", status="running", attempt=1, started_at=now),
            Run(issue_id="local-002", title="Queued task", branch="b2", worktree="w2",
                content_hash="h2", status="queued", attempt=1, started_at=now),
            Run(issue_id="local-003", title="Retrying task", branch="b3", worktree="w3",
                content_hash="h3", status="retrying", attempt=2, started_at=now),
            Run(issue_id="local-004", title="Succeeded task", branch="b4", worktree="w4",
                content_hash="h4", status="succeeded", attempt=1, started_at=now,
                finished_at=now),
            Run(issue_id="local-005", title="Failed task", branch="b5", worktree="w5",
                content_hash="h5", status="failed", attempt=3, error="ci failed", started_at=now,
                finished_at=now),
        ]
        path = tmp_path / "state.json"
        save_run_atomic(str(path), runs)

        all_runs = load_all(str(path))
        running = load_running(all_runs)

        assert len(running) == 3
        assert "local-001" in running
        assert "local-002" in running
        assert "local-003" in running
        assert "local-004" not in running
        assert "local-005" not in running

    def test_load_all_empty_file(self, tmp_path):
        """Test loading from an empty JSON file."""
        from symphony_oc.state import save_run_atomic, load_all

        path = tmp_path / "empty_state.json"
        # Create an empty JSON file
        path.write_text("[]")

        loaded = load_all(str(path))
        assert len(loaded) == 0


class TestRunHelpers:
    """Test run state transition helpers."""

    def test_schedule_retry_sets_retry_time(self):
        """Test that schedule_retry sets next_retry_at with backoff."""
        from symphony_oc.state import schedule_retry

        now = datetime.now()
        run = Run(issue_id="local-001", title="Test", branch="b", worktree="w",
                   content_hash="h", status="failed", attempt=1, started_at=now)

        schedule_retry(run, "CI timeout", backoff_ms=5000)

        assert run.status == "retrying"
        assert run.next_retry_at is not None
        assert run.error == "CI timeout"

    def test_schedule_retry_default_backoff(self):
        """Test that schedule_retry uses default backoff of 10000ms."""
        from symphony_oc.state import schedule_retry

        now = datetime.now()
        run = Run(issue_id="local-001", title="Test", branch="b", worktree="w",
                   content_hash="h", status="failed", attempt=1, started_at=now)

        schedule_retry(run, "Error message")

        assert run.status == "retrying"
        assert run.next_retry_at is not None
        assert run.error == "Error message"

    def test_mark_failed_sets_error_and_status(self):
        """Test that mark_failed sets proper error and status."""
        from symphony_oc.state import mark_failed

        now = datetime.now()
        run = Run(issue_id="local-001", title="Test", branch="b", worktree="w",
                   content_hash="h", status="running", attempt=1, started_at=now)

        mark_failed(run, "Build failed")

        assert run.status == "failed"
        assert run.error == "Build failed"
        assert run.finished_at is not None

    def test_mark_succeeded_sets_pr_url_and_status(self):
        """Test that mark_succeeded sets PR URL and status."""
        from symphony_oc.state import mark_succeeded

        now = datetime.now()
        run = Run(issue_id="local-001", title="Add auth", branch="b", worktree="w",
                   content_hash="h", status="running", attempt=1, started_at=now)

        pr_url = "https://github.com/example/repo/pull/123"
        mark_succeeded(run, pr_url)

        assert run.status == "succeeded"
        assert run.pr_url == pr_url
        assert run.finished_at is not None


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
