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

    def test_default_reviewer_when_absent(self, tmp_path: Path):
        from symphony_oc.config import load_config
        wf = tmp_path / "WF.md"
        wf.write_text("---\nagent:\n  name: symphony-worker\n---\n")
        cfg = load_config(wf)
        assert cfg.agent.reviewer.name == "symphony-reviewer"
        assert cfg.agent.reviewer.min_iterations == 3
        assert cfg.agent.reviewer.max_iterations == 5
        assert cfg.agent.reviewer.extra_args == []

    def test_full_reviewer_node_parsed(self, tmp_path: Path):
        from symphony_oc.config import load_config
        wf = tmp_path / "WF.md"
        wf.write_text(
            "---\n"
            "agent:\n"
            "  name: symphony-worker\n"
            "  reviewer:\n"
            "    name: my-reviewer\n"
            "    min_iterations: 2\n"
            "    max_iterations: 4\n"
            "    extra_args: [\"--model\", \"anthropic/claude-opus\"]\n"
            "---\n"
        )
        cfg = load_config(wf)
        assert cfg.agent.reviewer.name == "my-reviewer"
        assert cfg.agent.reviewer.min_iterations == 2
        assert cfg.agent.reviewer.max_iterations == 4
        assert cfg.agent.reviewer.extra_args == ["--model", "anthropic/claude-opus"]

    def test_partial_reviewer_node_uses_defaults(self, tmp_path: Path):
        from symphony_oc.config import load_config
        wf = tmp_path / "WF.md"
        wf.write_text(
            "---\n"
            "agent:\n"
            "  reviewer:\n"
            "    min_iterations: 4\n"
            "---\n"
        )
        cfg = load_config(wf)
        assert cfg.agent.reviewer.min_iterations == 4
        assert cfg.agent.reviewer.max_iterations == 5  # default
        assert cfg.agent.reviewer.name == "symphony-reviewer"  # default
