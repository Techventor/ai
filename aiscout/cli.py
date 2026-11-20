"""Command line entry point.

    python -m aiscout scan                     # full run
    python -m aiscout scan --quick             # core topics only, fast
    python -m aiscout scan --no-enrich         # stage 1 only
    python -m aiscout trend langchain-ai/langchain
    python -m aiscout limits
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from . import config
from .client import GitHubClient, GitHubError
from .discover import Discovery
from .enrich import Enricher
from .report import write_all
from .score import Scorer, attach_momentum
from .store import Store


def cmd_scan(args: argparse.Namespace) -> int:
    client = GitHubClient(backend=args.backend, verbose=args.verbose,
                          search_min_interval=args.search_interval)
    print(f"backend={client.backend}  token={'yes' if client.token else 'no (gh-injected)'}")

    store = Store(args.db)
    run_id = store.start_run()
    prior_runs = store.run_count()
    print(f"run_id={run_id}  prior completed runs={prior_runs}")

    # ---- stage 1 --------------------------------------------------------
    discovery = Discovery(
        client=client,
        min_stars_core=args.min_stars,
        min_stars_adjacent=max(args.min_stars, args.min_stars_adjacent),
        min_stars_keyword=max(args.min_stars, args.min_stars_keyword),
        verbose=True,
    )
    if args.topics:
        picked = [t.strip() for t in args.topics.split(",") if t.strip()]
        print(f"\nStage 1: discovery over {len(picked)} given topic(s) "
              f"(>= {args.min_stars} stars)")
        discovery.by_topics(picked, args.min_stars, "core")
        found = discovery.repos
        print(f"\nDiscovery done: {len(found)} unique repos from "
              f"{discovery.queries_run} search requests")
    else:
        found = discovery.run(use_adjacent=not args.quick,
                              use_keywords=not args.quick)
    repos = list(found.values())
    if not repos:
        print("no repositories found; check search access", file=sys.stderr)
        store.finish_run(run_id, 0, 0, True, "no results")
        return 1

    # ---- provisional scoring, to pick a shortlist worth enriching -------
    scorer = Scorer(min_relevance=args.min_relevance,
                    include_non_software=args.include_lists,
                    max_per_owner=args.max_per_owner)
    scorer.score(repos, enriched=False)

    baseline_7d = store.baselines(7)
    baseline_30d = store.baselines(30)
    attach_momentum(repos, baseline_7d, baseline_30d,
                    dt.datetime.now(dt.timezone.utc))
    momentum_source = "delta" if baseline_7d or baseline_30d else "lifetime"
    print(f"\nmomentum source: {momentum_source} "
          f"(7d baselines={len(baseline_7d)}, 30d={len(baseline_30d)})")

    scorer.rank(repos, enriched=False)

    # ---- stage 2 --------------------------------------------------------
    enricher = Enricher(client=client, verbose=True)
    degraded = True
    if args.enrich:
        ordered = sorted(repos, key=lambda r: r["influence"], reverse=True)
        enricher.run(ordered, args.enrich_top, args.enrich_budget)
        degraded = enricher.degraded
        if not degraded:
            scorer.rank(repos, enriched=True)
    else:
        print("\nStage 2: skipped (--no-enrich)")

    # ---- persist + report ----------------------------------------------
    store.save_snapshots(run_id, repos)

    influence = scorer.shortlist(repos, "influence", args.top)
    momentum = scorer.shortlist(repos, "momentum", args.top)
    store.save_scores(run_id, influence + momentum)

    meta = {
        "run_id": run_id,
        "candidates": len(repos),
        "queries": discovery.queries_run,
        "enriched": enricher.ok,
        "degraded": degraded,
        "momentum_source": momentum_source,
        "dropped_signals": scorer.dropped_signals,
        "api_requests": client.stats["requests"],
        "min_stars": args.min_stars,
    }
    store.finish_run(run_id, len(repos), enricher.ok, degraded,
                     f"queries={discovery.queries_run}")

    paths = write_all(args.out, influence=influence, momentum=momentum,
                      meta=meta)
    print(f"\nAPI requests: {client.stats['requests']}  "
          f"retries: {client.stats['retries']}  "
          f"slept: {client.stats['slept']:.0f}s")
    print("\nwrote:")
    for label in ("markdown", "json"):
        print(f"  {paths[label]}")

    print(f"\nTop {min(15, len(influence))} by influence:")
    for i, r in enumerate(influence[:15], 1):
        flags = (" [" + ",".join(r["flags"]) + "]") if r["flags"] else ""
        print(f"  {i:2}. {r['influence']:5.1f}  {r['full_name']:<45} "
              f"{r['_d']['stars']:>7,}★{flags}")

    print(f"\nTop {min(15, len(momentum))} by momentum:")
    for i, r in enumerate(momentum[:15], 1):
        print(f"  {i:2}. {r['momentum']:5.1f}  {r['full_name']:<45} "
              f"+{r['weekly_stars']:>8,.0f}★/wk")

    store.close()
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    """Re-score a stored run offline. No network, no search quota."""
    store = Store(args.db)
    run_id = args.run_id or store.latest_run_id()
    if not run_id:
        print("no completed runs in the database", file=sys.stderr)
        return 1
    repos = store.load_run(run_id)
    if not repos:
        print(f"run {run_id} holds no snapshots", file=sys.stderr)
        return 1
    print(f"rescoring run {run_id}: {len(repos)} repos (offline)")

    scorer = Scorer(min_relevance=args.min_relevance,
                    include_non_software=args.include_lists,
                    max_per_owner=args.max_per_owner)
    scorer.score(repos, enriched=False)
    baseline_7d, baseline_30d = store.baselines(7), store.baselines(30)
    attach_momentum(repos, baseline_7d, baseline_30d,
                    dt.datetime.now(dt.timezone.utc))
    momentum_source = "delta" if baseline_7d or baseline_30d else "lifetime"
    enriched = any(r.get("contributors") is not None for r in repos)
    scorer.rank(repos, enriched=enriched)

    influence = scorer.shortlist(repos, "influence", args.top)
    momentum = scorer.shortlist(repos, "momentum", args.top)
    info = store.run_info(run_id)
    meta = {
        "run_id": run_id, "candidates": len(repos),
        # the searches that gathered this data, not the zero this rescore made
        "queries": info.get("queries", 0),
        "enriched": sum(1 for r in repos if r.get("contributors") is not None),
        "degraded": not enriched, "momentum_source": momentum_source,
        "dropped_signals": scorer.dropped_signals, "api_requests": 0,
        "rescored": True,
    }
    paths = write_all(args.out, influence=influence, momentum=momentum,
                      meta=meta)
    if scorer.dropped_signals:
        print(f"dropped signals (no data): {', '.join(scorer.dropped_signals)}")
    print(f"wrote {paths['markdown']}\n")

    label = "fastest since launch" if momentum_source == "lifetime" else "momentum"
    for title, rows, key in (("influence", influence, "influence"),
                             (label, momentum, "momentum")):
        print(f"Top {min(15, len(rows))} by {title}:")
        for i, r in enumerate(rows[:15], 1):
            flags = (" [" + ",".join(r["flags"]) + "]") if r["flags"] else ""
            print(f"  {i:2}. {r[key]:5.1f}  {r['full_name']:<42} "
                  f"{r['_d']['stars']:>7,}* +{r['weekly_stars']:>7,.0f}/wk{flags}")
        print()
    store.close()
    return 0


def cmd_trend(args: argparse.Namespace) -> int:
    store = Store(args.db)
    rows = store.history(args.repo)
    if not rows:
        print(f"no snapshots for {args.repo}")
        return 1
    print(f"{'captured':<22}{'stars':>10}{'forks':>9}{'contrib':>9}{'90d commits':>13}")
    prev = None
    for row in reversed(rows):
        delta = "" if prev is None else f"  ({row['stars'] - prev:+,})"
        print(f"{row['captured_at']:<22}{row['stars']:>10,}"
              f"{row['forks'] or 0:>9,}{row['contributors'] or 0:>9}"
              f"{row['commits_90d'] or 0:>13}{delta}")
        prev = row["stars"]
    store.close()
    return 0


def cmd_limits(args: argparse.Namespace) -> int:
    client = GitHubClient(backend=args.backend, verbose=args.verbose)
    res = client.rate_limit()
    if not res:
        print("could not read rate limits")
        return 1
    for name in ("core", "search", "code_search", "graphql"):
        r = res.get(name)
        if r:
            print(f"{name:<12} {r['remaining']:>6}/{r['limit']:<6} "
                  f"resets in {max(0, r['reset'] - int(time.time()))}s")
    return 0


def build_parser() -> argparse.ArgumentParser:
    d = config.DEFAULTS
    p = argparse.ArgumentParser(prog="aiscout",
                                description="Find influential AI open source on GitHub")
    p.add_argument("--db", default="data/aiscout.db")
    p.add_argument("--backend", default="auto", choices=["auto", "requests", "gh"])
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="discover, score and report")
    s.add_argument("--min-stars", type=int, default=d["min_stars_core"])
    s.add_argument("--min-stars-adjacent", type=int, default=d["min_stars_adjacent"])
    s.add_argument("--min-stars-keyword", type=int, default=d["min_stars_keyword"])
    s.add_argument("--top", type=int, default=d["report_top_n"])
    s.add_argument("--enrich-top", type=int, default=d["enrich_top_n"])
    s.add_argument("--enrich-budget", type=float, default=d["enrich_budget_s"],
                   help="wall-clock seconds stage 2 may spend before it stops "
                        "and leaves the rest on search-only signals")
    s.add_argument("--max-per-owner", type=int, default=d["max_per_owner"],
                   help="0 disables the per-owner cap")
    s.add_argument("--min-relevance", type=float, default=0.35)
    s.add_argument("--search-interval", type=float, default=d["search_min_interval"])
    s.add_argument("--out", default="reports")
    s.add_argument("--quick", action="store_true",
                   help="core topics only; skips adjacent topics and keywords")
    s.add_argument("--topics", default=None,
                   help="comma-separated topic list to scan instead of the "
                        "built-in sweep, e.g. --topics llm,rag,ai-agents")
    s.add_argument("--include-lists", action="store_true",
                   help="keep awesome-lists, courses and roadmaps in the rankings")
    s.add_argument("--no-enrich", dest="enrich", action="store_false",
                   help="stage 1 only; no per-repo API calls")
    s.set_defaults(enrich=True, func=cmd_scan)

    r = sub.add_parser("rescore", help="re-score a stored run offline, so "
                                       "config.py weights can be tuned "
                                       "without spending search quota")
    r.add_argument("--run-id", default=None, help="defaults to the latest run")
    r.add_argument("--top", type=int, default=d["report_top_n"])
    r.add_argument("--max-per-owner", type=int, default=d["max_per_owner"])
    r.add_argument("--min-relevance", type=float, default=0.35)
    r.add_argument("--include-lists", action="store_true")
    r.add_argument("--out", default="reports")
    r.set_defaults(func=cmd_rescore)

    t = sub.add_parser("trend", help="snapshot history for one repo")
    t.add_argument("repo")
    t.set_defaults(func=cmd_trend)

    l = sub.add_parser("limits", help="show API quota")
    l.set_defaults(func=cmd_limits)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GitHubError as exc:
        print(f"github error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
