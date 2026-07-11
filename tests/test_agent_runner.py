import subprocess
import tempfile
from unittest.mock import MagicMock, patch, mock_open
from symphony_oc.agent_runner import _spawn_agent


class TestSpawnAgent:
    def test_command_construction(self):
        """Verify the exact argv passed to subprocess.Popen."""
        with patch("symphony_oc.agent_runner.subprocess.Popen") as mock_popen, \
             patch("builtins.open", mock_open()) as mock_file:
            mock_popen.return_value = MagicMock(pid=12345)
            _spawn_agent(
                agent="symphony-reviewer",
                wt_path="/tmp/wt",
                extra_args=["--model", "anthropic/claude-opus"],
                prompt_path="/tmp/wt/.san/review/review-1.prompt",
                log_path="/tmp/log/review-1.log",
            )
        args, kwargs = mock_popen.call_args
        cmd = args[0] if args else kwargs.get("cmd")
        assert cmd[0] == "opencode"
        assert cmd[1] == "run"
        assert "--agent" in cmd
        assert "symphony-reviewer" in cmd
        assert "--dir" in cmd
        assert "/tmp/wt" in cmd
        assert "--model" in cmd
        assert "anthropic/claude-opus" in cmd
        assert "/tmp/wt/.san/review/review-1.prompt" in cmd
        # Must NOT include --dangerously-skip-permissions
        assert "--dangerously-skip-permissions" not in cmd

    def test_start_new_session_true(self):
        """Subprocess must be in its own session for clean SIGKILL of process group."""
        with patch("symphony_oc.agent_runner.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(pid=1)
            _spawn_agent("a", "/w", [], "p", "l")
        _, kwargs = mock_popen.call_args
        assert kwargs.get("start_new_session") is True

    def test_returns_popen_with_pid(self):
        """Return value must expose .pid for the caller to record."""
        fake_proc = MagicMock(pid=99999)
        with patch("symphony_oc.agent_runner.subprocess.Popen", return_value=fake_proc):
            proc = _spawn_agent("a", "/w", [], "p", "l")
        assert proc.pid == 99999
