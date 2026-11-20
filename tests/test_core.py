"""Offline tests for the parts that are easy to break and hard to notice.

No network, no pytest required:

    python3 tests/test_core.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiscout.client import Response, _parse_raw_http          # noqa: E402
from aiscout.score import (attach_momentum, categorise,       # noqa: E402
                           is_non_software, percentile_ranks, relevance)
from aiscout.store import Store                               # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name}  {detail}")


# ------------------------------------------------------------ percentiles --

def test_percentiles() -> None:
    print("percentile_ranks")
    check("empty input", percentile_ranks([]) == [])
    check("single value", percentile_ranks([5]) == [0.0])
    check("ordered", percentile_ranks([10, 20, 30]) == [0.0, 0.5, 1.0])
    check("None sinks to 0", percentile_ranks([10, None, 30]) == [0.0, 0.0, 1.0])
    check("ties share mean rank", percentile_ranks([7, 7, 9]) == [0.25, 0.25, 1.0])
    # order of input must not matter to the rank each value receives
    ranks = percentile_ranks([30, 10, 20])
    check("order independent", ranks == [1.0, 0.0, 0.5], str(ranks))


# ------------------------------------------------------------ http parsing --

def test_http_parsing() -> None:
    print("_parse_raw_http")
    raw = ("HTTP/1.1 301 Moved\r\nLocation: x\r\n\r\n"
           "HTTP/1.1 200 OK\r\nLink: <https://a?page=42>; rel=\"last\"\r\n"
           "X-RateLimit-Remaining: 7\r\n\r\n{\"a\": 1}")
    status, headers, body = _parse_raw_http(raw)
    check("takes the last header block", status == 200, str(status))
    check("lowercases header names", headers.get("x-ratelimit-remaining") == "7")
    check("body survives intact", body.strip() == '{"a": 1}', body)

    link = '<https://api.github.com/x?per_page=1&page=413>; rel="last"'
    check("Link rel=last -> page number",
          Response(200, {"link": link}, None).last_page == 413)
    check("no Link header -> None",
          Response(200, {}, None).last_page is None)


# -------------------------------------------------------------- category ---

CATEGORY_CASES = [
    ("huggingface/transformers",
     "🤗 Transformers: the model-definition framework for state-of-the-art models",
     ["audio", "speech-recognition", "deep-learning", "pytorch"],
     "framework/sdk"),
    ("langchain-ai/langchain", "The agent engineering platform.",
     ["agents", "ai", "rag", "ai-agents"], "framework/sdk"),
    ("vllm-project/vllm",
     "A high-throughput and memory-efficient inference and serving engine for LLMs",
     ["cuda", "inference", "llm"], "inference/serving"),
    ("ollama/ollama", "Get up and running with Kimi, GLM, DeepSeek, Qwen",
     ["ollama", "go", "llama"], "inference/serving"),
    ("x/awesome-llm", "A curated list of LLM resources", ["llm"],
     "learning/list"),
    ("ms/autogen", "A programming framework for agentic AI", ["ai-agents"],
     "framework/sdk"),
    ("comfyanonymous/ComfyUI", "The most powerful diffusion model GUI and backend",
     ["stable-diffusion", "image-generation"], "generative-media"),
    ("openai/whisper", "Robust Speech Recognition via Large-Scale Weak Supervision",
     ["speech-recognition", "asr"], "generative-media"),
    ("hiyouga/LLaMA-Factory", "Unified efficient fine-tuning of 100+ LLMs",
     ["fine-tuning", "lora", "peft"], "training/tuning"),
    ("chroma-core/chroma", "the AI-native open-source embedding database",
     ["vector-database", "embeddings"], "rag/retrieval"),
]


def test_categories() -> None:
    print("categorise")
    for name, desc, topics, want in CATEGORY_CASES:
        repo = {"name": name.split("/")[-1], "full_name": name,
                "description": desc, "topics": topics}
        got = categorise(repo)
        check(f"{name} -> {want}", got == want, f"got {got}")

    check("awesome-list detected", is_non_software({"name": "awesome-llm"}))
    check("real project not flagged", not is_non_software({"name": "vllm"}))
    check("strong AI signal scores high",
          relevance({"full_name": "vllm-project/vllm",
                     "description": "inference engine for LLMs",
                     "topics": ["llm", "llm-inference"]}) > 0.8)
    check("unrelated repo scores low",
          relevance({"full_name": "x/css-reset",
                     "description": "A tiny CSS reset", "topics": ["css"]}) < 0.2)


# -------------------------------------------------------------- momentum ---

def test_momentum_baselines() -> None:
    print("store.baselines + attach_momentum")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.db")
        store = Store(path)
        now = dt.datetime.now(dt.timezone.utc)
        con = sqlite3.connect(path)

        def snap(run: str, days_ago: float, stars: int) -> None:
            ts = (now - dt.timedelta(days=days_ago)).isoformat(timespec="seconds")
            con.execute("INSERT OR REPLACE INTO runs (run_id, started_at) "
                        "VALUES (?,?)", (run, ts))
            con.execute("INSERT OR REPLACE INTO snapshots (run_id, repo_id, "
                        "full_name, captured_at, stars) VALUES (?,1,'o/r',?,?)",
                        (run, ts, stars))

        # A clearly accelerating project:
        #   day -31 .. -8  (23 days): 70k -> 77k  =  7k   ->  2,130/wk
        #   day  -8 .. now ( 8 days): 77k -> 100k = 23k   -> 20,125/wk
        snap("eight_days", 8, 77_000)     # the right baseline for a 7d window
        snap("two_days", 2, 99_900)       # decoy: far too recent
        snap("thirty_one", 31, 70_000)    # the right baseline for a 30d window
        con.commit()
        con.close()

        b7 = store.baselines(7)
        b30 = store.baselines(30)
        check("7d window picks the 8-day snapshot",
              1 in b7 and b7[1][0] == 77_000, str(b7))
        check("7d window rejects the 2-day decoy",
              1 in b7 and abs(b7[1][1] - 8) < 0.1, str(b7))
        check("30d window picks the 31-day snapshot",
              1 in b30 and b30[1][0] == 70_000, str(b30))

        repo = {"id": 1, "full_name": "o/r", "stargazers_count": 100_000,
                "_d": {"age_days": 400.0}}
        attach_momentum([repo], b7, b30, now)
        d = repo["_d"]
        # 23,000 stars over 8 days == 20,125/week
        check("weekly rate normalised from an 8-day gap",
              abs(d["weekly_stars"] - 20_125) < 1, str(d["weekly_stars"]))
        check("momentum source reports the real gap",
              d["momentum_src"].startswith("delta/"), d["momentum_src"])
        # earlier stretch (day -31..-8) ran at 7,000/23*7 = 2,130.4/wk, so
        # acceleration is 20,125.0 - 2,130.4 = 17,994.6. Tolerance is loose
        # because snapshot timestamps carry second precision, so the gap is
        # 8.00001 days rather than exactly 8.
        check("acceleration uses the non-overlapping earlier window",
              d["accel"] is not None and abs(d["accel"] - 17_994.6) < 5,
              str(d["accel"]))
        store.close()


def test_momentum_deceleration() -> None:
    """A project past its peak must score negative acceleration."""
    print("attach_momentum on a decelerating project")
    now = dt.datetime.now(dt.timezone.utc)
    # day -30..-7 (23 days): 70k -> 93k = 23k -> 7,000/wk
    # day  -7..now ( 7 days): 93k -> 95k =  2k -> 2,000/wk
    repo = {"id": 2, "full_name": "o/cooling", "stargazers_count": 95_000,
            "_d": {"age_days": 400.0}}
    attach_momentum([repo], {2: (93_000, 7.0)}, {2: (70_000, 30.0)}, now)
    d = repo["_d"]
    check("weekly rate reflects the recent slowdown",
          abs(d["weekly_stars"] - 2_000) < 1, str(d["weekly_stars"]))
    check("acceleration is negative", d["accel"] < 0, str(d["accel"]))
    check("deceleration magnitude is right",
          abs(d["accel"] - (2_000 - 7_000)) < 1, str(d["accel"]))


def test_momentum_without_history() -> None:
    print("attach_momentum with no snapshots")
    repo = {"id": 9, "full_name": "o/new", "stargazers_count": 7_300,
            "_d": {"age_days": 365.0}}
    attach_momentum([repo], {}, {}, dt.datetime.now(dt.timezone.utc))
    d = repo["_d"]
    check("falls back to a lifetime average", d["momentum_src"] == "lifetime")
    check("lifetime rate is stars/age*7",
          abs(d["weekly_stars"] - 7_300 / 365 * 7) < 0.01, str(d["weekly_stars"]))
    check("no acceleration without a second window", d["accel"] is None)
    # Under the lifetime fallback weekly/stars reduces to 7/age, which is
    # repo age wearing a growth label. Scoring it ranked the youngest repos
    # top and called it momentum, so it must be suppressed here.
    check("rel_growth suppressed, not silently equal to 7/age",
          d["rel_growth"] is None, str(d["rel_growth"]))


def test_rel_growth_with_history() -> None:
    """With a measured delta, rel_growth is a real share of total stars."""
    print("attach_momentum rel_growth from a measured delta")
    repo = {"id": 3, "full_name": "o/r", "stargazers_count": 10_000,
            "_d": {"age_days": 400.0}}
    attach_momentum([repo], {3: (9_300, 7.0)}, {}, dt.datetime.now(dt.timezone.utc))
    d = repo["_d"]
    check("weekly rate from the delta", abs(d["weekly_stars"] - 700) < 0.01,
          str(d["weekly_stars"]))
    check("rel_growth is weekly/total", abs(d["rel_growth"] - 0.07) < 1e-9,
          str(d["rel_growth"]))


def test_weight_renormalisation() -> None:
    print("_renormalise")
    from aiscout.score import _renormalise
    base = {"a": 0.5, "b": 0.3, "c": 0.2}
    kept = _renormalise(base, {"c"})
    check("dead signal dropped", "c" not in kept, str(kept))
    check("remaining weights sum to 1", abs(sum(kept.values()) - 1.0) < 1e-9,
          str(sum(kept.values())))
    check("ratio between survivors preserved",
          abs(kept["a"] / kept["b"] - 0.5 / 0.3) < 1e-9, str(kept))
    check("nothing dropped means unchanged", _renormalise(base, set()) == base)
    everything = _renormalise(base, {"a", "b", "c"})
    check("all dead falls back to equal weights",
          abs(sum(everything.values()) - 1.0) < 1e-9, str(everything))


def test_teaching_repos_excluded() -> None:
    """Course repos win on stars but are not software."""
    print("non-software detection")
    for name in ("generative-ai-for-beginners", "awesome-llm", "llm-course",
                 "nlp-tutorial", "ai-roadmap", "python-exercises",
                 "deep-learning-crash-course"):
        check(f"{name} flagged", is_non_software({"name": name}))
    for name in ("vllm", "langchain", "transformers", "ollama", "whisper.cpp"):
        check(f"{name} not flagged", not is_non_software({"name": name}))


# ------------------------------------------------------------------ flags --

def test_flags() -> None:
    print("flags_for")
    from aiscout.score import derive, flags_for
    now = dt.datetime.now(dt.timezone.utc)
    old = (now - dt.timedelta(days=500)).isoformat()
    fresh = (now - dt.timedelta(days=1)).isoformat()
    born = (now - dt.timedelta(days=900)).isoformat()

    farmed = {"full_name": "x/farmed", "name": "farmed",
              "description": "an LLM agent framework", "topics": ["llm"],
              "stargazers_count": 60_000, "forks_count": 120,
              "open_issues_count": 3, "created_at": born, "pushed_at": fresh,
              "license": {"spdx_id": "MIT"}}
    farmed["_d"] = derive(farmed, now)
    check("suspect-stars on a sub-1% fork ratio",
          "suspect-stars" in flags_for(farmed), str(flags_for(farmed)))

    healthy = dict(farmed, forks_count=9_000)
    healthy["_d"] = derive(healthy, now)
    check("healthy fork ratio is not flagged",
          "suspect-stars" not in flags_for(healthy), str(flags_for(healthy)))

    stale = dict(healthy, pushed_at=old)
    stale["_d"] = derive(stale, now)
    f = flags_for(stale)
    check("dormant after a year without a push", "dormant" in f, str(f))
    check("alive decays to roughly zero", stale["_d"]["alive"] < 0.01,
          str(stale["_d"]["alive"]))

    unlicensed = dict(healthy, license=None)
    unlicensed["_d"] = derive(unlicensed, now)
    check("no-license flagged", "no-license" in flags_for(unlicensed))


def test_202_gives_up_quickly() -> None:
    """A cold statistics cache must cost seconds, not half a minute.

    GitHub answers 202 while it computes a repo's commit activity. Across a
    few hundred repos a cold cache is the common case, so waiting it out
    once cost up to 30s each and then raised anyway -- enough sleeping to
    time out a scheduled run for a figure that is only a nice-to-have.
    """
    print("client 202 handling")
    from aiscout.client import GitHubClient, Response

    client = GitHubClient(backend="gh", max_retries=5, max_202_waits=2)
    calls = {"n": 0}
    slept = {"total": 0.0}
    client._raw_get = lambda path, params: (
        calls.__setitem__("n", calls["n"] + 1), Response(202, {}, None))[1]
    client._sleep = lambda s, why: slept.__setitem__("total", slept["total"] + s)

    resp = client.get("repos/o/r/stats/commit_activity", allow_202=True)
    check("hands back the 202 instead of raising", resp.status == 202,
          str(resp.status))
    check("stops after max_202_waits + 1 requests", calls["n"] == 3,
          str(calls["n"]))
    check("total wait stays under 10s", slept["total"] <= 10,
          str(slept["total"]))
    check("counted as unavailable",
          client.stats["stats_unavailable"] == 1,
          str(client.stats["stats_unavailable"]))

    # and the caller treats it as "no data", not as an error
    from aiscout.enrich import Enricher
    e = Enricher(client=client, verbose=False)
    recent, prior = e.commit_activity("o/r")
    check("commit_activity reports no data", recent is None and prior is None,
          f"{recent}, {prior}")


def test_enrichment_budget() -> None:
    """Stage 2 must stop on time rather than overrun the job."""
    print("enrichment time budget")
    from aiscout.enrich import Enricher
    from aiscout.client import GitHubClient

    client = GitHubClient(backend="gh")
    e = Enricher(client=client, verbose=False)
    seen = []
    e.enrich_one = lambda repo: (seen.append(repo["full_name"]),
                                 time.sleep(0.05), {"enriched": True})[2]

    repos = [{"full_name": "o/r%d" % i} for i in range(40)]
    e.run(repos, limit=40, budget_seconds=0.2)
    check("stopped well before the full shortlist", len(seen) < 40, str(len(seen)))
    check("but did enrich something", len(seen) > 0, str(len(seen)))


def test_sharding_request_count() -> None:
    """Each shard must cost one request, not a count plus a read.

    total_count arrives inside the first page of results, so asking for it
    separately spent an extra request on every shard -- and most shards hold
    under one page, where that was half the cost of reading them.
    """
    print("discovery sharding")
    from aiscout.discover import Discovery

    class FakeResp:
        def __init__(self, body):
            self.body = body

    class FakeClient:
        """Stands in for GitHub: 250 repos between 1000 and 1999 stars."""
        def __init__(self):
            self.calls = []

        def search_repos(self, query, page=1, per_page=100):
            self.calls.append((query, page))
            m = re.search(r"stars:(\d+)\.\.(\d+)", query)
            lo, hi = int(m.group(1)), int(m.group(2))
            ids = [i for i in range(250) if 1000 <= 1000 + i < 2000
                   and lo <= 1000 + i <= hi]
            start = (page - 1) * per_page
            window = ids[start:start + per_page]
            return FakeResp({
                "total_count": len(ids),
                "items": [{"id": i, "full_name": "o/r%d" % i,
                           "stargazers_count": 1000 + i} for i in window],
            })

    client = FakeClient()
    d = Discovery(client=client, verbose=False)
    d._sharded("topic:x", "topic:x", 1000)

    check("every repo found", len(d.repos) == 250, str(len(d.repos)))
    # 250 results over 100 per page = 3 pages, and no separate count request
    check("three requests, no count probe", len(client.calls) == 3,
          str(client.calls))
    check("pages read in order",
          [c[1] for c in client.calls] == [1, 2, 3], str(client.calls))
    check("queries_run matches real requests",
          d.queries_run == len(client.calls),
          f"{d.queries_run} vs {len(client.calls)}")


def test_small_shard_is_one_request() -> None:
    """A shard that fits in a page must cost exactly one request."""
    print("discovery small shard")
    from aiscout.discover import Discovery

    class FakeResp:
        def __init__(self, body):
            self.body = body

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def search_repos(self, query, page=1, per_page=100):
            self.calls += 1
            return FakeResp({
                "total_count": 7,
                "items": [{"id": i, "full_name": "o/r%d" % i,
                           "stargazers_count": 1500} for i in range(7)],
            })

    client = FakeClient()
    d = Discovery(client=client, verbose=False)
    d._sharded("topic:x", "topic:x", 1000)
    check("one request for a one-page shard", client.calls == 1, str(client.calls))
    check("all seven kept", len(d.repos) == 7, str(len(d.repos)))


def test_contributor_saturation() -> None:
    """A capped API number must not order the top of the ranking.

    GitHub resolves only the first 500 author emails to accounts, and this
    call drops anonymous contributors, so the count saturates below 500
    however large the project is. A first real scan returned 467 for
    langchain and 439 for transformers -- both have thousands -- beside 408
    for a 21k-star repo. Scoring those apart ranked projects by which
    addresses happened to resolve.
    """
    print("contributor saturation")
    from aiscout import config
    from aiscout.score import percentile_ranks, _log

    cap = config.CONTRIBUTOR_SATURATION
    counts = [12, 80, cap + 7, cap + 39, cap + 76]

    raw = percentile_ranks([_log(v) for v in counts])
    check("raw counts order the saturated repos apart",
          len(set(raw[2:])) == 3, str(raw))

    clamped = percentile_ranks([_log(min(v, cap)) for v in counts])
    check("clamped counts tie them", len(set(clamped[2:])) == 1, str(clamped))
    check("below the cap still discriminates",
          clamped[0] < clamped[1] < clamped[2], str(clamped))


def test_contributors_reports_saturation() -> None:
    """The enricher flags a saturated count rather than hiding it."""
    print("contributors saturation flag")
    from aiscout.client import GitHubClient, Response
    from aiscout.enrich import Enricher, CONTRIBUTOR_SATURATION

    client = GitHubClient(backend="gh")
    e = Enricher(client=client, verbose=False)

    def fake(page):
        link = '<https://api.github.com/x?per_page=1&page=%d>; rel="last"' % page
        return Response(200, {"link": link}, [{}])

    client.get = lambda path, params=None, **kw: fake(CONTRIBUTOR_SATURATION + 60)
    count, saturated = e.contributors("o/big")
    check("returns the raw count", count == CONTRIBUTOR_SATURATION + 60, str(count))
    check("flags it as saturated", saturated is True, str(saturated))

    client.get = lambda path, params=None, **kw: fake(31)
    count, saturated = e.contributors("o/small")
    check("a small count is not flagged",
          count == 31 and saturated is False, f"{count}, {saturated}")


def main() -> int:
    for fn in (test_percentiles, test_http_parsing, test_categories,
               test_momentum_baselines, test_momentum_deceleration,
               test_momentum_without_history, test_rel_growth_with_history,
               test_weight_renormalisation, test_teaching_repos_excluded,
               test_202_gives_up_quickly, test_enrichment_budget,
               test_sharding_request_count, test_small_shard_is_one_request,
               test_contributor_saturation, test_contributors_reports_saturation,
               test_flags):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all tests pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
