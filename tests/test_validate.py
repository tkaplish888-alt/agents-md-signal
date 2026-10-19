"""Offline tests for hand-validating Jev's labels."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from agentsignal import validate
from agentsignal.__main__ import run
from agentsignal.github import GitHub
from agentsignal.jev import Jev
from tests.test_experiment import ExperimentTest, fake_fetch, fake_jev_post
from tests.test_pipeline import fake_claude


def edits(n_team, n_solo):
    mk = lambda g, i: {"group": g, "repo": f"{g}{i}/app", "sha": f"{g}{i}", "commit_message": "m", "diff": "d",
                       "jev_edit_type": "new_rule", "jev_contradicts": 0.1, "jev_friction": False}
    return [mk("team", i) for i in range(n_team)] + [mk("solo", i) for i in range(n_solo)]


class SampleTest(unittest.TestCase):
    def test_even_split_and_blind(self):
        rows, key = validate.draw_sample(edits(100, 100), 25, seed=1)
        self.assertEqual(len(rows), 50)
        self.assertEqual(sum(k["group"] == "team" for k in key.values()), 25)
        self.assertEqual(sum(k["group"] == "solo" for k in key.values()), 25)
        self.assertFalse(any("jev" in f for f in rows[0]))  # Jev's answers never appear on the sheet
        self.assertEqual(validate.draw_sample(edits(100, 100), 25, seed=1)[0], rows)  # reproducible

    def test_small_group_takes_what_exists(self):
        rows, _ = validate.draw_sample(edits(100, 3), 25, seed=0)
        self.assertEqual(len(rows), 28)


class ScoreTest(unittest.TestCase):
    def test_kappa(self):
        self.assertEqual(validate.cohen_kappa([("a", "a"), ("b", "b")]), 1.0)
        self.assertAlmostEqual(validate.cohen_kappa([("a", "b"), ("b", "a")]), -1.0)

    def key(self):
        return {f"e{i}": {"group": "team" if i % 2 else "solo", "jev_edit_type": "reversal" if i < 4 else "new_rule",
                          "jev_friction": i < 4} for i in range(10)}

    def test_perfect_and_total_disagreement(self):
        agree = {f"e{i}": ("reversal" if i < 4 else "new_rule", False) for i in range(10)}
        r = validate.score_labels(agree, self.key())
        self.assertEqual(r["all"]["friction_agreement"], 1.0)
        self.assertEqual(r["verdict"], "high")
        flipped = {f"e{i}": ("new_rule" if i < 4 else "reversal", False) for i in range(10)}
        r = validate.score_labels(flipped, self.key())
        self.assertEqual(r["all"]["friction_agreement"], 0.0)
        self.assertEqual(r["verdict"], "low")
        self.assertEqual(r["type_disagreements"][("new_rule", "reversal")], 4)

    def test_contradicts_makes_friction(self):
        r = validate.score_labels({"e0": ("new_rule", True)}, self.key())  # Jev: friction; me: friction via contradicts
        self.assertEqual(r["all"]["friction_agreement"], 1.0)

    def test_read_labels_skips_blank_and_reports_bad(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "s.csv")
            p.write_text("id,commit_message,diff,my_edit_type,my_contradicts\n"
                         "e1,m,d,,\ne2,m,d,Reversal,Y\ne3,m,d,nonsense,n\ne4,m,d,cleanup,maybe\n")
            labels, problems = validate.read_labels(str(p))
        self.assertEqual(labels, {"e2": ("reversal", True)})
        self.assertEqual(len(problems), 2)


class EndToEndTest(ExperimentTest):
    def test_experiment_to_sample_to_score(self):
        out = Path(self.tmp.name, "out")
        run(self.args(out=str(out)), self.gh, api_key="k", post=fake_claude, jev=self.jev)
        self.assertNotIn("edits", json.loads((out / "experiment.json").read_text()))
        self.assertEqual(len(json.loads((out / "edits.json").read_text())), 240)

        self.assertEqual(validate.main(["sample", "--out", str(out), "--per-group", "25"]), 0)
        self.assertEqual(validate.main(["sample", "--out", str(out)]), 1)  # won't clobber labels
        with open(out / "label_sheet.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 50)

        # label like a careful human: REVERSE edits are reversals, the rest are new rules
        for r in rows:
            rev = "REVERSE" in r["diff"]
            r["my_edit_type"], r["my_contradicts"] = ("reversal", "y") if rev else ("new_rule", "n")
        with open(out / "label_sheet.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=validate.SHEET_FIELDS)
            w.writeheader()
            w.writerows(rows)
        key = json.loads((out / "label_key.json").read_text())
        r = validate.score_labels(validate.read_labels(str(out / "label_sheet.csv"))[0], key)
        self.assertEqual((r["n"], r["all"]["friction_agreement"], r["verdict"]), (50, 1.0, "high"))
        self.assertEqual(validate.main(["score", "--out", str(out)]), 0)


if __name__ == "__main__":
    unittest.main()
