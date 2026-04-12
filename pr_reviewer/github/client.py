# pr_reviewer/github/client.py
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx


@dataclass
class PRInfo:
    owner: str
    repo: str
    number: int
    head_sha: str
    base_sha: str
    title: str
    body: str


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """
    Accept either a URL or owner/repo#number shorthand.
    Returns (owner, repo, number).
    """
    # Full URL: https://github.com/owner/repo/pull/123
    url_match = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url.strip()
    )
    if url_match:
        owner, repo, number = url_match.groups()
        return owner, repo, int(number)

    # Short form: owner/repo#123
    short_match = re.match(r"([^/]+)/([^#]+)#(\d+)", url.strip())
    if short_match:
        owner, repo, number = short_match.groups()
        return owner, repo, int(number)

    raise ValueError(
        f"Cannot parse PR reference: {url!r}\n"
        "Expected https://github.com/owner/repo/pull/N  or  owner/repo#N"
    )


class GitHubClient:
    BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._token = token
        # We need two clients: one for JSON, one for diff (different Accept header)
        self._json_client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )
        self._diff_client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.diff",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=60,
        )

    def _check(self, response: httpx.Response) -> httpx.Response:
        """Raise a readable error on non-2xx."""
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text[:200])
            except Exception:
                detail = response.text[:200]
            raise RuntimeError(
                f"GitHub API error {response.status_code}: {detail}\n"
                f"URL: {response.url}"
            )
        return response

    def get_pr_info(self, owner: str, repo: str, number: int) -> PRInfo:
        url = f"{self.BASE}/repos/{owner}/{repo}/pulls/{number}"
        resp = self._check(self._json_client.get(url))
        data = resp.json()
        return PRInfo(
            owner=owner,
            repo=repo,
            number=number,
            head_sha=data["head"]["sha"],
            base_sha=data["base"]["sha"],
            title=data["title"],
            body=data.get("body") or "",
        )

    def get_diff(self, owner: str, repo: str, number: int) -> str:
        url = f"{self.BASE}/repos/{owner}/{repo}/pulls/{number}"
        resp = self._check(self._diff_client.get(url))
        return resp.text

    def get_pr_files(self, owner: str, repo: str, number: int) -> list[dict]:
        """Returns the files list (useful for metadata, not the full diff)."""
        url = f"{self.BASE}/repos/{owner}/{repo}/pulls/{number}/files"
        resp = self._check(self._json_client.get(url))
        return resp.json()

    def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        commit_sha: str,
        comments: list[dict],
        body: str,
    ) -> dict:
        """
        Post a review with inline comments.
        Each comment dict: {"path": str, "position": int, "body": str}
        Note: GitHub calls it "position" (not "line") for diff-based comments.
        """
        url = f"{self.BASE}/repos/{owner}/{repo}/pulls/{number}/reviews"
        payload = {
            "commit_id": commit_sha,
            "event": "COMMENT",
            "body": body,
            "comments": comments,
        }
        resp = self._check(self._json_client.post(url, json=payload))
        return resp.json()

    def close(self) -> None:
        self._json_client.close()
        self._diff_client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()