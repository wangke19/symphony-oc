import subprocess
import pytest
from symphony_oc.subproc import run_bash, is_pid_alive


class TestRunBash:
    def test_simple_command(self):
        result = run_bash("echo hello", timeout=5)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_captures_stderr(self):
        result = run_bash("echo err >&2", timeout=5, shell=True)
        assert "err" in result.stderr

    def test_raises_on_nonzero(self):
        with pytest.raises(subprocess.CalledProcessError):
            run_bash("exit 1", timeout=5, shell=True)

    def test_nonzero_with_check_false(self):
        result = run_bash("exit 1", timeout=5, check=False, shell=True)
        assert result.returncode == 1

    def test_timeout(self):
        with pytest.raises(subprocess.TimeoutExpired):
            run_bash("sleep 10", timeout=1)


class TestIsPidAlive:
    def test_current_process_alive(self):
        assert is_pid_alive(0)

    def test_none_pid(self):
        assert is_pid_alive(None) is False

    def test_negative_pid(self):
        assert is_pid_alive(-1) is False
