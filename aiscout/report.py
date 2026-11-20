"""Report rendering: Markdown for humans, JSON for everything else."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _k(n: float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def _trim(text: str | None, width: int) -> str:
    text = (text or "").replace("\n", " ").replace("|", "/").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _row(i: int, r: dict, *, show_momentum: bool) -> str:
    growth = f"+{_k(r['weekly_stars'])}/wk" if r["weekly_stars"] else "-"
    flags = " ".join(f"`{f}`" for f in r["flags"][:3]) or ""
    score = r["momentum"] if show_momentum else r["influence"]
    contributors = r.get("contributors")
    return (f"| {i} | [{r['full_name']}]({r['html_url']}) | {score:.1f} | "
            f"{_k(r['_d']['stars'])} | {growth} | "
            f"{_k(contributors) if contributors is not None else '-'} | "
            f"{r['category']} | {_trim(r.get('description'), 70)} {flags} |")


HEADER = ("| # | repo | score | stars | growth | contrib | category | "
          "what it is |\n|--:|---|--:|--:|--:|--:|---|---|")


def render_markdown(*, influence: list[dict], momentum: list[dict],
                    meta: dict) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# AI open-source radar — {now}",
        "",
        f"Scanned **{meta['candidates']}** candidate repositories from "
        f"**{meta['queries']}** search requests; "
        f"enriched **{meta['enriched']}**.",
        "",
    ]

    if meta.get("degraded"):
        lines += [
            "> **Degraded run.** Per-repo endpoints (contributors, commit "
            "activity) were unavailable, so `community` falls back to forks "
            "and `alive` to push recency. Scores are still comparable within "
            "this run, but contributor counts are absent.",
            "",
        ]
    if meta.get("momentum_source") == "lifetime":
        lines += [
            "> **No history yet, so the second ranking is not momentum.** "
            "With no earlier snapshot to diff against, growth can only be a "
            "lifetime average (`stars / age`). That measures how fast a "
            "project gathered stars *since it was created*, which is a "
            "launch rate, not a current one, and it favours young repos "
            "because their denominator is small. Two momentum signals "
            "(`rel_growth`, `accel`) carry no information in this mode and "
            "are dropped rather than scored, with their weight "
            "redistributed. Real week-over-week deltas appear from the "
            "second run onward, and the ranking becomes momentum then.",
            "",
        ]
    if meta.get("dropped_signals"):
        lines += ["> Signals dropped this run for lack of data: "
                  + ", ".join(f"`{s}`" for s in meta["dropped_signals"])
                  + ". Remaining weights were rescaled to sum to 1.", ""]

    lines += ["## Most influential", "",
              "Durable weight: popularity, community breadth, liveness and "
              "fork activity, with momentum as a minor term.", "", HEADER]
    lines += [_row(i, r, show_momentum=False) for i, r in enumerate(influence, 1)]

    if meta.get("momentum_source") == "lifetime":
        lines += ["", "## Fastest since launch", "",
                  "Average stars per week over each project's whole life. "
                  "Young repos dominate this by construction — read it as "
                  "*what landed hard*, not *what is growing now*.", "", HEADER]
    else:
        lines += ["", "## Strongest momentum", "",
                  "Ranked on recent star velocity, its share of total stars, "
                  "and acceleration against the preceding window — so small "
                  "fast-growing projects can outrank large stable ones.",
                  "", HEADER]
    lines += [_row(i, r, show_momentum=True) for i, r in enumerate(momentum, 1)]

    # category breakdown
    by_cat: dict[str, int] = {}
    for r in influence:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    if by_cat:
        lines += ["", "## Top list by category", ""]
        for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            picks = [r["full_name"] for r in influence if r["category"] == cat][:5]
            lines.append(f"- **{cat}** ({n}): " + ", ".join(f"`{p}`" for p in picks))

    flagged = [r for r in influence + momentum if r["flags"]]
    if flagged:
        lines += ["", "## Flagged", "",
                  "Not accusations — just things the score already "
                  "discounted, surfaced so you can judge for yourself.", "",
                  "| repo | flags |", "|---|---|"]
        seen = set()
        for r in sorted(flagged, key=lambda r: -r["influence"]):
            if r["full_name"] in seen:
                continue
            seen.add(r["full_name"])
            lines.append(f"| `{r['full_name']}` | "
                         + " ".join(f"`{f}`" for f in r["flags"]) + " |")

    lines += ["", "---", "",
              "<sub>`growth` = stars/week (" + meta.get("momentum_source", "?")
              + "). `suspect-stars` = fork:star ratio below 1%, which real "
              "projects rarely show. Generated by ai-oss-radar.</sub>", ""]
    return "\n".join(lines)


def render_json(*, influence: list[dict], momentum: list[dict],
                meta: dict) -> str:
    def pack(r: dict) -> dict:
        return {
            "full_name": r["full_name"],
            "url": r["html_url"],
            "description": r.get("description"),
            "influence": r["influence"],
            "momentum": r["momentum"],
            "category": r["category"],
            "flags": r["flags"],
            "stars": r["_d"]["stars"],
            "forks": r["_d"]["forks"],
            "weekly_stars": r["weekly_stars"],
            "momentum_src": r["momentum_src"],
            "contributors": r.get("contributors"),
            "commits_90d": r.get("commits_90d"),
            "language": r.get("language"),
            "license": (r.get("license") or {}).get("spdx_id"),
            "topics": r.get("topics"),
            "age_days": round(r["_d"]["age_days"] or 0, 1),
            "idle_days": round(r["_d"]["idle_days"] or 0, 1),
            "relevance": round(r["_d"]["relevance"], 3),
            "components": r.get("components"),
        }

    return json.dumps({
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "meta": meta,
        "influence": [pack(r) for r in influence],
        "momentum": [pack(r) for r in momentum],
    }, indent=2, ensure_ascii=False)


def write_all(out_dir: str, *, influence: list[dict], momentum: list[dict],
              meta: dict) -> dict[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    paths = {
        "markdown": out / f"radar-{stamp}.md",
        "json": out / f"radar-{stamp}.json",
        "latest_md": out / "latest.md",
        "latest_json": out / "latest.json",
    }
    md = render_markdown(influence=influence, momentum=momentum, meta=meta)
    js = render_json(influence=influence, momentum=momentum, meta=meta)
    paths["markdown"].write_text(md, encoding="utf-8")
    paths["latest_md"].write_text(md, encoding="utf-8")
    paths["json"].write_text(js, encoding="utf-8")
    paths["latest_json"].write_text(js, encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}
