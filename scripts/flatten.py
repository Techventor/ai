#!/usr/bin/env python3
"""Flatten a radar report into one flat record per repository.

    python3 scripts/flatten.py reports/latest.json site/radar-rows.json \
        --run-out site/radar-run.json

The report groups repos under "influence" and "momentum", and a repo can
appear in both. Anything that reads tabular data -- a web page, a
spreadsheet, a notebook -- wants the opposite shape: one row per repo,
scalars only, with each ranking as its own column. Nested values (topics,
flags, component scores) are flattened to strings or separate columns, so
nothing needs a second parsing pass.

With --run-out it also writes a one-row file describing the scan itself,
so a page can state how its numbers were gathered instead of hardcoding
figures that go stale.

Output is JSON, not CSV: descriptions contain commas and naive splitters
mangle them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Component scores worth exposing as their own columns.
COMPONENTS = ["stars", "community", "alive", "momentum", "forks",
              "maturity", "endurance", "abs_growth", "rel_growth", "accel"]


def flatten(report: dict) -> list[dict]:
    rows: dict[str, dict] = {}

    for ranking in ("influence", "momentum"):
        for position, repo in enumerate(report.get(ranking) or [], 1):
            name = repo["full_name"]
            row = rows.get(name)
            if row is None:
                components = repo.get("components") or {}
                row = {
                    "full_name": name,
                    "owner": name.split("/")[0],
                    "name": name.split("/")[-1],
                    "url": repo.get("url"),
                    "description": (repo.get("description") or "").strip(),
                    "category": repo.get("category"),
                    "language": repo.get("language") or "",
                    "license": repo.get("license") or "",
                    "stars": repo.get("stars"),
                    "forks": repo.get("forks"),
                    "weekly_stars": repo.get("weekly_stars"),
                    "contributors": repo.get("contributors"),
                    "commits_90d": repo.get("commits_90d"),
                    "age_days": repo.get("age_days"),
                    "idle_days": repo.get("idle_days"),
                    "relevance": repo.get("relevance"),
                    "influence_score": repo.get("influence"),
                    "momentum_score": repo.get("momentum"),
                    "momentum_src": repo.get("momentum_src"),
                    # fork:star ratio is the anti-star-farm signal, so it is
                    # a column rather than something to recompute downstream
                    "fork_ratio": round(
                        (repo.get("forks") or 0) / max(repo.get("stars") or 1, 1), 4),
                    "flags": ", ".join(repo.get("flags") or []),
                    "flag_count": len(repo.get("flags") or []),
                    "topics": ", ".join((repo.get("topics") or [])[:8]),
                    "rank_influence": None,
                    "rank_momentum": None,
                }
                for key in COMPONENTS:
                    row[f"c_{key}"] = components.get(key)
                rows[name] = row
            row[f"rank_{ranking}"] = position

    out = list(rows.values())
    out.sort(key=lambda r: (r["rank_influence"] is None,
                            r["rank_influence"] or 1e9))
    return out


def run_summary(report: dict, rows: list[dict]) -> list[dict]:
    """One row describing the scan, so a page can cite it rather than
    hardcode figures that drift out of date."""
    meta = report.get("meta", {})
    return [{
        "generated_at": report.get("generated_at"),
        "candidates": meta.get("candidates"),
        "search_requests": meta.get("queries") or 0,
        "ranked": len(rows),
        "in_both_rankings": sum(
            1 for r in rows if r["rank_influence"] and r["rank_momentum"]),
        "flagged": sum(1 for r in rows if r["flags"]),
        "momentum_source": meta.get("momentum_source"),
        "dropped_signals":
            ", ".join(meta.get("dropped_signals") or []) or "none",
        "enriched": meta.get("enriched") or 0,
        "degraded": bool(meta.get("degraded")),
        "min_stars": meta.get("min_stars"),
    }]


def main() -> int:
    p = argparse.ArgumentParser(
        description="Flatten a radar report into one row per repository.")
    p.add_argument("report", help="a radar JSON report, e.g. reports/latest.json")
    p.add_argument("rows_out", help="where to write the flat row array")
    p.add_argument("--run-out", default=None,
                   help="also write a one-row summary of the scan here")
    args = p.parse_args()

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    rows = flatten(report)

    out = Path(args.rows_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"{len(rows)} rows -> {out}")

    if args.run_out:
        run_out = Path(args.run_out)
        run_out.parent.mkdir(parents=True, exist_ok=True)
        run_out.write_text(
            json.dumps(run_summary(report, rows), ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"1 run summary -> {run_out}")

    meta = report.get("meta", {})
    both = sum(1 for r in rows if r["rank_influence"] and r["rank_momentum"])
    print(f"  {both} appear in both rankings")
    print(f"  candidates scanned: {meta.get('candidates')}, "
          f"momentum source: {meta.get('momentum_source')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
