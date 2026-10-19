"""Publish findings into a SageOx Team Context repo.

SageOx Team Context is a real git repository: commit markdown to it and SageOx
syncs it, so every agent session primed with `ox agent prime` starts out
knowing what this research found (for example, when drafting outreach).
Docs: https://sageox.ai/docs/features/team-context
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .scan import Result


def findings_markdown(experiment: dict | None, funnel: dict, top: list[Result]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# AGENTS.md Signal findings ({today})", ""]
    if experiment:
        t, c = experiment["treatment"], experiment["control"]
        lines += [
            "## Hypothesis test",
            "",
            f"**Hypothesis:** {experiment['hypothesis']}",
            "",
            f"**Verdict: {experiment['verdict']}.** {experiment['detail']}",
            "",
            f"- 3+ editors: {t['friction_edits']}/{t['edits_classified']} friction edits "
            f"({t['friction_rate']:.1%}, 95% CI {t['wilson_95'][0]:.1%}–{t['wilson_95'][1]:.1%}) across {t['repos']} repos",
            f"- 1 editor (control): {c['friction_edits']}/{c['edits_classified']} friction edits "
            f"({c['friction_rate']:.1%}, 95% CI {c['wilson_95'][0]:.1%}–{c['wilson_95'][1]:.1%}) across {c['repos']} repos",
            f"- Friction = {experiment['friction_definition']}. Edits classified by Jev ({experiment['classifier']['model']}).",
            "",
        ]
    lines += ["## Funnel", ""] + [f"- {k}: {v}" for k, v in funnel.items()] + ["", "## Top targets", ""]
    for r in top:
        bits = [f"**{r.org_name}**", f"{r.authors_90d} editors in 90d"]
        if r.friction_rate is not None:
            bits.append(f"friction {r.friction_rate:.0%} of {r.edits_classified} edits")
        if r.what_they_build:
            bits.append(r.what_they_build)
        lines.append("- " + " · ".join(bits))
        if r.opener_hook:
            lines.append(f"  - Opener: {r.opener_hook}")
    lines += [
        "",
        "## How to use this",
        "",
        "Outreach should lead with the specific friction seen in each team's file, framed as a question, "
        "not a pitch. Treat the verdict above as the current state of the hypothesis; re-run to update.",
    ]
    return "\n".join(lines) + "\n"


def publish(context_dir: str | Path, markdown: str, commit: bool = True, push: bool = False, log=print) -> Path:
    root = Path(context_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"SageOx context folder not found: {root}")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = root / "research" / "agents-md-signal" / f"{today}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(markdown)
    log(f"  wrote {dest}")
    if commit and (root / ".git").exists():
        rel = str(dest.relative_to(root))
        subprocess.run(["git", "-C", str(root), "add", rel], check=True)
        r = subprocess.run(["git", "-C", str(root), "commit", "-m", f"AGENTS.md Signal findings {today}"],
                           capture_output=True, text=True)
        log("  committed to SageOx team context" if r.returncode == 0 else f"  nothing new to commit ({r.stdout.strip()[:80]})")
        if push:
            subprocess.run(["git", "-C", str(root), "push"], check=True)
            log("  pushed; SageOx will sync it")
    return dest
