"""Scoring: turn raw counts into two rankings and a set of honesty flags.

Design notes
------------
Star count is a lagging, gameable, winner-take-all metric. It is kept, but
log-scaled and held to about a quarter of the weight. The rest comes from
signals that are harder to fake and say more about whether a project
matters:

  * forks and contributors -- people building on it, not bookmarking it
  * push recency           -- exponential decay, 30-day half life
  * star delta             -- real momentum, from the snapshot store
  * licence / releases     -- can anyone actually depend on this

Every signal is converted to a percentile rank *within the candidate pool*
before weighting. That sidesteps hand-tuned constants, survives the power
law in every GitHub count, and keeps weights comparable.

"Influential" and "trending" are different questions, so they get separate
rankings rather than one blended number that answers neither.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field

from . import config


# --------------------------------------------------------------- utilities --

def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def percentile_ranks(values: list[float | None]) -> list[float]:
    """Rank each value in [0, 1]; None -> 0.0. Ties share the mean rank."""
    present = [(v, i) for i, v in enumerate(values) if v is not None]
    out = [0.0] * len(values)
    if not present:
        return out
    present.sort(key=lambda pair: pair[0])
    n = len(present)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][0] == present[i][0]:
            j += 1
        # mean rank of the tie group, normalised to [0, 1]
        rank = ((i + j) / 2.0) / max(n - 1, 1)
        for k in range(i, j + 1):
            out[present[k][1]] = rank
        i = j + 1
    return out


def _log(x: float | None) -> float | None:
    return None if x is None else math.log1p(max(0.0, x))


# -------------------------------------------------------------- derivation --

def _text_of(repo: dict) -> str:
    return " ".join([
        repo.get("full_name", ""),
        repo.get("description") or "",
        " ".join(repo.get("topics") or []),
    ]).lower()


def relevance(repo: dict) -> float:
    """How confidently AI-related this repo is, in [0, 1]."""
    text = _text_of(repo)
    hits = sum(1 for term in config.RELEVANCE_TERMS if term in text)
    topics = {t.lower() for t in (repo.get("topics") or [])}
    strong = len(topics & set(config.TOPICS_CORE))
    return min(1.0, (hits + 2.5 * strong) / 8.0)


def categorise(repo: dict) -> str:
    name = repo.get("name", "").lower()
    text = _text_of(repo)
    description = (repo.get("description") or "").lower()
    topics = {t.lower() for t in (repo.get("topics") or [])}

    # 1. Not-software wins outright: an awesome-list is an awesome-list
    #    whatever it is a list of.
    if any(p in name for p in config.NON_SOFTWARE_PATTERNS) or \
            any(t in text for t in ("awesome list", "curated list", "tutorial",
                                    "course", "roadmap")):
        return "learning/list"

    # 2. Self-description beats topic keywords -- see config.FRAMEWORK_SELFDESC.
    if any(t in description for t in config.FRAMEWORK_SELFDESC):
        return "framework/sdk"

    # 3. Topic sweep, specific categories first.
    for label, topic_terms, text_terms in config.CATEGORIES:
        if label == "learning/list":
            continue
        if topics & set(topic_terms) or any(t in text for t in text_terms):
            return label
    return config.DEFAULT_CATEGORY


def is_non_software(repo: dict) -> bool:
    name = repo.get("name", "").lower()
    return any(p in name for p in config.NON_SOFTWARE_PATTERNS)


def derive(repo: dict, now: dt.datetime) -> dict:
    """Attach the computed, unit-bearing metrics to one repo record."""
    stars = repo.get("stargazers_count") or 0
    forks = repo.get("forks_count") or 0
    created = _parse_ts(repo.get("created_at"))
    pushed = _parse_ts(repo.get("pushed_at"))

    age_days = max((now - created).total_seconds() / 86400.0, 1.0) if created else None
    idle_days = (now - pushed).total_seconds() / 86400.0 if pushed else None

    d = {
        "stars": stars,
        "forks": forks,
        "age_days": age_days,
        "idle_days": idle_days,
        "fork_ratio": (forks / stars) if stars else 0.0,
        "issue_ratio": ((repo.get("open_issues_count") or 0) / stars) if stars else 0.0,
        "category": categorise(repo),
        "relevance": relevance(repo),
        "non_software": is_non_software(repo),
        "owner_type": (repo.get("owner") or {}).get("type"),
        "has_license": bool((repo.get("license") or {}).get("spdx_id")
                            not in (None, "NOASSERTION")),
    }

    if idle_days is None or repo.get("archived"):
        d["alive"] = 0.0
    else:
        d["alive"] = 0.5 ** (max(idle_days, 0.0) / config.ALIVE_HALF_LIFE_DAYS)

    return d


def attach_momentum(repos: list[dict], baseline_7d: dict, baseline_30d: dict,
                    now: dt.datetime) -> None:
    """Weekly star rate, preferring measured deltas over a lifetime average."""
    for repo in repos:
        rid, stars = repo["id"], repo.get("stargazers_count") or 0
        short = baseline_7d.get(rid)    # (stars_then, days_ago)
        long = baseline_30d.get(rid)
        weekly, accel, source = None, None, "lifetime"

        near = short or long
        if near:
            then, days = near
            weekly = (stars - then) / days * 7.0
            source = f"delta/{days:.0f}d"

        # Acceleration compares two NON-OVERLAPPING stretches: the recent
        # one (now back to the short baseline) against the one before it
        # (short baseline back to the long baseline). Differencing against
        # the trailing 30-day average instead would double-count the recent
        # window and flatten the signal.
        if short and long and long[1] > short[1] + 1:
            gap = long[1] - short[1]
            earlier_weekly = (short[0] - long[0]) / gap * 7.0
            accel = weekly - earlier_weekly

        if weekly is None:
            age = repo["_d"]["age_days"] or 1.0
            weekly = stars / age * 7.0
            source = "lifetime"

        repo["_d"]["weekly_stars"] = max(weekly, 0.0)
        repo["_d"]["momentum_src"] = source

        # rel_growth is the share of total stars earned in the recent window.
        # It only means that for a measured delta. Under the lifetime
        # fallback weekly == stars/age*7, so weekly/stars collapses to
        # 7/age -- pure repo age, carrying no growth information at all.
        # Scoring it there would rank the youngest repos top and call it
        # momentum, so it is suppressed and its weight redistributed.
        repo["_d"]["rel_growth"] = (
            None if source == "lifetime" or stars <= 0 else weekly / stars)
        repo["_d"]["accel"] = accel


# ------------------------------------------------------------------ flags --

def flags_for(repo: dict) -> list[str]:
    d, rules = repo["_d"], config.FLAG_RULES
    out = []

    if repo.get("archived"):
        out.append("archived")
    if d["stars"] >= rules["suspect_stars_min"] and \
            d["fork_ratio"] < config.MIN_HEALTHY_FORK_RATIO:
        out.append("suspect-stars")
    contributors = repo.get("contributors")
    if contributors is not None and contributors <= 2 and \
            d["stars"] >= rules["solo_stars_min"]:
        out.append("solo-project")
    if d["idle_days"] is not None and d["idle_days"] > rules["dormant_days"]:
        out.append("dormant")
    if d["age_days"] is not None and d["age_days"] < rules["too_new_days"]:
        out.append("too-new")
    if not d["has_license"]:
        out.append("no-license")
    if d["issue_ratio"] > rules["issue_backlog_ratio"] and d["stars"] > 1000:
        out.append("issue-backlog")
    if d["non_software"]:
        out.append("not-software")
    if d["relevance"] < 0.35:
        out.append("weak-ai-signal")
    return out


# ------------------------------------------------------------------ score --

def _renormalise(weights: dict, dead: set) -> dict:
    """Drop unusable signals and rescale the rest back to a sum of 1."""
    kept = {k: v for k, v in weights.items() if k not in dead}
    total = sum(kept.values())
    if total <= 0:
        return {k: 1.0 / len(weights) for k in weights}
    return {k: v / total for k, v in kept.items()}


@dataclass
class Scorer:
    min_relevance: float = 0.35
    include_non_software: bool = False
    max_per_owner: int = config.DEFAULTS["max_per_owner"]
    dropped_signals: list = field(default_factory=list, init=False)

    def score(self, repos: list[dict], *, enriched: bool) -> list[dict]:
        now = dt.datetime.now(dt.timezone.utc)
        for repo in repos:
            repo["_d"] = derive(repo, now)
        return repos  # momentum + ranking happen in rank(), after baselines

    def rank(self, repos: list[dict], *, enriched: bool) -> list[dict]:
        if not repos:
            return []

        # --- percentile columns -------------------------------------------
        cols = {
            "stars":      percentile_ranks([_log(r["_d"]["stars"]) for r in repos]),
            "forks":      percentile_ranks([_log(r["_d"]["forks"]) for r in repos]),
            "endurance":  percentile_ranks([_log(r["_d"]["age_days"]) for r in repos]),
            "abs_growth": percentile_ranks([_log(r["_d"]["weekly_stars"]) for r in repos]),
            "rel_growth": percentile_ranks([r["_d"]["rel_growth"] for r in repos]),
            "accel":      percentile_ranks([r["_d"]["accel"] for r in repos]),
        }
        # The influence ranking spends one term on "is it still growing";
        # absolute weekly gain is the right reading of that there, whereas
        # the momentum ranking splits growth into absolute/relative/accel.
        cols["momentum"] = cols["abs_growth"]

        # Community breadth: contributors when stage 2 ran, forks otherwise.
        if enriched:
            # Clamp at the saturation point: above it the API's number says
            # "many", not how many, and ranking those apart is noise. Equal
            # values tie in percentile_ranks, so clamping makes them tie.
            cols["community"] = percentile_ranks(
                [_log(min(r["contributors"], config.CONTRIBUTOR_SATURATION))
                 if r.get("contributors") is not None else None
                 for r in repos])
            cols["alive"] = percentile_ranks(
                [_log(r.get("commits_90d")) if r.get("commits_90d") is not None
                 else r["_d"]["alive"] for r in repos])
        else:
            cols["community"] = cols["forks"]
            cols["alive"] = [r["_d"]["alive"] for r in repos]

        # Maturity is a small bounded bonus, not a percentile.
        cols["maturity"] = [
            min(1.0, 0.40 * r["_d"]["has_license"]
                + 0.25 * (r["_d"]["owner_type"] == "Organization")
                + 0.20 * bool(r.get("releases"))
                + 0.15 * bool(r.get("has_discussions")))
            for r in repos
        ]

        # A signal with no data anywhere in the pool ranks every repo 0,
        # so leaving it in the sum would silently spend its weight on
        # nothing and flatten the differences that remain. Drop it and
        # renormalise, and record what was dropped so the report can say so.
        # With one repo every percentile is 0 by definition, so the test
        # below would call every signal dead.
        dead = set() if len(repos) < 2 else {
            k for k, col in cols.items() if not any(v > 0 for v in col)}
        wi = _renormalise(config.WEIGHTS_INFLUENCE, dead)
        wm = _renormalise(config.WEIGHTS_MOMENTUM, dead)
        self.dropped_signals = sorted(dead & (set(config.WEIGHTS_INFLUENCE)
                                              | set(config.WEIGHTS_MOMENTUM)))

        for i, repo in enumerate(repos):
            d = repo["_d"]
            influence = sum(w * cols[k][i] for k, w in wi.items())
            momentum = sum(w * cols[k][i] for k, w in wm.items())

            # Relevance gates the result instead of being a weighted term:
            # a marginally-AI repo should not rank high on stars alone.
            gate = min(1.0, d["relevance"] / 0.6)
            flags = flags_for(repo)
            penalty = 1.0
            if "suspect-stars" in flags:
                penalty *= 0.55
            if "archived" in flags or "dormant" in flags:
                penalty *= 0.40
            if "not-software" in flags:
                penalty *= 0.70
            if "too-new" in flags:
                penalty *= 0.80

            repo["influence"] = round(100 * influence * gate * penalty, 2)
            repo["momentum"] = round(100 * momentum * gate
                                     * (0.55 if "suspect-stars" in flags else 1.0), 2)
            repo["flags"] = flags
            repo["category"] = d["category"]
            repo["weekly_stars"] = round(d["weekly_stars"], 1)
            repo["momentum_src"] = d["momentum_src"]
            repo["components"] = {k: round(cols[k][i], 3) for k in cols}

        return repos

    # ------------------------------------------------------------ shortlist

    def shortlist(self, repos: list[dict], key: str, top_n: int,
                  *, cap_per_owner: bool = True) -> list[dict]:
        pool = [
            r for r in repos
            if r["_d"]["relevance"] >= self.min_relevance
            and not r.get("archived")
            and (self.include_non_software or not r["_d"]["non_software"])
        ]
        pool.sort(key=lambda r: r[key], reverse=True)
        if not cap_per_owner or self.max_per_owner <= 0:
            return pool[:top_n]

        seen: dict[str, int] = {}
        out = []
        for repo in pool:
            owner = repo["full_name"].split("/")[0].lower()
            if seen.get(owner, 0) >= self.max_per_owner:
                continue
            seen[owner] = seen.get(owner, 0) + 1
            out.append(repo)
            if len(out) >= top_n:
                break
        return out
