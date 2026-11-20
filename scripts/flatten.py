#!/usr/bin/env python3
"""Flatten a radar report into one flat record per repository.

    python3 scripts/flatten.py reports/latest.json rows.json

The report groups repos under "influence" and "momentum", and a repo can
appear in both. Anything that reads tabular data -- a dashboard, a
spreadsheet, a notebook -- wants the opposite shape: one row per repo,
scalars only, with each ranking as its own column. Nested values (topics,
flags, component scores) are flattened to strings or separate columns, so
nothing needs a second parsing pass.

Output is a JSON array, which loads as rows directly. CSV is avoided on
purpose: descriptions contain commas and naive splitters mangle them.
"""

from __future__ import annotations

import json
import sys
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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    rows = flatten(report)
    Path(argv[2]).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                             encoding="utf-8")

    meta = report.get("meta", {})
    both = sum(1 for r in rows if r["rank_influence"] and r["rank_momentum"])
    print(f"{len(rows)} rows -> {argv[2]}")
    print(f"  {both} appear in both rankings")
    print(f"  candidates scanned: {meta.get('candidates')}, "
          f"momentum source: {meta.get('momentum_source')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
