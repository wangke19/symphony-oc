"""Subprocess launcher for opencode agents.

Extracted from executor.dispatch so reviewer.py can reuse the same
launch logic without duplicating subprocess plumbing.
"""

import subprocess


def _spawn_agent(agent: str, wt_path: str, extra_args: list[str],
                 prompt_path: str, log_path: str) -> subprocess.Popen:
    """Start `opencode run` for the given agent and return the Popen.

    - start_new_session=True so SIGKILL hits the whole process group
    - stdout redirected to log_path
    - Never passes --dangerously-skip-permissions (agent boundary is enforced
      by the agent definition itself)
    """
    cmd = ["opencode", "run", "--agent", agent, "--dir", wt_path,
           *extra_args, prompt_path]
    return subprocess.Popen(
        cmd,
        start_new_session=True,
        stdout=open(log_path, "wb"),
        stderr=subprocess.STDOUT,
    )
