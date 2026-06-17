import pytest
from datetime import datetime
from pathlib import Path
from symphony_oc.issue_source import Issue
from symphony_oc.issue_source.local import LocalIssueSource

SAMPLE_ISSUE = """---
title: "Add user authentication"
labels: ["feature", "auth"]
---

Implement login with email and password.

## Acceptance Criteria

- User can register
- User can log in
"""

class TestLocalIssueSource:
    def test_read_single_issue(self, tmp_path: Path):
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / "add-auth.md").write_text(SAMPLE_ISSUE)
        source = LocalIssueSource(issues_dir=str(issues_dir))
        issues = source.fetch_issues()
        assert len(issues) == 1
        assert issues[0].title == "Add user authentication"
        assert issues[0].labels == ["feature", "auth"]
        assert issues[0].source == "local"

    def test_skip_prompt_files(self, tmp_path: Path):
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / "valid.md").write_text(SAMPLE_ISSUE)
        (issues_dir / "archived.prompt").write_text("just a prompt")
        source = LocalIssueSource(issues_dir=str(issues_dir))
        issues = source.fetch_issues()
        assert len(issues) == 1

    def test_empty_dir(self, tmp_path: Path):
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        source = LocalIssueSource(issues_dir=str(issues_dir))
        issues = source.fetch_issues()
        assert issues == []

    def test_issue_without_frontmatter(self, tmp_path: Path):
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / "no-fm.md").write_text("Just a description without frontmatter")
        source = LocalIssueSource(issues_dir=str(issues_dir))
        issues = source.fetch_issues()
        assert len(issues) == 1
        assert issues[0].title == "No Fm"
        assert issues[0].labels == []

    def test_auto_id_increment(self, tmp_path: Path):
        issues_dir = tmp_path / "issues"
        issues_dir.mkdir()
        (issues_dir / "a.md").write_text(SAMPLE_ISSUE)
        (issues_dir / "b.md").write_text(SAMPLE_ISSUE)
        source = LocalIssueSource(issues_dir=str(issues_dir))
        issues = source.fetch_issues()
        assert issues[0].id.startswith("local-")
        assert issues[1].id.startswith("local-")
        assert issues[0].id != issues[1].id
