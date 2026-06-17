import os
import signal
import subprocess
import shlex


def run_bash(cmd: str | list[str], cwd: str | None = None,
             timeout: int | None = None, check: bool = True,
             shell: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    if isinstance(cmd, str) and not shell:
        cmd = shlex.split(cmd)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=check,
        shell=shell,
        env=env,
    )


def interrupt_process(pid: int) -> None:
    if pid and pid > 0:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def is_pid_alive(pid: int | None) -> bool:
    if pid is None or pid < 0:
        return False
    if pid == 0:
        return True  # PID 0 is always alive (kernel/swapper process)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
