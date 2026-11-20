"""Rate-limit-aware GitHub API client.

Two backends:
  * "requests" -- uses GITHUB_TOKEN. Full API access. Preferred.
  * "gh"       -- shells out to the `gh` CLI. Works in sandboxes where a
                  token is injected by a proxy, but may be scope-limited.

Backend "auto" picks requests when a token is set, otherwise gh.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field

API_ROOT = "https://api.github.com"
USER_AGENT = "ai-oss-radar/1.0"


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class AccessDenied(GitHubError):
    """403 that is about repository scope, not about rate limits."""


@dataclass
class Response:
    status: int
    headers: dict
    body: object

    @property
    def last_page(self) -> int | None:
        """Page number from a Link rel="last" header, if present."""
        link = self.headers.get("link", "")
        m = re.search(r'<([^>]*[?&]page=(\d+)[^>]*)>;\s*rel="last"', link)
        return int(m.group(2)) if m else None


def _parse_raw_http(raw: str) -> tuple[int, dict, str]:
    """Parse `gh api -i` output, tolerating redirect header blocks."""
    text = raw.replace("\r\n", "\n")
    pos, status, headers = 0, 0, {}
    while text.startswith("HTTP/", pos):
        end = text.find("\n\n", pos)
        block = text[pos:] if end == -1 else text[pos:end]
        pos = len(text) if end == -1 else end + 2
        lines = block.split("\n")
        parts = lines[0].split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
    return status, headers, text[pos:]


@dataclass
class GitHubClient:
    token: str | None = None
    backend: str = "auto"
    search_min_interval: float = 2.1
    max_retries: int = 5
    # How long to wait for a 202 "statistic is being computed" before giving
    # up on it. Kept tiny on purpose: see the 202 branch in get().
    max_202_waits: int = 2
    timeout: int = 60
    verbose: bool = False

    _last_search: float = field(default=0.0, init=False)
    _session: object | None = field(default=None, init=False)
    stats: dict = field(default_factory=lambda: {"requests": 0, "retries": 0,
                                                 "denied": 0, "slept": 0.0,
                                                 "stats_unavailable": 0},
                        init=False)

    def __post_init__(self) -> None:
        self.token = self.token or os.environ.get("GITHUB_TOKEN") \
            or os.environ.get("GH_TOKEN") or None
        if self.backend == "auto":
            self.backend = "requests" if self.token else "gh"
        if self.backend == "requests":
            if not self.token:
                raise GitHubError("backend 'requests' needs GITHUB_TOKEN")
            import requests  # imported lazily so gh-only use needs no deps
            self._session = requests.Session()
            self._session.headers.update({
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {self.token}",
            })
        elif self.backend == "gh":
            if not shutil.which("gh"):
                raise GitHubError("backend 'gh' needs the gh CLI on PATH")
        else:
            raise GitHubError(f"unknown backend {self.backend!r}")

    # ------------------------------------------------------------ plumbing

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [api] {msg}", flush=True)

    def _raw_get(self, path: str, params: dict | None) -> Response:
        self.stats["requests"] += 1
        if self.backend == "requests":
            url = path if path.startswith("http") else f"{API_ROOT}/{path.lstrip('/')}"
            r = self._session.get(url, params=params, timeout=self.timeout)
            try:
                body = r.json()
            except ValueError:
                body = r.text
            return Response(r.status_code, {k.lower(): v for k, v in r.headers.items()}, body)

        cmd = ["gh", "api", "-i", "--method", "GET", path]
        for k, v in (params or {}).items():
            cmd += ["-f", f"{k}={v}"]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=self.timeout)
        if not proc.stdout.strip():
            raise GitHubError(proc.stderr.strip()
                              or f"gh exited {proc.returncode} with no output")
        status, headers, raw_body = _parse_raw_http(proc.stdout)
        try:
            body = json.loads(raw_body) if raw_body.strip() else None
        except ValueError:
            body = raw_body
        return Response(status, headers, body)

    def _sleep(self, seconds: float, why: str) -> None:
        seconds = max(0.0, min(seconds, 900.0))
        if seconds <= 0:
            return
        self.stats["slept"] += seconds
        self._log(f"sleep {seconds:.1f}s ({why})")
        time.sleep(seconds)

    def _respect_limits(self, resp: Response) -> None:
        """Pre-emptively wait when the remaining quota is nearly spent."""
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is None or reset is None:
            return
        try:
            remaining, reset = int(remaining), int(reset)
        except ValueError:
            return
        if remaining <= 1:
            self._sleep(reset - time.time() + 2, "primary rate limit")

    def get(self, path: str, params: dict | None = None,
            *, is_search: bool = False, allow_202: bool = False) -> Response:
        """GET with retries for rate limits, 202 stat builds and 5xx."""
        waits_202 = 0
        for attempt in range(self.max_retries):
            if is_search:
                wait = self.search_min_interval - (time.time() - self._last_search)
                self._sleep(wait, "search pacing")
                self._last_search = time.time()

            resp = self._raw_get(path, params)
            status = resp.status

            if status in (200, 201):
                self._respect_limits(resp)
                return resp

            if status == 202 and allow_202:
                # GitHub answers 202 while it builds a repo's statistics, and
                # across a few hundred repos a cold cache is the common case,
                # not the exception. Waiting it out cost up to 30s each and
                # then raised anyway -- minutes of sleeping per run, for a
                # figure that is a nice-to-have. So wait briefly, then hand
                # back the 202 and let the caller treat it as "no data".
                if waits_202 >= self.max_202_waits:
                    self.stats["stats_unavailable"] += 1
                    return resp
                waits_202 += 1
                self._sleep(2 * waits_202, "202 stats warming up")
                self.stats["retries"] += 1
                continue

            if status == 204:
                return resp

            if status == 404:
                raise GitHubError(f"404 for {path}", 404)

            message = ""
            if isinstance(resp.body, dict):
                message = str(resp.body.get("message", ""))

            if status == 403 and "not enabled for this session" in message:
                self.stats["denied"] += 1
                raise AccessDenied(message, 403)

            if status in (403, 429):
                retry_after = resp.headers.get("retry-after")
                if retry_after:
                    self._sleep(float(retry_after) + 1, "Retry-After")
                elif resp.headers.get("x-ratelimit-remaining") == "0":
                    reset = int(resp.headers.get("x-ratelimit-reset", 0))
                    self._sleep(reset - time.time() + 2, "quota exhausted")
                else:
                    self._sleep(10 * (attempt + 1), "secondary rate limit")
                self.stats["retries"] += 1
                continue

            if status >= 500:
                self._sleep(2 ** attempt, f"server {status}")
                self.stats["retries"] += 1
                continue

            raise GitHubError(f"{status} for {path}: {message}", status)

        raise GitHubError(f"gave up on {path} after {self.max_retries} attempts")

    # --------------------------------------------------------------- search

    def search_repos(self, query: str, page: int = 1,
                     per_page: int = 100) -> Response:
        return self.get("search/repositories",
                        {"q": query, "per_page": per_page, "page": page,
                         "sort": "stars", "order": "desc"},
                        is_search=True)

    # ---------------------------------------------------------------- misc

    def rate_limit(self) -> dict:
        try:
            return self.get("rate_limit").body.get("resources", {})
        except GitHubError:
            return {}


def encode_query(parts: list[str]) -> str:
    return " ".join(p for p in parts if p)


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")
