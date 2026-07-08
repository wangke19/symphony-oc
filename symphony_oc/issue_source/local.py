import yaml
from datetime import datetime
from pathlib import Path
from symphony_oc.issue_source import Issue


class LocalIssueSource:
    def __init__(self, issues_dir: str = "./issues"):
        self._dir = Path(issues_dir)

    def fetch_issues(self) -> list[Issue]:
        if not self._dir.exists():
            return []
        issues = []
        md_files = [f for f in sorted(self._dir.iterdir()) if f.name.endswith(".md")]
        for idx, f in enumerate(md_files, start=1):
            content = f.read_text()
            title, labels, body = self._parse(content, f.stem)
            issue = Issue(
                id=f"local-{idx:04d}",
                title=title,
                description=body,
                labels=labels,
                source="local",
                created_at=datetime.fromtimestamp(f.stat().st_mtime),
            )
            issues.append(issue)
        return issues

    @staticmethod
    def _parse(content: str, filename_stem: str) -> tuple[str, list[str], str]:
        if not content.startswith("---"):
            return filename_stem.replace("-", " ").title(), [], content.strip()
        parts = content.split("---", 2)
        if len(parts) < 3:
            return filename_stem.replace("-", " ").title(), [], content.strip()
        try:
            frontmatter = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            frontmatter = {}
        title = frontmatter.get("title", filename_stem.replace("-", " ").title())
        labels = frontmatter.get("labels", [])
        body = parts[2].strip()
        return title, labels, body
