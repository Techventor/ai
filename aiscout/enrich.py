"""Stage 2 -- deep signals for the shortlist only.

Search results tell you how popular a repo is. They do not tell you whether
anyone besides the author works on it, or whether it was alive last month.
Those two facts separate a real project from a well-marketed one, and both
need per-repo endpoints:

  contributors  -- /repos/{o}/{r}/contributors?per_page=1 and read the page
                   count out of the Link rel="last" header. One request for
                   an exact-enough count instead of paging thousands.
  commits_90d   -- /repos/{o}/{r}/stats/commit_activity returns 52 weekly
                   buckets in one request. Answers 202 while GitHub builds
                   the cache, so the client retries.
  releases      -- /repos/{o}/{r}/releases?per_page=1 -- shipping cadence.

Budget: ~3 requests per repo against the 5000/hour core quota, so the
shortlist should stay in the hundreds. Sandboxes that scope the API to one
repository return 403 here; that is caught and the run degrades to the
search-only signal set.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field

from . import config
from .client import AccessDenied, GitHubClient, GitHubError

CONTRIBUTOR_SATURATION = config.CONTRIBUTOR_SATURATION


@dataclass
class Enricher:
    client: GitHubClient
    want_releases: bool = True
    verbose: bool = True

    denied: bool = field(default=False, init=False)
    ok: int = field(default=0, init=False)
    failed: int = field(default=0, init=False)

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # --------------------------------------------------------------- parts

    def contributors(self, full_name: str) -> tuple[int | None, bool]:
        """(contributor count, whether that count is saturated).

        GitHub resolves only the first 500 author email addresses to user
        accounts; past that, contributors come back anonymous, and this call
        excludes anonymous ones. So the count saturates somewhere below 500
        however large the project really is -- a first scan put langchain
        and transformers, which have thousands of contributors each, at 467
        and 439, beside a 21k-star repo at 408.

        Returning the saturation flag lets the scorer tie those repos
        instead of ordering the top of the ranking by which of the first
        500 addresses happened to resolve.
        """
        resp = self.client.get(f"repos/{full_name}/contributors",
                               {"per_page": 1, "anon": "false"})
        last = resp.last_page
        if last is None:
            body = resp.body
            count = len(body) if isinstance(body, list) else None
        else:
            count = last
        if count is None:
            return None, False
        return count, count >= CONTRIBUTOR_SATURATION

    def commit_activity(self, full_name: str) -> tuple[int | None, int | None]:
        """(commits in last 13 weeks, commits in the 13 weeks before that)."""
        resp = self.client.get(f"repos/{full_name}/stats/commit_activity",
                               allow_202=True)
        weeks = resp.body
        if not isinstance(weeks, list) or not weeks:
            return None, None
        totals = [int(w.get("total", 0)) for w in weeks if isinstance(w, dict)]
        recent = sum(totals[-13:])
        prior = sum(totals[-26:-13]) if len(totals) >= 26 else None
        return recent, prior

    def latest_release(self, full_name: str) -> tuple[int, str | None]:
        """(number of releases if cheap to know, date of the newest)."""
        resp = self.client.get(f"repos/{full_name}/releases", {"per_page": 1})
        body = resp.body
        if not isinstance(body, list) or not body:
            return 0, None
        count = resp.last_page or len(body)
        published = body[0].get("published_at") or body[0].get("created_at")
        return count, published

    # ----------------------------------------------------------------- run

    def enrich_one(self, repo: dict) -> dict:
        full_name = repo["full_name"]
        extra: dict = {"enriched": False}
        try:
            count, saturated = self.contributors(full_name)
            extra["contributors"] = count
            extra["contributors_saturated"] = saturated
            recent, prior = self.commit_activity(full_name)
            extra["commits_90d"] = recent
            extra["commits_prior_90d"] = prior
            if self.want_releases:
                count, published = self.latest_release(full_name)
                extra["releases"] = count
                extra["latest_release_at"] = published
            extra["enriched"] = True
            self.ok += 1
        except AccessDenied as exc:
            self.denied = True
            extra["enrich_error"] = f"access denied: {exc}"
            self.failed += 1
        except GitHubError as exc:
            extra["enrich_error"] = str(exc)
            self.failed += 1
        return extra

    def run(self, repos: list[dict], limit: int,
            budget_seconds: float = 1200.0) -> list[dict]:
        """Enrich the shortlist, within a wall-clock budget.

        Stage 1 is paced by a fixed search interval, so its cost is
        predictable. Stage 2's is not: a repo whose statistics GitHub has
        not cached costs several seconds more than one it has, and that is
        decided per repo on the day. The budget keeps a slow afternoon from
        turning into a timed-out job -- the repos it does not reach simply
        keep their search-only signals, which is what every repo outside
        the shortlist has anyway.
        """
        shortlist = repos[:limit]
        started = time.monotonic()
        self._log(f"\nStage 2: enriching top {len(shortlist)} repos "
                  f"(~3 API requests each, {budget_seconds:.0f}s budget)")

        for i, repo in enumerate(shortlist, 1):
            spent = time.monotonic() - started
            if spent > budget_seconds:
                self._log(f"  ! budget of {budget_seconds:.0f}s spent after "
                          f"{i - 1} repos; the remaining "
                          f"{len(shortlist) - i + 1} keep search-only "
                          f"signals")
                break

            repo.update(self.enrich_one(repo))

            if self.denied and self.ok == 0 and i >= 3:
                self._log("  ! per-repo API access is denied in this "
                          "environment; skipping stage 2 entirely.\n"
                          "    Scores will use search-API signals only.")
                break
            if self.verbose and (i % 25 == 0 or i == len(shortlist)):
                self._log(f"  enriched {i}/{len(shortlist)} "
                          f"(ok={self.ok} failed={self.failed}, "
                          f"{spent:.0f}s elapsed)")
        return repos

    @property
    def degraded(self) -> bool:
        return self.ok == 0


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
