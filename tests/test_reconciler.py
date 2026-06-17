import pytest
from symphony_oc.reconciler import commit_all, commit_selective, has_pending_changes, cleanup_worktree, GhAuthExpired, assert_gh_auth


class TestCommitHelpers:
    def test_commit_all_calls_git(self, monkeypatch):
        cmds = []
        monkeypatch.setattr("symphony_oc.reconciler.run_bash", lambda c, **kw: cmds.append(c))
        commit_all("/tmp/wt", "feat: test")
        assert any("git add -A" in c for c in cmds)
        assert any("git commit" in c for c in cmds)
        assert any("feat: test" in c for c in cmds)

    def test_has_pending_changes(self, monkeypatch):
        monkeypatch.setattr("symphony_oc.reconciler.run_bash",
                            lambda *args, **kw: type("R", (), {"stdout": " M file.py\n", "returncode": 0})())
        assert has_pending_changes("/tmp/wt") is True

    def test_no_pending_changes(self, monkeypatch):
        monkeypatch.setattr("symphony_oc.reconciler.run_bash",
                            lambda *args, **kw: type("R", (), {"stdout": "", "returncode": 0})())
        assert has_pending_changes("/tmp/wt") is False

    def test_commit_selective_excludes_pattern(self, monkeypatch):
        cmds = []
        monkeypatch.setattr("symphony_oc.reconciler.run_bash", lambda c, **kw: cmds.append(c))
        commit_selective("/tmp/wt", "feat: code", exclude=["*.lock", "vendor/"])
        add_cmds = [c for c in cmds if "git add" in str(c)]
        assert len(add_cmds) > 0

    def test_cleanup_worktree(self, monkeypatch):
        cmds = []
        monkeypatch.setattr("symphony_oc.reconciler.run_bash", lambda c, **kw: cmds.append(c))
        monkeypatch.setattr("symphony_oc.reconciler.os.path.isdir", lambda p: True)
        from symphony_oc.state import Run
        from datetime import datetime
        run = Run(issue_id="local-001", title="T", branch="symphony/local-001/test",
                   worktree="./worktrees/local-001", content_hash="abc",
                   status="succeeded", attempt=1, started_at=datetime.now())
        cleanup_worktree(run)
        assert any("worktree remove" in c for c in cmds)
        assert any("branch -D" in c for c in cmds)


class TestGhAuthExpired:
    def test_assert_gh_auth_passes(self, monkeypatch):
        monkeypatch.setattr("symphony_oc.reconciler.run_bash",
                            lambda *args, **kw: type("R", (), {"returncode": 0})())
        assert_gh_auth()

    def test_assert_gh_auth_raises(self, monkeypatch):
        monkeypatch.setattr("symphony_oc.reconciler.run_bash",
                            lambda *args, **kw: type("R", (), {"returncode": 1})())
        with pytest.raises(GhAuthExpired):
            assert_gh_auth()
