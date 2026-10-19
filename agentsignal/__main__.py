"""CLI: python -m agentsignal [options]"""

from __future__ import annotations

import argparse
import os
import sys

from .enrich import DEFAULT_MODEL, enrich
from .github import GitHub, GitHubError, resolve_token
from .report import write_outputs
from .scan import analyze, apply_segment, collapse_by_org, discover, fetch_excerpt, score


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentsignal",
        description="Find teams co-editing AGENTS.md / CLAUDE.md files: a public signal of shared-context pain.",
    )
    p.add_argument("--filenames", nargs="+", default=["AGENTS.md", "CLAUDE.md"])
    p.add_argument("--min-size", type=int, default=4000, help="ignore files smaller than this (bytes)")
    p.add_argument("--since-days", type=int, default=90, help="recency window for co-editing")
    p.add_argument("--max-repos", type=int, default=400, help="cap on repos analyzed (largest files first)")
    p.add_argument("--min-authors", type=int, default=2, help="keep repos with at least this many human editors ever")
    p.add_argument("--enrich-top", type=int, default=40, help="classify the top N orgs with Claude (0 = off)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--include-users", action="store_true", help="also include user-owned repos")
    p.add_argument("--out", default="out")
    p.add_argument("--cache", default=".cache")
    return p


def run(args, gh: GitHub, api_key: str | None, post=None) -> dict:
    log("1/4 Discovering agent-context files…")
    cands = discover(gh, args.filenames, args.min_size, args.include_users, log=log)
    funnel = {"Org-owned repos with a context file": len(cands)}

    ordered = sorted(cands.values(), key=lambda c: c.size_min, reverse=True)[: args.max_repos]
    funnel["Analyzed (largest files first)"] = len(ordered)

    log(f"2/4 Reading commit history for {len(ordered)} repos…")
    results = []
    for i, c in enumerate(ordered, 1):
        r = analyze(gh, c, args.since_days)
        if r and r.authors_all >= args.min_authors:
            score(r, args.since_days)
            results.append(r)
        if i % 25 == 0:
            log(f"  {i}/{len(ordered)} analyzed, {len(results)} multi-author so far")
    funnel[f"{args.min_authors}+ human editors"] = len(results)
    funnel["Edited by 3+ people in window (Tier A repos)"] = sum(1 for r in results if r.tier == "A")

    results = collapse_by_org(results)
    funnel["Unique organizations"] = len(results)

    top = sorted(results, key=lambda r: r.score, reverse=True)[: args.enrich_top]
    if args.enrich_top and api_key:
        log(f"3/4 Classifying top {len(top)} orgs with Claude ({args.model})…")
        for r in top:
            fetch_excerpt(gh, r)
            try:
                enrich(r, api_key, args.model, post=post)
            except Exception as e:  # keep going; enrichment is optional
                log(f"  enrich failed for {r.repo}: {e}")
            apply_segment(r)
    else:
        log("3/4 Skipping Claude enrichment (no ANTHROPIC_API_KEY or --enrich-top 0)")

    log("4/4 Writing outputs…")
    params = {k: v for k, v in vars(args).items() if k not in ("cache",)}
    info = write_outputs(results, funnel, params, args.out)
    log(f"Done: {info['count']} orgs → {info['report']} ({gh.calls} GitHub calls)")
    return info


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    token = resolve_token()
    if not token:
        log("No GitHub token found. Run `gh auth login` or set GITHUB_TOKEN (code search requires auth).")
        return 1
    gh = GitHub(token=token, cache_dir=args.cache, log=log)
    try:
        run(args, gh, os.environ.get("ANTHROPIC_API_KEY"))
    except GitHubError as e:
        log(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
