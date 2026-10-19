"""Validate Jev's labels against yours before trusting the ~30% friction rate.

    python -m agentsignal validate sample   # writes out/label_sheet.csv (blind) + out/label_key.json
    python -m agentsignal validate score    # compares your labels with Jev's

`sample` draws random edits from out/edits.json (written by a full run), split
evenly between team and solo files. The sheet hides Jev's answers so they can't
anchor you. `score` reports agreement on edit type and on friction, the
yes/no that actually drives the headline number.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter

from .experiment import EDIT_TYPES, FRICTION_TYPES, GroupStats

SHEET_FIELDS = ["id", "commit_message", "diff", "my_edit_type", "my_contradicts"]
KAPPA_HIGH, KAPPA_LOW = 0.6, 0.4  # Landis & Koch: 0.6+ substantial, below 0.4 fair/poor


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def draw_sample(edits: list[dict], per_group: int, seed: int) -> tuple[list[dict], dict]:
    """Random edits, per_group from each of team/solo, shuffled together. Returns (sheet rows, key)."""
    rng = random.Random(seed)
    picked = []
    for group in ("team", "solo"):
        pool = [e for e in edits if e.get("group") == group]
        if len(pool) < per_group:
            log(f"  warning: only {len(pool)} {group} edits available (wanted {per_group})")
        picked += rng.sample(pool, min(per_group, len(pool)))
    rng.shuffle(picked)
    rows, key = [], {}
    for i, e in enumerate(picked, 1):
        eid = f"e{i:02d}"
        rows.append({"id": eid, "commit_message": e["commit_message"], "diff": e["diff"],
                     "my_edit_type": "", "my_contradicts": ""})
        key[eid] = {k: e.get(k) for k in ("group", "repo", "sha", "jev_edit_type", "jev_contradicts", "jev_friction")}
    return rows, key


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    mine, theirs = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(mine[k] * theirs[k] for k in mine) / (n * n)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def read_labels(path: str) -> tuple[dict, list[str]]:
    """Returns ({id: (edit_type, contradicts_bool)}, problems). Blank rows are skipped, bad ones reported."""
    labels, problems = {}, []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            eid = (row.get("id") or "").strip()
            et = (row.get("my_edit_type") or "").strip().lower()
            ct = (row.get("my_contradicts") or "").strip().lower()
            if not et and not ct:
                continue
            if et not in EDIT_TYPES:
                problems.append(f"{eid}: my_edit_type {et!r} is not one of {sorted(EDIT_TYPES)}")
            elif ct not in ("y", "n", "yes", "no"):
                problems.append(f"{eid}: my_contradicts {ct!r} should be y or n")
            else:
                labels[eid] = (et, ct.startswith("y"))
    return labels, problems


def score_labels(labels: dict, key: dict) -> dict:
    """Compare hand labels with Jev's. Friction uses the same rule as the experiment."""
    rows = []
    for eid, (et, contradicts) in labels.items():
        j = key.get(eid)
        if not j:
            continue
        rows.append({
            "id": eid, "group": j["group"],
            "my_type": et, "jev_type": j["jev_edit_type"],
            "my_friction": et in FRICTION_TYPES or contradicts, "jev_friction": bool(j["jev_friction"]),
        })
    n = len(rows)
    out = {"n": n}
    if not n:
        return out

    def block(subset: list[dict]) -> dict:
        m = len(subset)
        if not m:
            return {"n": 0}
        t_ok = sum(r["my_type"] == r["jev_type"] for r in subset)
        f_ok = sum(r["my_friction"] == r["jev_friction"] for r in subset)
        lo, hi = GroupStats("", edits=m, friction=f_ok).wilson()
        tp = sum(r["my_friction"] and r["jev_friction"] for r in subset)
        return {
            "n": m,
            "type_agreement": t_ok / m,
            "type_kappa": cohen_kappa([(r["my_type"], r["jev_type"]) for r in subset]),
            "friction_agreement": f_ok / m,
            "friction_agreement_95": (lo, hi),
            "friction_kappa": cohen_kappa([(str(r["my_friction"]), str(r["jev_friction"])) for r in subset]),
            "my_friction_rate": sum(r["my_friction"] for r in subset) / m,
            "jev_friction_rate": sum(r["jev_friction"] for r in subset) / m,
            "jev_precision": tp / max(1, sum(r["jev_friction"] for r in subset)),
            "jev_recall": tp / max(1, sum(r["my_friction"] for r in subset)),
        }

    out["all"] = block(rows)
    out["team"] = block([r for r in rows if r["group"] == "team"])
    out["solo"] = block([r for r in rows if r["group"] == "solo"])
    out["type_disagreements"] = Counter((r["my_type"], r["jev_type"]) for r in rows if r["my_type"] != r["jev_type"])
    k = out["all"]["friction_kappa"]
    if k is None or k >= KAPPA_HIGH:
        out["verdict"] = "high"
    elif k < KAPPA_LOW:
        out["verdict"] = "low"
    else:
        out["verdict"] = "moderate"
    return out


VERDICTS = {
    "high": "Jev agrees with you on friction. The ~30% is real enough to act on.",
    "moderate": "Partial agreement. Read the disagreements below; the ~30% may be over- or under-stated "
                "(compare the two friction rates) and the criteria may need tightening.",
    "low": "Jev disagrees with you often. Fix the classification (criteria, threshold) before building on the ~30%.",
}


def format_report(r: dict) -> str:
    if not r["n"]:
        return "No labeled rows matched the key."
    pct = lambda x: f"{x * 100:.0f}%"
    a = r["all"]
    kap = lambda x: "n/a" if x is None else f"{x:.2f}"
    lines = [
        f"Labeled edits: {r['n']}",
        "",
        f"Friction (yes/no, drives the headline number)",
        f"  agreement   {pct(a['friction_agreement'])}  (95% CI {pct(a['friction_agreement_95'][0])}-{pct(a['friction_agreement_95'][1])}), kappa {kap(a['friction_kappa'])}",
        f"  friction rate  you {pct(a['my_friction_rate'])}  vs  Jev {pct(a['jev_friction_rate'])}",
        f"  when Jev says friction you agree {pct(a['jev_precision'])} of the time; Jev catches {pct(a['jev_recall'])} of yours",
        f"Edit type (6 categories)",
        f"  agreement   {pct(a['type_agreement'])}, kappa {kap(a['type_kappa'])}",
        "",
        "By group (friction agreement):",
    ]
    for g in ("team", "solo"):
        b = r[g]
        lines.append(f"  {g:<5} n={b['n']}" + (f"  {pct(b['friction_agreement'])}  you {pct(b['my_friction_rate'])} / Jev {pct(b['jev_friction_rate'])}" if b["n"] else ""))
    if r["type_disagreements"]:
        lines += ["", "Edit-type disagreements (you -> Jev):"]
        lines += [f"  {c}x  {m} -> {j}" for (m, j), c in r["type_disagreements"].most_common()]
    lines += ["", f"Verdict: agreement is {r['verdict']}. {VERDICTS[r['verdict']]}"]
    if r["n"] < 50:
        lines.append(f"Note: only {r['n']} labeled; the interval above is wide.")
    return "\n".join(lines)


def cmd_sample(args) -> int:
    path = os.path.join(args.out, "edits.json")
    if not os.path.exists(path):
        log(f"{path} not found. Re-run the full pipeline (with TYPESAFE_API_KEY) once to record per-edit labels.")
        return 1
    with open(path) as f:
        edits = json.load(f)
    sheet, key_path = os.path.join(args.out, "label_sheet.csv"), os.path.join(args.out, "label_key.json")
    if os.path.exists(sheet) and not args.force:
        log(f"{sheet} exists; labels in it would be lost. Pass --force to overwrite.")
        return 1
    rows, key = draw_sample(edits, args.per_group, args.seed)
    with open(sheet, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(key_path, "w") as f:
        json.dump(key, f, indent=1)
    log(f"Wrote {len(rows)} edits to {sheet} (seed {args.seed}).")
    log("Fill my_edit_type with one of: " + ", ".join(EDIT_TYPES))
    for name, desc in EDIT_TYPES.items():
        log(f"  {name}: {desc}")
    log("Fill my_contradicts with y/n: does the edit remove, reverse or contradict guidance that was previously in the file?")
    log("Then run: python -m agentsignal validate score")
    return 0


def cmd_score(args) -> int:
    sheet, key_path = os.path.join(args.out, "label_sheet.csv"), os.path.join(args.out, "label_key.json")
    if not (os.path.exists(sheet) and os.path.exists(key_path)):
        log("Run `python -m agentsignal validate sample` first.")
        return 1
    labels, problems = read_labels(sheet)
    with open(key_path) as f:
        key = json.load(f)
    for p in problems:
        log(f"  skipped {p}")
    unlabeled = len(key) - len(labels) - len(problems)
    if unlabeled:
        log(f"  {unlabeled} rows not labeled yet")
    print(format_report(score_labels(labels, key)))
    return 1 if problems else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentsignal validate", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="draw a blind hand-labeling sheet")
    s.add_argument("--per-group", type=int, default=25, help="edits from each of team and solo files")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--force", action="store_true", help="overwrite an existing label sheet")
    s.add_argument("--out", default="out")
    s.set_defaults(fn=cmd_sample)
    c = sub.add_parser("score", help="report agreement between your labels and Jev's")
    c.add_argument("--out", default="out")
    c.set_defaults(fn=cmd_score)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
