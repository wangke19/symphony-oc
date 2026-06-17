import pytest
from pathlib import Path
from symphony_oc.bootstrap import (
    check_opencode_version,
    check_external_tool,
    check_git_remote,
    check_installed_agent_hash,
    MIN_OPENCODE_VERSION,
)


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
