import os
import requests

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self):
        self._headers = {
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._repo = os.environ["GITHUB_REPO"]

    def _url(self, path: str) -> str:
        return f"{GITHUB_API}/repos/{self._repo}/{path}"

    def get_issue(self, issue_number: int) -> dict:
        r = requests.get(self._url(f"issues/{issue_number}"), headers=self._headers)
        r.raise_for_status()
        return r.json()

    def post_comment(self, issue_number: int, body: str) -> dict:
        r = requests.post(
            self._url(f"issues/{issue_number}/comments"),
            headers=self._headers,
            json={"body": body},
        )
        r.raise_for_status()
        return r.json()

    def get_comments(self, issue_number: int) -> list[dict]:
        r = requests.get(self._url(f"issues/{issue_number}/comments"), headers=self._headers)
        r.raise_for_status()
        return r.json()

    def add_label(self, issue_number: int, label: str) -> None:
        r = requests.post(
            self._url(f"issues/{issue_number}/labels"),
            headers=self._headers,
            json={"labels": [label]},
        )
        r.raise_for_status()

    def close_issue(self, issue_number: int) -> None:
        r = requests.patch(
            self._url(f"issues/{issue_number}"),
            headers=self._headers,
            json={"state": "closed"},
        )
        r.raise_for_status()

    def get_open_pr_for_branch(self, branch: str) -> dict | None:
        owner = self._repo.split("/")[0]
        r = requests.get(
            self._url("pulls"),
            headers=self._headers,
            params={"head": f"{owner}:{branch}", "state": "open"},
        )
        r.raise_for_status()
        prs = r.json()
        return prs[0] if prs else None

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> dict:
        r = requests.post(
            self._url("pulls"),
            headers=self._headers,
            json={"title": title, "body": body, "head": head, "base": base},
        )
        if not r.ok:
            raise RuntimeError(f"GitHub PR creation failed {r.status_code}: {r.text}")
        return r.json()

    def get_commit_author_login(self, commit_sha: str) -> str | None:
        r = requests.get(self._url(f"commits/{commit_sha}"), headers=self._headers)
        if r.status_code != 200:
            return None
        return (r.json().get("author") or {}).get("login")

    def get_file_top_authors(self, filepath: str, n: int = 2) -> list[str]:
        """Return top-n GitHub logins by commit count on filepath."""
        r = requests.get(
            self._url("commits"),
            headers=self._headers,
            params={"path": filepath, "per_page": 100},
        )
        if r.status_code != 200:
            return []
        counts: dict[str, int] = {}
        for c in r.json():
            login = (c.get("author") or {}).get("login")
            if login:
                counts[login] = counts.get(login, 0) + 1
        return sorted(counts, key=lambda x: counts[x], reverse=True)[:n]
