"""The experiment: is co-editing actually a pain signal?

Hypothesis: when several people edit the same AGENTS.md, a larger share of
their edits are *friction* (reversing, contradicting or repairing earlier
instructions) than when one person maintains the file alone.

Method: classify every human edit to the file with Jev, for a treatment group
(3+ editors) and a control group (1 editor), then compare friction rates with
Wilson intervals and a two-proportion z-test. A null result is a result.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from statistics import median
from urllib.parse import quote

from .github import GitHub
from .jev import Jev
from .scan import Result

EDIT_TYPES = {
    "new_rule": "Adds a new rule, convention, command or piece of context",
    "clarification": "Rewords or expands an existing rule without changing what it asks for",
    "reversal": "Removes, reverses or weakens a rule that was there before",
    "conflict_fix": "Resolves contradictory or duplicated instructions, or repairs guidance that had drifted out of date",
    "restructure": "Reorganizes or moves sections without changing meaning",
    "cleanup": "Typo, formatting or trivial wording fix",
}
FRICTION_TYPES = {"reversal", "conflict_fix"}
CONTRADICTS_THRESHOLD = 0.6

QUESTIONS = {
    "edit_type": {
        "type": "choice",
        "instructions": "What kind of change is this edit to an AI agent instructions file (AGENTS.md / CLAUDE.md)?",
        "criteria": EDIT_TYPES,
    },
    "contradicts_prior": {
        "type": "noul",
        "instructions": "This edit removes, reverses or contradicts guidance that was previously in the file",
    },
}

MAX_PATCH_CHARS = 6000


@dataclass
class GroupStats:
    name: str
    repos: int = 0
    edits: int = 0
    friction: int = 0
    per_repo_rates: list = field(default_factory=list)
    types: dict = field(default_factory=dict)
    records: list = field(default_factory=list)  # one per classified edit, for hand validation

    @property
    def rate(self) -> float:
        return self.friction / self.edits if self.edits else 0.0

    def wilson(self, z: float = 1.96) -> tuple[float, float]:
        n, p = self.edits, self.rate
        if not n:
            return (0.0, 0.0)
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        return (max(0.0, centre - half), min(1.0, centre + half))

    def summary(self) -> dict:
        lo, hi = self.wilson()
        return {
            "group": self.name,
            "repos": self.repos,
            "edits_classified": self.edits,
            "friction_edits": self.friction,
            "friction_rate": round(self.rate, 4),
            "wilson_95": [round(lo, 4), round(hi, 4)],
            "median_repo_rate": round(median(self.per_repo_rates), 4) if self.per_repo_rates else None,
            "edit_types": self.types,
        }


def two_proportion_p(a: GroupStats, b: GroupStats) -> float | None:
    """Two-sided p-value for H0: friction rates are equal."""
    if not a.edits or not b.edits:
        return None
    pooled = (a.friction + b.friction) / (a.edits + b.edits)
    se = math.sqrt(pooled * (1 - pooled) * (1 / a.edits + 1 / b.edits))
    if se == 0:
        return 1.0
    z = (a.rate - b.rate) / se
    return math.erfc(abs(z) / math.sqrt(2))


def _patch_for(gh: GitHub, repo: str, sha: str, path: str) -> tuple[str, str] | None:
    data = gh.get(f"/repos/{repo}/commits/{quote(sha)}")
    if not data:
        return None
    message = ((data.get("commit") or {}).get("message") or "").splitlines()[:1]
    for f in data.get("files") or []:
        if f.get("filename") == path and f.get("patch"):
            return f["patch"][:MAX_PATCH_CHARS], (message[0] if message else "")
    return None


def is_friction(answers: dict) -> tuple[str, bool]:
    edit_type = ((answers.get("edit_type") or {}).get("choice")) or "unknown"
    contradicts = (answers.get("contradicts_prior") or {}).get("noul", 0.0) or 0.0
    return edit_type, (edit_type in FRICTION_TYPES or contradicts >= CONTRADICTS_THRESHOLD)


def classify_repo(gh: GitHub, jev: Jev, res: Result, stats: GroupStats, edits_per_repo: int, log=print) -> None:
    done = friction = 0
    for sha in res.human_shas[:edits_per_repo]:
        got = _patch_for(gh, res.repo, sha, res.path)
        if not got:
            continue
        patch, message = got
        state = json.dumps({"file": res.path, "commit_message": message, "diff": patch})
        try:
            answers = jev.evaluate(state, QUESTIONS)
        except Exception as e:  # one bad call shouldn't sink the run
            log(f"  jev failed on {res.repo}@{sha[:7]}: {e}")
            continue
        edit_type, fr = is_friction(answers)
        stats.records.append({
            "repo": res.repo, "sha": sha, "file": res.path, "commit_message": message, "diff": patch,
            "jev_edit_type": edit_type,
            "jev_contradicts": (answers.get("contradicts_prior") or {}).get("noul"),
            "jev_friction": fr,
        })
        stats.types[edit_type] = stats.types.get(edit_type, 0) + 1
        done += 1
        friction += fr
    if done:
        stats.repos += 1
        stats.edits += done
        stats.friction += friction
        stats.per_repo_rates.append(friction / done)
        res.edits_classified, res.friction_edits = done, friction
        res.friction_rate = round(friction / done, 3)


def verdict(treat: GroupStats, control: GroupStats, min_edits: int = 50) -> tuple[str, str]:
    if treat.edits < min_edits or control.edits < min_edits:
        return ("insufficient data", f"Need at least {min_edits} classified edits per group.")
    p = two_proportion_p(treat, control)
    ratio = treat.rate / control.rate if control.rate else float("inf")
    if p is not None and p < 0.05 and ratio >= 1.3:
        return ("supported", f"Co-edited files show {ratio:.1f}x the friction rate of single-author files (p={p:.3g}).")
    if p is not None and p < 0.05 and treat.rate < control.rate:
        return ("contradicted", f"Co-edited files show *less* friction than single-author files (p={p:.3g}).")
    return ("inconclusive", f"No clear difference: ratio {ratio:.2f}, p={p:.3g}." if p is not None else "No clear difference.")


def run_experiment(gh: GitHub, jev: Jev, treatment: list[Result], control: list[Result],
                   edits_per_repo: int, log=print) -> dict:
    t, c = GroupStats("3+ editors"), GroupStats("1 editor (control)")
    for label, group, stats in (("treatment", treatment, t), ("control", control, c)):
        log(f"  classifying {label}: {len(group)} repos")
        for i, r in enumerate(group, 1):
            classify_repo(gh, jev, r, stats, edits_per_repo, log)
            if i % 10 == 0:
                log(f"    {i}/{len(group)} repos, {stats.edits} edits")
    status, detail = verdict(t, c)
    p = two_proportion_p(t, c)
    return {
        "hypothesis": "Files co-edited by 3+ people have a higher share of friction edits "
                      "(reversals, contradictions, drift repairs) than single-author files.",
        "verdict": status,
        "detail": detail,
        "p_value": round(p, 6) if p is not None else None,
        "ratio": round(t.rate / c.rate, 3) if c.rate else None,
        "treatment": t.summary(),
        "control": c.summary(),
        "classifier": {"model": jev.model, "calls": jev.calls},
        "friction_definition": f"edit_type in {sorted(FRICTION_TYPES)} or contradicts_prior >= {CONTRADICTS_THRESHOLD}",
        # per-edit records; the caller writes these to their own file, not experiment.json
        "edits": [dict(r, group="team") for r in t.records] + [dict(r, group="solo") for r in c.records],
    }
