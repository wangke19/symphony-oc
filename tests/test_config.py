import pytest
from pathlib import Path
from symphony_oc.config import Config, load_config


SAMPLE_CONFIG = """---
tracker:
  local_dir: "./issues"
  github:
    repo: "owner/repo"
    labels: ["symphony"]
    active_states: ["open"]
git:
  remote: "upstream"
  base_branch: "upstream/main"
  worktree_root: "./worktrees"
  two_commit_pr: true
  two_commit_exclude:
    - "*.lock"
ci:
  command: "pytest -q"
  timeout_ms: 120000
agent:
  name: "symphony-worker"
  max_concurrent: 3
  stall_timeout_ms: 1800000
  max_retries: 3
  retry_backoff_ms: 10000
  extra_args: ["--pure"]
local_issue:
  retrigger: "hash"
polling_interval_ms: 30000
---


"""


class TestConfig:
    def test_load_config(self, tmp_path: Path):
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text(SAMPLE_CONFIG)
        cfg = load_config(str(wf))
        assert cfg.git.remote == "upstream"
        assert cfg.git.base_branch == "upstream/main"
        assert cfg.git.worktree_root == "./worktrees"
        assert cfg.git.two_commit_pr is True
        assert cfg.git.two_commit_exclude == ["*.lock"]
        assert cfg.ci.command == "pytest -q"
        assert cfg.ci.timeout_ms == 120000
        assert cfg.agent.name == "symphony-worker"
        assert cfg.agent.max_concurrent == 3
        assert cfg.agent.stall_timeout_ms == 1800000
        assert cfg.agent.max_retries == 3
        assert cfg.agent.retry_backoff_ms == 10000
        assert cfg.agent.extra_args == ["--pure"]
        assert cfg.local_issue.retrigger == "hash"
        assert cfg.polling_interval_ms == 30000

    def test_load_config_defaults(self, tmp_path: Path):
        minimal = "---\nci:\n  command: \"pytest\"\n---\n"
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text(minimal)
        cfg = load_config(str(wf))
        assert cfg.ci.command == "pytest"
        assert cfg.git.remote == "upstream"
        assert cfg.git.two_commit_pr is True
        assert cfg.agent.max_concurrent == 3

    def test_load_config_missing_file(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/WORKFLOW.md")

    def test_load_config_invalid_yaml(self, tmp_path: Path):
        wf = tmp_path / "WORKFLOW.md"
        wf.write_text("---\ninvalid: [unclosed\n---\n")
        with pytest.raises(ValueError, match="YAML"):
            load_config(str(wf))
