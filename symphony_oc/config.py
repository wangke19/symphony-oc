from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


@dataclass
class GitConfig:
    remote: str = "upstream"
    base_branch: str = "upstream/main"
    worktree_root: str = "./worktrees"
    two_commit_pr: bool = True
    two_commit_exclude: list[str] = field(default_factory=lambda: ["*.lock", "requirements*.txt", "*.generated.*", "vendor/", "zz_generated*"])


@dataclass
class GithubTracker:
    repo: str = ""
    labels: list[str] = field(default_factory=lambda: ["symphony"])
    active_states: list[str] = field(default_factory=lambda: ["open"])


@dataclass
class TrackerConfig:
    local_dir: str = "./issues"
    github: GithubTracker = field(default_factory=GithubTracker)


@dataclass
class CiConfig:
    command: str = "pytest -q"
    timeout_ms: int = 120000


@dataclass
class AgentConfig:
    name: str = "symphony-worker"
    max_concurrent: int = 3
    stall_timeout_ms: int = 1_800_000
    max_retries: int = 3
    retry_backoff_ms: int = 10_000
    extra_args: list[str] = field(default_factory=lambda: ["--pure"])


@dataclass
class LocalIssueConfig:
    retrigger: str = "hash"


@dataclass
class Config:
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    git: GitConfig = field(default_factory=GitConfig)
    ci: CiConfig = field(default_factory=CiConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    local_issue: LocalIssueConfig = field(default_factory=LocalIssueConfig)
    polling_interval_ms: int = 30_000


def load_config(path: str | Path) -> Config:
    content = Path(path).read_text()
    if not content.startswith("---"):
        raise ValueError("YAML frontmatter not found (must start with '---')")
    parts = content.split("---", 2)
    if len(parts) < 3:
        raise ValueError("YAML frontmatter not closed (missing second '---')")
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error: {e}")
    return _dict_to_config(data or {})


def _dict_to_config(d: dict) -> Config:
    cfg = Config()
    if "tracker" in d:
        t = d["tracker"]
        gt = GithubTracker()
        if "github" in t:
            gt.repo = t["github"].get("repo", gt.repo)
            gt.labels = t["github"].get("labels", gt.labels)
            gt.active_states = t["github"].get("active_states", gt.active_states)
        cfg.tracker = TrackerConfig(local_dir=t.get("local_dir", cfg.tracker.local_dir), github=gt)
    if "git" in d:
        g = d["git"]
        cfg.git = GitConfig(**{k: g.get(k, getattr(cfg.git, k)) for k in ["remote", "base_branch", "worktree_root", "two_commit_pr", "two_commit_exclude"]})
    if "ci" in d:
        c = d["ci"]
        cfg.ci = CiConfig(**{k: c.get(k, getattr(cfg.ci, k)) for k in ["command", "timeout_ms"]})
    if "agent" in d:
        a = d["agent"]
        cfg.agent = AgentConfig(**{k: a.get(k, getattr(cfg.agent, k)) for k in ["name", "max_concurrent", "stall_timeout_ms", "max_retries", "retry_backoff_ms", "extra_args"]})
    if "local_issue" in d:
        cfg.local_issue.retrigger = d["local_issue"].get("retrigger", cfg.local_issue.retrigger)
    if "polling_interval_ms" in d:
        cfg.polling_interval_ms = d["polling_interval_ms"]
    return cfg
