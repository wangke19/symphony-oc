import pytest
from datetime import datetime
from symphony_oc.state import Issue, Run
from symphony_oc.executor import slugify, can_dispatch, should_dispatch


class TestSlugify:
    def test_basic(self):
        assert slugify("Add user auth") == "add-user-auth"
    def test_special_chars_removed(self):
        assert slugify("Fix #42: null pointer!") == "fix-42-null-pointer"
    def test_max_length(self):
        long = "a" * 100
        assert len(slugify(long)) <= 60
    def test_consecutive_hyphens_collapsed(self):
        assert slugify("foo   bar---baz") == "foo-bar-baz"


class TestCanDispatch:
    def test_allow_when_under_limit(self):
        runs = [make_run("r1", "running"), make_run("r2", "succeeded")]
        assert can_dispatch(runs, max_concurrent=3) is True
    def test_deny_when_at_limit(self):
        runs = [make_run(f"r{i}", "running") for i in range(3)]
        assert can_dispatch(runs, max_concurrent=3) is False
    def test_queued_counts_as_running(self):
        runs = [make_run("r1", "running"), make_run("r2", "queued"), make_run("r3", "retrying")]
        assert can_dispatch(runs, max_concurrent=3) is False
    def test_failed_does_not_count(self):
        runs = [make_run("r1", "failed"), make_run("r2", "succeeded")]
        assert can_dispatch(runs, max_concurrent=1) is True


def make_run(issue_id: str, status: str) -> Run:
    return Run(issue_id=issue_id, title="t", branch="b", worktree="w",
               content_hash="abc", status=status, attempt=1,
               started_at=datetime.now())


class TestShouldDispatch:
    def test_github_issue_new(self):
        issue = Issue(id="GH-1", title="Fix bugs", description="fix",
                       labels=[], source="github", created_at=datetime.now())
        assert should_dispatch(issue, []) is True
    def test_github_issue_already_running(self):
        issue = Issue(id="GH-1", title="Fix bugs", description="fix",
                       labels=[], source="github", created_at=datetime.now())
        runs = [make_run("GH-1", "running")]
        assert should_dispatch(issue, runs) is False
    def test_github_issue_already_succeeded(self):
        issue = Issue(id="GH-1", title="Fix bugs", description="fix",
                       labels=[], source="github", created_at=datetime.now())
        runs = [make_run("GH-1", "succeeded")]
        assert should_dispatch(issue, runs) is False
    def test_local_issue_new(self):
        issue = Issue(id="local-001", title="Add auth", description="auth",
                       labels=[], source="local", created_at=datetime.now())
        assert should_dispatch(issue, []) is True
    def test_local_issue_same_hash_skipped(self):
        now = datetime.now()
        issue = Issue(id="local-001", title="Add auth", description="v1",
                       labels=[], source="local", created_at=now)
        runs = [make_run_with_hash("local-001", "succeeded", "v1")]
        assert should_dispatch(issue, runs) is False
    def test_local_issue_different_hash_redispatched(self):
        now = datetime.now()
        issue = Issue(id="local-001", title="Add auth", description="v2",
                       labels=[], source="local", created_at=now)
        runs = [make_run_with_hash("local-001", "succeeded", "v1")]
        assert should_dispatch(issue, runs) is True
    def test_local_issue_running_not_dispatched(self):
        now = datetime.now()
        issue = Issue(id="local-001", title="Add auth", description="v1",
                       labels=[], source="local", created_at=now)
        runs = [make_run_with_hash("local-001", "running", "v1")]
        assert should_dispatch(issue, runs) is False


def make_run_with_hash(issue_id: str, status: str, desc_version: str) -> Run:
    from symphony_oc.state import hash_issue
    issue = Issue(id=issue_id, title="t", description=desc_version,
                   labels=[], source="local", created_at=datetime.now())
    return Run(issue_id=issue_id, title="t", branch="b", worktree="w",
               content_hash=hash_issue(issue), status=status, attempt=1,
               started_at=datetime.now())


class TestGeneratePrompt:
    def test_generates_prompt(self):
        from symphony_oc.executor import generate_prompt
        issue = Issue(id="local-001", title="Add auth", description="Add login",
                       labels=[], source="local", created_at=datetime.now())
        prompt = generate_prompt(issue, ci_command="pytest -q")
        assert "local-001" in prompt
        assert "Add login" in prompt
        assert "pytest -q" in prompt

    def test_mentions_worktree(self):
        from symphony_oc.executor import generate_prompt
        issue = Issue(id="GH-42", title="Fix bug", description="fix NPE",
                       labels=[], source="github", created_at=datetime.now())
        prompt = generate_prompt(issue, ci_command="go test ./...")
        assert "GH-42" in prompt
        assert "go test" in prompt
