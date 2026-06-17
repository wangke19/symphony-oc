import json
import subprocess
from datetime import datetime
from symphony_oc.issue_source import Issue


class GitHubIssueSource:
    """Fetch issues from GitHub via gh CLI (Section 2.2)."""

    def __init__(self, repo: str, labels: list[str] | None = None,
                 active_states: list[str] | None = None):
        self._repo = repo
        self._labels = labels or ["symphony"]
        self._states = active_states or ["open"]

    def fetch_issues(self) -> list[Issue]:
        label_filter = ",".join(self._labels)
        state_filter = ",".join(self._states)
        result = subprocess.run(
            ["gh", "issue", "list",
             "--repo", self._repo,
             "--label", label_filter,
             "--state", state_filter,
             "--json", "number,title,body,labels,createdAt",
             "--limit", "50"],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        return [self._to_issue(item) for item in data]

    def _to_issue(self, item: dict) -> Issue:
        return Issue(
            id=f"GH-{item['number']}",
            title=item["title"],
            description=item.get("body", ""),
            labels=[l["name"] for l in item.get("labels", [])],
            source="github",
            created_at=datetime.fromisoformat(item["createdAt"].replace("Z", "+00:00")),
        )