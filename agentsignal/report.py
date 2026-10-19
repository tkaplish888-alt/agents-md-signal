"""Write targets.csv, targets.json and a readable report.md."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .scan import Result


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def write_outputs(results: list[Result], funnel: dict, params: dict, out_dir: str | Path) -> dict:
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

    tiers = {t: sum(1 for r in ranked if r.tier == t) for t in "ABC"}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "# AGENTS.md Signal report",
        "",
        f"Run: {now} · files: {', '.join(params.get('filenames', []))} · window: {params.get('since_days')} days",
        "",
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
        "| # | Org | What they build | People editing (90d / ever) | Score | Why it's a signal |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ranked[:25], 1):
        org = f"[{_md(r.org_name)}]({r.org_website})" if r.org_website.startswith("http") else _md(r.org_name)
        lines.append(
            f"| {i} | {org} | {_md(r.what_they_build) or _md(r.repo_description)[:80]} | "
            f"{r.authors_90d} / {r.authors_all} | {r.fit_score} | "
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
        "5. Optionally classify the strongest targets with Claude and draft a specific opener.",
        "",
        "Only org-level data is reported. No individual names or emails are stored in outputs.",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return {"csv": str(csv_path), "report": str(out / "report.md"), "count": len(ranked)}
