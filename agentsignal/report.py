"""Write targets.csv, targets.json and a readable report.md."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .scan import Result


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def experiment_lines(exp: dict) -> list[str]:
    t, c = exp["treatment"], exp["control"]

    def row(g):
        lo, hi = g["wilson_95"]
        med = f"{g['median_repo_rate']:.1%}" if g["median_repo_rate"] is not None else "–"
        return (f"| {g['group']} | {g['repos']} | {g['edits_classified']} | {g['friction_edits']} | "
                f"**{g['friction_rate']:.1%}** ({lo:.1%}–{hi:.1%}) | {med} |")

    types = sorted(set(t["edit_types"]) | set(c["edit_types"]))
    lines = [
        "## Hypothesis test",
        "",
        f"**Hypothesis:** {exp['hypothesis']}",
        "",
        f"**Verdict: {exp['verdict']}.** {exp['detail']}",
        "",
        "| Group | Repos | Edits classified | Friction edits | Friction rate (95% CI) | Median repo rate |",
        "|---|---|---|---|---|---|",
        row(t),
        row(c),
        "",
        "| Edit type | 3+ editors | 1 editor |",
        "|---|---|---|",
    ]
    for k in types:
        lines.append(f"| {k} | {t['edit_types'].get(k, 0)} | {c['edit_types'].get(k, 0)} |")
    lines += [
        "",
        f"Friction = {exp['friction_definition']}. Each edit's diff and commit message were classified by "
        f"Jev ({exp['classifier']['model']}, {exp['classifier']['calls']} calls). Intervals are Wilson 95%; "
        "p-value from a two-proportion z-test.",
        "",
    ]
    return lines


def rules_context_lines(exp: dict) -> list[str]:
    rc = exp.get("rules_vs_context")
    if not rc or not rc.get("edits"):
        return []

    def pct(x):
        return f"{x:.1%}" if x is not None else "–"

    lo, hi = rc["context_share_ci"]
    p = rc["friction_vs_non_friction_p"]
    lines = [
        "## Rules vs. context",
        "",
        "SageOx's argument is that AGENTS.md should hold **rules**, and that **context** (decisions, history, "
        "what was tried, current status) belongs in a shared layer outside the file. This asks how much "
        "real-world AGENTS.md churn is context anyway.",
        "",
        f"- **{pct(rc['context_share'])} of edits add or change context** (95% CI {pct(lo)}–{pct(hi)}, n={rc['edits']}); "
        f"team files {pct(rc['context_share_team'])}, solo files {pct(rc['context_share_solo'])}.",
        f"- Context is {pct(rc['context_share_in_friction'])} of friction edits vs. {pct(rc['context_share_in_non_friction'])} "
        f"of other edits" + (f" (p={p:.3g})." if p is not None else "."),
        "",
        "| Kind | Edits |",
        "|---|---|",
    ]
    for k, v in sorted(rc["kinds"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |")
    lines += ["", f"Source of the split: {rc['source']}. Classified by Jev; not yet hand-validated.", ""]
    return lines


def write_outputs(results: list[Result], funnel: dict, params: dict, out_dir: str | Path,
                  experiment: dict | None = None) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ranked = sorted(results, key=lambda r: (r.fit_score, r.score), reverse=True)

    rows = [dict(rank=i + 1, **r.row()) for i, r in enumerate(ranked)]
    csv_path = out / "targets.csv"
    with csv_path.open("w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    (out / "targets.json").write_text(json.dumps({"params": params, "funnel": funnel, "targets": rows}, indent=2))
    if experiment:
        (out / "experiment.json").write_text(json.dumps(experiment, indent=2))

    tiers = {t: sum(1 for r in ranked if r.tier == t) for t in "ABC"}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "# AGENTS.md Signal report",
        "",
        f"Run: {now} · files: {', '.join(params.get('filenames', []))} · window: {params.get('since_days')} days",
        "",
        *(experiment_lines(experiment) if experiment else []),
        *(rules_context_lines(experiment) if experiment else []),
        "## Funnel",
        "",
        "| Stage | Count |",
        "|---|---|",
    ]
    for k, v in funnel.items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        f"Tiers: **A** (3+ people editing in window) {tiers['A']} · **B** {tiers['B']} · **C** {tiers['C']}",
        "",
        "## Top targets",
        "",
        "| # | Org | What they build | People editing (90d / ever) | Friction | Score | Why it's a signal |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ranked[:25], 1):
        org = f"[{_md(r.org_name)}]({r.org_website})" if r.org_website.startswith("http") else _md(r.org_name)
        fr = f"{r.friction_rate:.0%} of {r.edits_classified}" if r.friction_rate is not None else "–"
        lines.append(
            f"| {i} | {org} | {_md(r.what_they_build) or _md(r.repo_description)[:80]} | "
            f"{r.authors_90d} / {r.authors_all} | {fr} | {r.fit_score} | "
            f"{_md(r.context_signal) or f'[{r.path}]({r.file_url})'} |"
        )
    lines += [
        "",
        "## Method",
        "",
        "1. Code search for agent-context files, sliced by size to get past GitHub's 1,000-result cap.",
        "2. Keep org-owned, non-fork, non-archived repos (one context file per repo, shallowest path).",
        "3. Pull the file's commit history and count distinct human authors (bots and agent commits excluded).",
        "4. Score 0-100, weighted toward several people co-editing recently; collapse to one row per org.",
        "5. Test the hypothesis: classify each human edit with Jev, 3+ editor files vs single-author controls.",
        "6. Qualify the strongest targets with Claude and draft a specific opener.",
        "",
        "Only org-level data is reported. No individual names or emails are stored in outputs.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return {"csv": str(csv_path), "report": str(out / "report.md"), "count": len(ranked)}
