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
