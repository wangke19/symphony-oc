"""Pre-flight checks + agent install. Idempotent. Safe to re-run."""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

MIN_OPENCODE_VERSION = (1, 17, 7)
AGENT_NAME = "symphony-worker"
AGENT_INSTALL_DIR = Path.home() / ".config/opencode/agents"
AGENT_INSTALL_PATH = AGENT_INSTALL_DIR / f"{AGENT_NAME}.md"
BUNDLED_AGENT = Path(__file__).parent.parent / "agents" / f"{AGENT_NAME}.md"
REPO_ROOT = Path(__file__).parent.parent


class BootError(RuntimeError):
    """Pre-flight failure. Orchestrator must not start."""


def main() -> int:
    checks = [
        check_opencode_version,
        check_external_tools,
        check_git_remote,
        install_agent,
        verify_agent_discoverable,
        init_workspace,
        smoke_test_agent,
    ]
    for check in checks:
        try:
            check()
            print(f"  ✓ {check.__name__}")
        except BootError as e:
            print(f"  ✗ {check.__name__}: {e}", file=sys.stderr)
            return 1
    print("bootstrap complete")
    return 0


def check_opencode_version() -> None:
    out = subprocess.run(
        ["opencode", "--version"],
        capture_output=True, text=True, check=True,
    )
    version_str = out.stdout.strip().split()[-1]
    parts = tuple(int(x) for x in version_str.split("."))
    if parts < MIN_OPENCODE_VERSION:
        raise BootError(
            f"opencode {version_str} < required {'.'.join(map(str, MIN_OPENCODE_VERSION))}"
        )


def check_external_tools() -> None:
    for tool in ("gh", "git"):
        if not shutil.which(tool):
            raise BootError(f"{tool} not in PATH")


def check_external_tool(tool: str) -> None:
    """Check a single external tool is available."""
    if not shutil.which(tool):
        raise RuntimeError(f"tool '{tool}' not found in PATH")


def check_git_remote() -> None:
    out = subprocess.run(
        ["git", "remote"],
        capture_output=True, text=True, check=True, cwd=REPO_ROOT,
    )
    if "upstream" not in out.stdout.split():
        raise BootError(
            "git remote 'upstream' missing — run: git remote add upstream <url>"
        )


def install_agent() -> None:
    """Copy bundled agents/symphony-worker.md → ~/.config/opencode/agents/.
    Skip if installed copy's sha256 matches bundled file (idempotent)."""
    bundled_hash = hashlib.sha256(BUNDLED_AGENT.read_bytes()).hexdigest()

    AGENT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if AGENT_INSTALL_PATH.exists():
        installed_hash = hashlib.sha256(AGENT_INSTALL_PATH.read_bytes()).hexdigest()
        if installed_hash == bundled_hash:
            return
    AGENT_INSTALL_PATH.write_bytes(BUNDLED_AGENT.read_bytes())


def verify_agent_discoverable() -> None:
    """Key check — opencode would silently fall back to full-permission default
    if agent is not discoverable. Refuse to start if not found."""
    out = subprocess.run(
        ["opencode", "agent", "list"],
        capture_output=True, text=True, check=True,
    )
    if AGENT_NAME not in out.stdout:
        raise BootError(
            f"agent '{AGENT_NAME}' not in `opencode agent list` output. "
            "Permission boundary would be bypassed — refusing to start."
        )


def init_workspace() -> None:
    for d, ignore in [
        ("state", "*\n!.gitignore\n"),
        ("log", "*\n!.gitignore\n"),
        ("worktrees", "*\n!.gitignore\n"),
        ("issues", "*.prompt\n!.gitignore\n"),
    ]:
        path = REPO_ROOT / d
        path.mkdir(exist_ok=True)
        gi = path / ".gitignore"
        if not gi.exists():
            gi.write_text(ignore)

    runs = REPO_ROOT / "state/runs.jsonc"
    if not runs.exists():
        runs.write_text('{\n  "runs": [],\n  "last_poll": null\n}\n')


def smoke_test_agent() -> None:
    """Start opencode for 2s, scan output for 'not found' / 'Falling back'."""
    proc = subprocess.Popen(
        ["opencode", "run", "--agent", AGENT_NAME,
         "--dir", "/tmp", "--format", "json", "exit immediately"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()

    text = out.decode("utf-8", errors="replace").lower()
    if "not found" in text or "falling back" in text:
        raise BootError(
            f"smoke test detected agent fall-back. Output:\n{text[:500]}"
        )


def check_installed_agent_hash(bundled_path: str, installed_path: str) -> None:
    """Compare sha256 hash of bundled agent vs installed agent.
    Raises RuntimeError if installed file not found or hashes mismatch."""
    bundled = Path(bundled_path)
    installed = Path(installed_path)

    if not installed.exists():
        raise RuntimeError(f"installed agent not found: {installed_path}")

    bundled_hash = hashlib.sha256(bundled.read_bytes()).hexdigest()
    installed_hash = hashlib.sha256(installed.read_bytes()).hexdigest()

    if bundled_hash != installed_hash:
        raise RuntimeError("agent hash mismatch — bundled and installed differ")


if __name__ == "__main__":
    sys.exit(main())
