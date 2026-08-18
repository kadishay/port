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

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> dict:
        r = requests.post(
            self._url("pulls"),
            headers=self._headers,
            json={"title": title, "body": body, "head": head, "base": base},
        )
        r.raise_for_status()
        return r.json()
