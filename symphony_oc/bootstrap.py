"""Pre-flight checks + agent install. Idempotent. Safe to re-run."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from symphony_oc.config import load_config

MIN_OPENCODE_VERSION = (1, 17, 7)
AGENT_NAME = "symphony-worker"
REVIEWER_NAME = "symphony-reviewer"
AGENT_INSTALL_DIR = Path.home() / ".config/opencode/agents"
AGENT_INSTALL_PATH = AGENT_INSTALL_DIR / f"{AGENT_NAME}.md"
REVIEWER_INSTALL_PATH = AGENT_INSTALL_DIR / f"{REVIEWER_NAME}.md"
BUNDLED_AGENT = Path(__file__).parent.parent / "agents" / f"{AGENT_NAME}.md"
BUNDLED_REVIEWER = Path(__file__).parent.parent / "agents" / f"{REVIEWER_NAME}.md"
REPO_ROOT = Path(__file__).parent.parent


class BootError(RuntimeError):
    """Pre-flight failure. Orchestrator must not start."""


def main() -> int:
    checks = [
        ("check_opencode_version", check_opencode_version),
        ("check_external_tools", check_external_tools),
        ("check_git_remote", check_git_remote),
        ("install_agent", install_agent),
        ("install_reviewer_agent", install_reviewer_agent),
        ("verify_agent_discoverable", verify_agent_discoverable),
        ("verify_reviewer_discoverable", verify_reviewer_discoverable),
        ("check_providers", check_providers),
        ("init_workspace", init_workspace),
        ("check_reviewer_model", check_reviewer_model),
        ("smoke_test_agent", smoke_test_agent),
    ]
    all_ok = True
    for name, fn in checks:
        try:
            fn()
            print(f"  ✓ {name}")
        except BootError as e:
            print(f"  ✗ {name}: {e}", file=sys.stderr)
            all_ok = False
    if all_ok:
        print("bootstrap complete")
        return 0
    print("bootstrap failed — fix issues and re-run", file=sys.stderr)
    return 1


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


# ---------------------------------------------------------------------------
# Agent install
# ---------------------------------------------------------------------------

def _install_agent(name: str, bundled: Path, installed: Path) -> None:
    bundled_hash = hashlib.sha256(bundled.read_bytes()).hexdigest()
    AGENT_INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if installed.exists():
        installed_hash = hashlib.sha256(installed.read_bytes()).hexdigest()
        if installed_hash == bundled_hash:
            return
    installed.write_bytes(bundled.read_bytes())


def install_agent() -> None:
    _install_agent(AGENT_NAME, BUNDLED_AGENT, AGENT_INSTALL_PATH)


def install_reviewer_agent() -> None:
    if BUNDLED_REVIEWER.exists():
        _install_agent(REVIEWER_NAME, BUNDLED_REVIEWER, REVIEWER_INSTALL_PATH)


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


def verify_reviewer_discoverable() -> None:
    """Verify reviewer agent is discoverable (soft warning, not fatal)."""
    if not BUNDLED_REVIEWER.exists():
        print("  ⚠ agents/symphony-reviewer.md not bundled, skipping reviewer check")
        return
    out = subprocess.run(
        ["opencode", "agent", "list"],
        capture_output=True, text=True, check=True,
    )
    if REVIEWER_NAME not in out.stdout:
        print(
            "  ⚠ reviewer agent not installed — run `install_reviewer_agent()` or re-bootstrap"
        )


# ---------------------------------------------------------------------------
# Provider / API key checks
# ---------------------------------------------------------------------------

# Required providers and their env vars
REQUIRED_PROVIDERS: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "bigmodel": ["BIGMODEL_API_KEY", "BIGMODEL_CODING_API_KEY"],
}


def check_providers() -> None:
    """Check that LLM providers used by the orchestrator are available.

    Checks two things:
      1. Required env vars are set (export API_KEY=...)
      2. opencode.jsonc has matching provider entries (if the file exists)

    The orchestrator needs at minimum:
      - A provider for the worker agent (e.g. deepseek, anthropic)
      - bigmodel/coding provider for the reviewer agent
    """
    missing = []
    for provider, env_vars in REQUIRED_PROVIDERS.items():
        has_var = any(os.environ.get(var) for var in env_vars)
        has_provider = _opencode_has_provider(provider)
        if has_var or has_provider:
            continue
        # bigmodel is optional if reviewer features aren't used
        if provider == "bigmodel":
            print(
                f"  ⚠ {provider}: set {' or '.join(env_vars)} env var "
                "or configure in opencode.jsonc (needed for review loop)"
            )
            continue
        missing.append(f"{provider} ({' or '.join(env_vars)})")

    if missing:
        raise BootError(
            "missing provider config. Set one of these env vars:\n  "
            + "\n  ".join(missing)
            + "\nOr configure providers in ~/.config/opencode/opencode.jsonc"
        )


def _opencode_has_provider(provider_name: str) -> bool:
    """Check if a provider is configured in opencode.jsonc."""
    config_paths = [
        Path.home() / ".config/opencode/opencode.jsonc",
        REPO_ROOT / ".opencode.jsonc",
    ]
    for path in config_paths:
        if not path.exists():
            continue
        try:
            text = path.read_text()
            text = re.sub(r"//.*", "", text)  # strip comments for JSONC
            data = json.loads(text)
        except (json.JSONDecodeError, OSError):
            continue
        # opencode.jsonc has providers at top level or under llm
        providers = data.get("providers", data.get("llm", {}).get("providers", {}))
        if provider_name in providers:
            return True
    return False


# ---------------------------------------------------------------------------
# Workspace init
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

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


def _list_opencode_models() -> list[str]:
    """Best-effort scan of opencode config for available provider/model ids.

    Returns [] if nothing parseable is found — caller treats as 'no providers'.
    """
    config_path = Path.home() / ".config/opencode/opencode.jsonc"
    if not config_path.exists():
        return []
    try:
        text = config_path.read_text()
        text = re.sub(r"//.*", "", text)  # strip JSONC comments
        data = json.loads(text)
    except (json.JSONDecodeError, OSError):
        return []
    providers = data.get("providers", {})
    out = []
    for provider_name, provider_cfg in providers.items():
        if isinstance(provider_cfg, dict):
            models = provider_cfg.get("models", {})
            if isinstance(models, dict):
                for model_id in models:
                    out.append(f"{provider_name}/{model_id}")
    return out


def check_installed_agent_hash(bundled_path: str, installed_path: str) -> None:
    """Compare sha256 hash of bundled agent vs installed agent.
    Raises RuntimeError if installed file not found or hashes differ."""
    bundled = Path(bundled_path)
    installed = Path(installed_path)

    if not installed.exists():
        raise RuntimeError(f"installed agent not found: {installed_path}")

    bundled_hash = hashlib.sha256(bundled.read_bytes()).hexdigest()
    installed_hash = hashlib.sha256(installed.read_bytes()).hexdigest()

    if bundled_hash != installed_hash:
        raise RuntimeError("agent hash mismatch — bundled and installed differ")


def check_reviewer_model() -> None:
    """Validate reviewer config + warn about missing --model."""
    cfg = load_config(REPO_ROOT / "WORKFLOW.md")

    if cfg.agent.reviewer.min_iterations > cfg.agent.reviewer.max_iterations:
        raise BootError(
            f"agent.reviewer.min_iterations ({cfg.agent.reviewer.min_iterations}) "
            f"> max_iterations ({cfg.agent.reviewer.max_iterations}). "
            f"决策表无法收敛 — 请调整 WORKFLOW.md。"
        )

    args = cfg.agent.reviewer.extra_args
    try:
        idx = args.index("--model")
        if idx + 1 < len(args):
            return  # explicit --model <value>
    except ValueError:
        pass

    available = _list_opencode_models()
    if available:
        preview = ", ".join(available[:5]) + (" ..." if len(available) > 5 else "")
        print(
            f"  ⚠ reviewer extra_args 未指定 --model。可用 provider/model: {preview}\n"
            f"    建议在 WORKFLOW.md 的 agent.reviewer.extra_args 加 "
            f"[\"--model\", \"<strong-model>\"]"
        )
    else:
        print(
            "  ⚠ reviewer extra_args 未指定 --model，且未在 ~/.config/opencode/opencode.jsonc "
            "找到已配置 provider。审查将用 opencode 默认模型（可能偏弱）。"
        )



if __name__ == "__main__":
    sys.exit(main())
