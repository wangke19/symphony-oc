import pytest
from pathlib import Path
from unittest.mock import patch
from symphony_oc.bootstrap import (
    BootError,
    check_opencode_version,
    check_external_tool,
    check_git_remote,
    check_installed_agent_hash,
    MIN_OPENCODE_VERSION,
    check_reviewer_model,
    _list_opencode_models,
)


class TestCheckReviewerModel:
    def test_min_gt_max_raises_boot_error(self, tmp_path):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=5, max_iterations=3, extra_args=[],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap.REPO_ROOT", tmp_path):
                with patch("symphony_oc.bootstrap._list_opencode_models",
                           return_value=[]):
                    with pytest.raises(BootError):
                        check_reviewer_model()

    def test_min_eq_max_ok(self):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=3, max_iterations=3,
                    extra_args=["--model", "x"],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap._list_opencode_models",
                       return_value=[]):
                check_reviewer_model()  # must not raise

    def test_missing_model_with_providers_warns_not_raises(self, capsys):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=3, max_iterations=5, extra_args=[],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap._list_opencode_models",
                       return_value=["anthropic/claude-opus", "bigmodel/coding"]):
                check_reviewer_model()  # must not raise
            captured = capsys.readouterr()
            assert "未指定 --model" in captured.out

    def test_model_flag_present_silent(self, capsys):
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            agent=SimpleNamespace(
                reviewer=SimpleNamespace(
                    min_iterations=3, max_iterations=5,
                    extra_args=["--model", "anthropic/claude-opus"],
                ),
            ),
        )
        with patch("symphony_oc.bootstrap.load_config", return_value=cfg):
            with patch("symphony_oc.bootstrap._list_opencode_models",
                       return_value=["anthropic/claude-opus"]):
                check_reviewer_model()
            captured = capsys.readouterr()
            assert "未指定 --model" not in captured.out


class TestCheckOpenCodeVersion:
    def test_accepts_valid_version(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: type("R", (), {
            "stdout": f"opencode {MIN_OPENCODE_VERSION[0]}.{MIN_OPENCODE_VERSION[1]}.{MIN_OPENCODE_VERSION[2]}",
            "returncode": 0,
        })())
        check_opencode_version()

    def test_rejects_old_version(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: type("R", (), {
            "stdout": "opencode 1.16.0",
            "returncode": 0,
        })())
        with pytest.raises(RuntimeError, match="opencode"):
            check_opencode_version()


class TestCheckExternalTool:
    def test_accepts_existing_tool(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/git")
        check_external_tool("git")

    def test_rejects_missing_tool(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda x: None)
        with pytest.raises(RuntimeError, match="not found"):
            check_external_tool("nonexistent")


class TestCheckInstalledAgentHash:
    def test_matches_when_same(self, tmp_path: Path):
        bundled = tmp_path / "agents/symphony-worker.md"
        bundled.parent.mkdir()
        bundled.write_text("same content")
        installed = tmp_path / "installed/symphony-worker.md"
        installed.parent.mkdir()
        installed.write_text("same content")
        check_installed_agent_hash(str(bundled), str(installed))

    def test_raises_on_mismatch(self, tmp_path: Path):
        bundled = tmp_path / "agents/symphony-worker.md"
        bundled.parent.mkdir()
        bundled.write_text("content A")
        installed = tmp_path / "installed/symphony-worker.md"
        installed.parent.mkdir()
        installed.write_text("content B")
        with pytest.raises(RuntimeError, match="mismatch"):
            check_installed_agent_hash(str(bundled), str(installed))

    def test_raises_on_missing_installed(self, tmp_path: Path):
        bundled = tmp_path / "agents/symphony-worker.md"
        bundled.parent.mkdir()
        bundled.write_text("content")
        with pytest.raises(RuntimeError, match="not found"):
            check_installed_agent_hash(str(bundled), str(tmp_path / "nonexistent"))
