"""Discovery, per-repo analysis and scoring.

Core idea: a big AGENTS.md is not the signal (tools generate those). The
signal is several *different people* editing the same agent-context file,
recently. That is a team negotiating shared context by hand.
"""

from __future__ import annotations

import base64
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from .github import GitHub

# GitHub code search returns at most 1,000 results per query, so we slice by
# file size (bytes) to see more of the population. Upper bound is GitHub's
# 384 KB indexing limit.
SIZE_BUCKETS = [
    (2_000, 4_000),
    (4_000, 6_000),
    (6_000, 9_000),
    (9_000, 14_000),
    (14_000, 22_000),
    (22_000, 40_000),
    (40_000, 384_000),
]

BOT_PATTERN = re.compile(
    r"\[bot\]|github-actions|dependabot|renovate|copilot|devin-ai|cursoragent|"
    r"noreply@anthropic\.com|claude\[bot\]|codex|\bbot\b",
    re.IGNORECASE,
)


@dataclass
class Candidate:
    repo: str
    owner: str
    owner_type: str
    path: str
    size_min: int
    html_url: str
    filenames: set = field(default_factory=set)


@dataclass
class Result:
    repo: str
    owner: str
    path: str
    file_url: str
    size_bytes: int
    authors_90d: int
    authors_all: int
    commits_90d: int
    commits_all: int
    last_edit: str | None
    stars: int
    pushed_at: str | None
    repo_description: str
    org_name: str
    org_website: str
    org_location: str
    org_description: str
    score: float = 0.0
    tier: str = "C"
    # filled by enrich
    segment: str = ""
    what_they_build: str = ""
    context_signal: str = ""
    opener_hook: str = ""
    fit_score: float = 0.0
    excerpt: str = ""

    def row(self) -> dict:
        d = asdict(self)
        d.pop("excerpt", None)
        return d


# -- discovery ----------------------------------------------------------------
def discover(
    gh: GitHub,
    filenames: list[str],
    min_size: int,
    include_users: bool = False,
    log=print,
) -> dict[str, Candidate]:
    """Find repos containing agent-context files, org-owned and not forks."""
    found: dict[str, Candidate] = {}
    seen_items = 0
    for fn in filenames:
        for lo, hi in SIZE_BUCKETS:
            if hi <= min_size:
                continue
            lo = max(lo, min_size)
            query = f"filename:{fn} size:{lo}..{hi}"
            n = 0
            for item in gh.search_code(query):
                n += 1
                seen_items += 1
                repo = item.get("repository") or {}
                if repo.get("fork"):
                    continue
                owner = repo.get("owner") or {}
                if owner.get("type") != "Organization" and not include_users:
                    continue
                full = repo.get("full_name")
                path = item.get("path", fn)
                if not full:
                    continue
                cur = found.get(full)
                # prefer the shallowest (root-most) context file per repo
                if cur is None or path.count("/") < cur.path.count("/"):
                    found[full] = Candidate(
                        repo=full,
                        owner=owner.get("login", full.split("/")[0]),
                        owner_type=owner.get("type", ""),
                        path=path,
                        size_min=lo,
                        html_url=item.get("html_url", ""),
                        filenames=(cur.filenames if cur else set()) | {fn},
                    )
                else:
                    cur.filenames.add(fn)
            log(f"  {query}: {n} files")
    log(f"Discovery: {seen_items} files scanned, {len(found)} candidate repos kept")
    return found


# -- analysis -------------------------------------------------------------------
def _identity(commit: dict) -> str | None:
    """Stable identity for a commit's human author, or None if it is a bot."""
    author = commit.get("author") or {}
    meta = (commit.get("commit") or {}).get("author") or {}
    login = author.get("login") or ""
    if author.get("type") == "Bot":
        return None
    probe = " ".join([login, meta.get("name", ""), meta.get("email", "")])
    if BOT_PATTERN.search(probe):
        return None
    if login:
        return "u:" + login.lower()
    email = (meta.get("email") or "").lower()
    return ("e:" + email) if email else None


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def analyze(gh: GitHub, cand: Candidate, since_days: int, now: datetime | None = None) -> Result | None:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=since_days)

    meta = gh.get(f"/repos/{cand.repo}")
    if not meta or meta.get("archived") or meta.get("fork"):
        return None

    commits = gh.paginate(f"/repos/{cand.repo}/commits", {"path": cand.path}, max_pages=3)
    if not commits:
        return None

    all_ids, recent_ids = set(), set()
    recent_commits = 0
    last_edit = None
    for c in commits:
        ts = _parse_ts(((c.get("commit") or {}).get("author") or {}).get("date"))
        if ts and (last_edit is None or ts > last_edit):
            last_edit = ts
        ident = _identity(c)
        if ident:
            all_ids.add(ident)
        if ts and ts >= cutoff:
            recent_commits += 1
            if ident:
                recent_ids.add(ident)

    org = gh.get(f"/orgs/{cand.owner}") if cand.owner_type == "Organization" else None
    org = org or {}

    return Result(
        repo=cand.repo,
        owner=cand.owner,
        path=cand.path,
        file_url=cand.html_url,
        size_bytes=cand.size_min,
        authors_90d=len(recent_ids),
        authors_all=len(all_ids),
        commits_90d=recent_commits,
        commits_all=len(commits),
        last_edit=last_edit.isoformat() if last_edit else None,
        stars=meta.get("stargazers_count", 0),
        pushed_at=meta.get("pushed_at"),
        repo_description=meta.get("description") or "",
        org_name=org.get("name") or cand.owner,
        org_website=org.get("blog") or meta.get("homepage") or "",
        org_location=org.get("location") or "",
        org_description=org.get("description") or "",
    )


def fetch_excerpt(gh: GitHub, res: Result, limit: int = 4000) -> None:
    """Load the file itself: exact size plus an excerpt for enrichment."""
    data = gh.get(f"/repos/{res.repo}/contents/{quote(res.path)}")
    if not data or data.get("encoding") != "base64":
        return
    res.size_bytes = data.get("size", res.size_bytes)
    try:
        text = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
    except ValueError:
        return
    res.excerpt = text[:limit]


# -- scoring ---------------------------------------------------------------------
def score(res: Result, since_days: int, now: datetime | None = None) -> None:
    """0-100. Weighted toward multiple humans co-editing the file recently."""
    now = now or datetime.now(timezone.utc)
    s = 0.0
    s += min(res.authors_90d, 6) / 6 * 45          # team co-editing, recent
    s += min(res.authors_all, 10) / 10 * 15        # team co-editing, ever
    s += min(res.commits_90d, 20) / 20 * 15        # how actively it is maintained
    kb = max(res.size_bytes, 1) / 1000
    s += max(0.0, min(1.0, math.log2(kb / 2) / 5)) * 10   # 2KB -> 0, 64KB -> full
    last = _parse_ts(res.last_edit)
    if last:
        age = (now - last).days
        s += 10 if age <= 30 else 5 if age <= since_days else 0
    s += 5 if res.org_website else 0               # a reachable company
    res.score = round(s, 1)
    if res.authors_90d >= 3:
        res.tier = "A"
    elif res.authors_90d == 2 or res.authors_all >= 3:
        res.tier = "B"
    else:
        res.tier = "C"
    res.fit_score = res.score


SEGMENT_WEIGHT = {
    "product_company": 1.0,
    "devtools_company": 1.0,
    "oss_project": 0.75,
    "agency": 0.7,
    "education": 0.4,
    "personal": 0.4,
    "other": 0.6,
}


def apply_segment(res: Result) -> None:
    w = SEGMENT_WEIGHT.get(res.segment, 0.6) if res.segment else 1.0
    res.fit_score = round(res.score * w, 1)


def collapse_by_org(results: list[Result]) -> list[Result]:
    """One row per org: keep its strongest repo."""
    best: dict[str, Result] = {}
    for r in results:
        cur = best.get(r.owner)
        if cur is None or r.score > cur.score:
            best[r.owner] = r
    return list(best.values())
