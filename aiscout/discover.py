"""Stage 1 -- wide, cheap discovery via the search API.

The search API returns at most 1000 results per query, however large
total_count is. So each query is split over star ranges until every shard
fits under the cap. Splits are geometric, because star counts follow a
power law and an arithmetic midpoint would put almost everything in the
bottom shard.

One search request yields 100 fully-populated repo records, which is why
this stage can cover thousands of repos for a few dozen requests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import config
from .client import AccessDenied, GitHubClient, GitHubError

SEARCH_RESULT_CAP = 1000
MAX_SHARD_DEPTH = 8
# Above every repository on GitHub, so no shard needs an open upper bound.
# An open-ended top shard cost an extra probe to discover a ceiling it
# could have been given.
STAR_CEILING = 10_000_000


@dataclass
class Discovery:
    client: GitHubClient
    min_stars_core: int = config.DEFAULTS["min_stars_core"]
    min_stars_adjacent: int = config.DEFAULTS["min_stars_adjacent"]
    min_stars_keyword: int = config.DEFAULTS["min_stars_keyword"]
    per_page: int = config.DEFAULTS["search_page_size"]
    include_forks: bool = False
    include_archived: bool = False
    verbose: bool = True

    repos: dict[int, dict] = field(default_factory=dict, init=False)
    sources: dict[int, set] = field(default_factory=dict, init=False)
    queries_run: int = field(default=0, init=False)

    # ------------------------------------------------------------- helpers

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _base_filters(self) -> str:
        bits = ["is:public"]
        if not self.include_forks:
            bits.append("fork:false")
        if not self.include_archived:
            bits.append("archived:false")
        return " ".join(bits)

    def _absorb(self, items: list[dict], source: str) -> int:
        added = 0
        for item in items:
            rid = item.get("id")
            if rid is None:
                continue
            if rid not in self.repos:
                self.repos[rid] = item
                added += 1
            else:
                # Keep whichever record has more stars (they can differ
                # slightly between shards taken minutes apart).
                if item.get("stargazers_count", 0) > self.repos[rid].get("stargazers_count", 0):
                    self.repos[rid] = item
            self.sources.setdefault(rid, set()).add(source)
        return added

    # -------------------------------------------------------------- paging

    def _fetch_pages(self, query: str, source: str, expected: int,
                     first_page: int = 1) -> int:
        """Page through one query up to the 1000-result cap."""
        pages = min(math.ceil(expected / self.per_page),
                    SEARCH_RESULT_CAP // self.per_page)
        added = 0
        for page in range(first_page, pages + 1):
            try:
                resp = self.client.search_repos(query, page=page,
                                                per_page=self.per_page)
            except AccessDenied:
                raise
            except GitHubError as exc:
                self._log(f"    ! page {page} failed: {exc}")
                break
            self.queries_run += 1
            items = resp.body.get("items", []) if isinstance(resp.body, dict) else []
            added += self._absorb(items, source)
            if len(items) < self.per_page:
                break
        return added

    def _sharded(self, base_query: str, source: str, lo: int,
                 hi: int = STAR_CEILING, depth: int = 0) -> int:
        """Recursively split [lo, hi] on stars until each shard fits the cap.

        The first request of every shard asks for a full page rather than a
        bare count: `total_count` comes back in the same response, so asking
        separately spent one request per shard for a number already on its
        way. Most shards hold under a page, where that was half the cost of
        reading them.
        """
        span = f"stars:{lo}..{hi}"
        query = f"{base_query} {span}"

        try:
            resp = self.client.search_repos(query, page=1,
                                            per_page=self.per_page)
        except AccessDenied:
            raise
        except GitHubError as exc:
            self._log(f"    ! probe failed for {span}: {exc}")
            return 0
        self.queries_run += 1

        body = resp.body if isinstance(resp.body, dict) else {}
        items = body.get("items") or []
        total = int(body.get("total_count") or 0)

        # This page is never wasted work: its repos are kept whether the
        # shard is taken whole or split further.
        added = self._absorb(items, source)

        if total == 0 or len(items) < self.per_page or total <= len(items):
            return added

        if total <= SEARCH_RESULT_CAP or depth >= MAX_SHARD_DEPTH:
            if total > SEARCH_RESULT_CAP:
                self._log(f"    ~ {span}: {total} results, capped at "
                          f"{SEARCH_RESULT_CAP} (shard too deep)")
            return added + self._fetch_pages(query, source, total, first_page=2)

        # Geometric midpoint suits a power-law distribution.
        mid = int(math.sqrt(max(lo, 1) * hi))
        if mid <= lo or mid >= hi:
            mid = (lo + hi) // 2
        if mid <= lo or mid >= hi:
            return added + self._fetch_pages(query, source, total, first_page=2)

        return (added
                + self._sharded(base_query, source, lo, mid, depth + 1)
                + self._sharded(base_query, source, mid + 1, hi, depth + 1))

    # ---------------------------------------------------------------- runs

    def by_topics(self, topics: list[str], min_stars: int, label: str) -> None:
        base = self._base_filters()
        for i, topic in enumerate(topics, 1):
            before = len(self.repos)
            try:
                self._sharded(f"{base} topic:{topic}", f"topic:{topic}",
                              min_stars)
            except AccessDenied:
                self._log("  ! search access denied; stopping topic sweep")
                return
            self._log(f"  [{label} {i}/{len(topics)}] topic:{topic:<28} "
                      f"+{len(self.repos) - before:<5} pool={len(self.repos)}")

    def by_keywords(self, probes: list[str], min_stars: int) -> None:
        base = self._base_filters()
        for i, probe in enumerate(probes, 1):
            before = len(self.repos)
            quoted = f'"{probe}"' if " " in probe else probe
            try:
                self._sharded(f"{base} {quoted} in:name,description,readme",
                              f"kw:{probe}", min_stars)
            except AccessDenied:
                self._log("  ! search access denied; stopping keyword sweep")
                return
            self._log(f"  [kw {i}/{len(probes)}] {probe:<28} "
                      f"+{len(self.repos) - before:<5} pool={len(self.repos)}")

    def run(self, *, use_adjacent: bool = True,
            use_keywords: bool = True) -> dict[int, dict]:
        self._log(f"\nStage 1: discovery (core topics >= {self.min_stars_core} stars)")
        self.by_topics(config.TOPICS_CORE, self.min_stars_core, "core")

        if use_adjacent:
            self._log(f"\nStage 1b: adjacent topics (>= {self.min_stars_adjacent} stars)")
            self.by_topics(config.TOPICS_ADJACENT, self.min_stars_adjacent, "adj")

        if use_keywords:
            self._log(f"\nStage 1c: keyword probes (>= {self.min_stars_keyword} stars)")
            self.by_keywords(config.KEYWORD_PROBES, self.min_stars_keyword)

        self._log(f"\nDiscovery done: {len(self.repos)} unique repos "
                  f"from {self.queries_run} search requests")
        return self.repos
