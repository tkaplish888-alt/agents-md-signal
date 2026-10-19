"""CLI: python -m agentsignal [options]"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .enrich import DEFAULT_MODEL, enrich
from .experiment import run_experiment
from .github import GitHub, GitHubError, resolve_token
from .jev import DEFAULT_MODEL as JEV_MODEL
from .jev import Jev
from .report import write_outputs
from .sageox import findings_markdown, publish
from .scan import analyze, apply_segment, collapse_by_org, discover, fetch_excerpt, score


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentsignal",
        description="Find teams co-editing AGENTS.md / CLAUDE.md files, and test whether that co-editing is a pain signal.",
    )
    p.add_argument("--filenames", nargs="+", default=["AGENTS.md", "CLAUDE.md"])
    p.add_argument("--min-size", type=int, default=4000, help="ignore files smaller than this (bytes)")
    p.add_argument("--since-days", type=int, default=90, help="recency window for co-editing")
    p.add_argument("--max-repos", type=int, default=400, help="cap on repos analyzed (largest files first)")
    p.add_argument("--min-authors", type=int, default=2, help="keep repos with at least this many human editors ever")
    p.add_argument("--enrich-top", type=int, default=40, help="classify the top N orgs with Claude (0 = off)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Claude model for enrichment")
    p.add_argument("--experiment-repos", type=int, default=40, help="repos per group in the Jev experiment (0 = off)")
    p.add_argument("--edits-per-repo", type=int, default=15, help="most recent human edits classified per repo")
    p.add_argument("--jev-model", default=JEV_MODEL)
    p.add_argument("--sageox-context", default=os.environ.get("SAGEOX_CONTEXT_DIR"),
                   help="path to your SageOx Team Context repo; findings are written and committed there")
    p.add_argument("--sageox-push", action="store_true", help="also git push the SageOx context repo")
    p.add_argument("--include-users", action="store_true", help="also include user-owned repos")
    p.add_argument("--out", default="out")
    p.add_argument("--cache", default=".cache")
    return p


def run(args, gh: GitHub, api_key: str | None, post=None, jev: Jev | None = None) -> dict:
    log("1/6 Discovering agent-context files…")
    cands = discover(gh, args.filenames, args.min_size, args.include_users, log=log)
    funnel = {"Org-owned repos with a context file": len(cands)}

    ordered = sorted(cands.values(), key=lambda c: c.size_min, reverse=True)[: args.max_repos]
    funnel["Analyzed (largest files first)"] = len(ordered)

    log(f"2/6 Reading commit history for {len(ordered)} repos…")
    results, controls = [], []
    for i, c in enumerate(ordered, 1):
        r = analyze(gh, c, args.since_days)
        if r:
            score(r, args.since_days)
            if r.authors_all >= args.min_authors:
                results.append(r)
            elif r.authors_all == 1:
                controls.append(r)
        if i % 25 == 0:
            log(f"  {i}/{len(ordered)} analyzed, {len(results)} multi-author so far")
    funnel[f"{args.min_authors}+ human editors"] = len(results)
    funnel["Edited by 3+ people in window (Tier A repos)"] = sum(1 for r in results if r.tier == "A")
    funnel["Single-author files (control pool)"] = len(controls)

    experiment = None
    n = getattr(args, "experiment_repos", 0)
    if n and jev:
        treatment = sorted((r for r in results if r.authors_all >= 3), key=lambda r: r.score, reverse=True)[:n]
        control = sorted(controls, key=lambda r: r.commits_all, reverse=True)[:n]
        log(f"3/6 Testing the hypothesis with Jev ({len(treatment)} vs {len(control)} repos)…")
        experiment = run_experiment(gh, jev, treatment, control, args.edits_per_repo, log)
        log(f"  verdict: {experiment['verdict']}. {experiment['detail']}")
    else:
        log("3/6 Skipping the Jev experiment (no TYPESAFE_API_KEY or --experiment-repos 0)")

    results = collapse_by_org(results)
    funnel["Unique organizations"] = len(results)

    top = sorted(results, key=lambda r: r.score, reverse=True)[: args.enrich_top]
    if args.enrich_top and api_key:
        log(f"4/6 Qualifying top {len(top)} orgs with Claude ({args.model})…")
        for r in top:
            fetch_excerpt(gh, r)
            try:
                enrich(r, api_key, args.model, post=post)
            except Exception as e:  # keep going; enrichment is optional
                log(f"  enrich failed for {r.repo}: {e}")
            apply_segment(r)
    else:
        log("4/6 Skipping Claude enrichment (no ANTHROPIC_API_KEY or --enrich-top 0)")

    log("5/6 Writing outputs…")
    if experiment:  # per-edit records feed `validate sample`; kept out of experiment.json and the report
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "edits.json"), "w") as f:
            json.dump(experiment.pop("edits", []), f, indent=1)
    params = {k: v for k, v in vars(args).items() if k not in ("cache", "sageox_context")}
    info = write_outputs(results, funnel, params, args.out, experiment=experiment)

    if getattr(args, "sageox_context", None):
        log("6/6 Publishing findings to SageOx team context…")
        ranked = sorted(results, key=lambda r: (r.fit_score, r.score), reverse=True)[:15]
        try:
            publish(args.sageox_context, findings_markdown(experiment, funnel, ranked),
                    push=getattr(args, "sageox_push", False), log=log)
        except Exception as e:
            log(f"  SageOx publish failed: {e}")
    else:
        log("6/6 Skipping SageOx (pass --sageox-context or set SAGEOX_CONTEXT_DIR)")

    log(f"Done: {info['count']} orgs → {info['report']} ({gh.calls} GitHub calls)")
    info["experiment"] = experiment
    return info


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "validate":
        from .validate import main as validate_main
        return validate_main(argv[1:])
    args = build_parser().parse_args(argv)
    token = resolve_token()
    if not token:
        log("No GitHub token found. Run `gh auth login` or set GITHUB_TOKEN (code search requires auth).")
        return 1
    gh = GitHub(token=token, cache_dir=args.cache, log=log)
    jev_key = os.environ.get("TYPESAFE_API_KEY")
    jev = Jev(jev_key, model=args.jev_model) if jev_key else None
    try:
        run(args, gh, os.environ.get("ANTHROPIC_API_KEY"), jev=jev)
    except (GitHubError, RuntimeError) as e:
        log(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
