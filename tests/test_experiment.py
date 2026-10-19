"""Offline tests for the Jev experiment and the SageOx export."""

import json
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from agentsignal.__main__ import run
from agentsignal.experiment import GroupStats, is_friction, two_proportion_p, verdict
from agentsignal.github import GitHub
from agentsignal.jev import Jev
from tests.test_pipeline import commit, fake_claude, iso


def team_commits(repo, n_people, n_commits, friction_every):
    out = []
    for i in range(n_commits):
        c = commit(f"{repo.split('/')[0]}{i % n_people}", 2 + i)
        c["sha"] = f"{repo.replace('/', '-')}-{i}"
        c["_friction"] = friction_every and i % friction_every == 0
        out.append(c)
    return out


# treatment: 3+ editors, every 2nd edit is friction; control: 1 editor, every 6th edit
REPOS = {}
for k in range(6):
    REPOS[f"team{k}/app"] = dict(path="AGENTS.md", commits=team_commits(f"team{k}/app", 4, 20, 2))
    REPOS[f"solo{k}/app"] = dict(path="AGENTS.md", commits=team_commits(f"solo{k}/app", 1, 20, 6))
SHAS = {c["sha"]: (full, c) for full, s in REPOS.items() for c in s["commits"]}


def fake_fetch(url, headers):
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    parts = u.path.strip("/").split("/")

    def ok(d):
        return 200, {}, json.dumps(d)

    if u.path == "/search/code":
        if not q["q"].startswith("filename:AGENTS.md") or not q["q"].endswith("4000..6000"):
            return ok({"total_count": 0, "items": []})
        return ok({"total_count": len(REPOS), "items": [
            {"path": "AGENTS.md", "html_url": f"https://github.com/{f}/blob/main/AGENTS.md",
             "repository": {"full_name": f, "fork": False, "owner": {"login": f.split("/")[0], "type": "Organization"}}}
            for f in REPOS]})
    if parts[0] == "repos":
        full = "/".join(parts[1:3])
        spec = REPOS[full]
        if len(parts) == 3:
            return ok({"stargazers_count": 1, "pushed_at": iso(1), "archived": False, "fork": False,
                       "description": "d", "homepage": ""})
        if parts[3] == "commits" and len(parts) == 4:
            return ok(spec["commits"] if q.get("page") == "1" else [])
        if parts[3] == "commits" and len(parts) == 5:
            _, c = SHAS[parts[4]]
            marker = "REVERSE old rule" if c["_friction"] else "add new rule"
            return ok({"commit": {"message": marker + "\n\nbody"},
                       "files": [{"filename": "AGENTS.md", "patch": f"@@ -1 +1 @@\n-{marker}\n+{marker}"}]})
        if parts[3] == "contents":
            return ok({"encoding": "base64", "content": "", "size": 5000})
    if parts[0] == "orgs":
        return ok({"name": parts[1], "blog": f"https://{parts[1]}.dev"})
    return 404, {}, ""


def fake_jev_post(url, headers, body):
    req = json.loads(body)
    assert req["questions"]["edit_type"]["type"] == "choice"
    state = json.loads(req["state"])
    friction = "REVERSE" in state["diff"]
    answers = {
        "edit_type": {"type": "choice", "choice": "reversal" if friction else "new_rule", "confidence": 0.9},
        "contradicts_prior": {"type": "noul", "noul": 0.8 if friction else 0.1},
        "knowledge_kind": {"type": "choice", "choice": "context" if friction else "rule", "confidence": 0.9},
    }
    return 200, json.dumps({"model": "jev-test", "answers": answers, "usage": {"input_tokens": 100}})


class ExperimentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gh = GitHub(token="t", cache_dir=None, search_interval=0, fetch=fake_fetch,
                         sleep=lambda s: None, log=lambda m: None)
        self.jev = Jev("k", model="jev-test", post=fake_jev_post, sleep=lambda s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        base = dict(filenames=["AGENTS.md"], min_size=4000, since_days=90, max_repos=50, min_authors=2,
                    enrich_top=5, model="m", experiment_repos=10, edits_per_repo=20, jev_model="jev-test",
                    sageox_context=None, sageox_push=False, include_users=False, out=self.tmp.name, cache=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_experiment_detects_difference(self):
        info = run(self.args(), self.gh, api_key="k", post=fake_claude, jev=self.jev)
        exp = info["experiment"]
        self.assertEqual(exp["treatment"]["edits_classified"], 120)
        self.assertEqual(exp["control"]["edits_classified"], 120)
        self.assertAlmostEqual(exp["treatment"]["friction_rate"], 0.5)
        self.assertAlmostEqual(exp["control"]["friction_rate"], 0.2)  # edits 0,6,12,18 of 20
        self.assertEqual(exp["verdict"], "supported")
        report = Path(self.tmp.name, "report.md").read_text()
        self.assertIn("Hypothesis test", report)
        self.assertIn("Verdict: supported", report)
        self.assertTrue(Path(self.tmp.name, "experiment.json").exists())
        rc = exp["rules_vs_context"]
        self.assertEqual(rc["edits"], 240)
        self.assertAlmostEqual(rc["context_share"], 84 / 240, places=3)
        self.assertEqual(rc["context_share_in_friction"], 1.0)
        self.assertEqual(rc["context_share_in_non_friction"], 0.0)
        self.assertIn("Rules vs. context", report)

    def test_publishes_to_sageox_context_repo(self):
        ctx = Path(self.tmp.name, "team-context")
        ctx.mkdir()
        subprocess.run(["git", "init", "-q", str(ctx)], check=True)
        subprocess.run(["git", "-C", str(ctx), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(ctx), "config", "user.name", "t"], check=True)
        run(self.args(sageox_context=str(ctx), out=str(Path(self.tmp.name, "out"))),
            self.gh, api_key="k", post=fake_claude, jev=self.jev)
        files = list(ctx.glob("research/agents-md-signal/*.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        self.assertIn("Verdict: supported", text)
        self.assertIn("Top targets", text)
        log = subprocess.run(["git", "-C", str(ctx), "log", "--oneline"], capture_output=True, text=True).stdout
        self.assertIn("AGENTS.md Signal findings", log)

    def test_skips_experiment_without_key(self):
        info = run(self.args(), self.gh, api_key=None, jev=None)
        self.assertIsNone(info["experiment"])


class StatsTest(unittest.TestCase):
    def test_friction_rule(self):
        self.assertTrue(is_friction({"edit_type": {"choice": "conflict_fix"}})[1])
        self.assertTrue(is_friction({"edit_type": {"choice": "new_rule"}, "contradicts_prior": {"noul": 0.7}})[1])
        self.assertFalse(is_friction({"edit_type": {"choice": "cleanup"}, "contradicts_prior": {"noul": 0.2}})[1])

    def test_verdicts(self):
        a, b = GroupStats("a", edits=200, friction=60), GroupStats("b", edits=200, friction=20)
        self.assertEqual(verdict(a, b)[0], "supported")
        self.assertLess(two_proportion_p(a, b), 0.001)
        self.assertEqual(verdict(GroupStats("a", edits=200, friction=21), b)[0], "inconclusive")
        self.assertEqual(verdict(GroupStats("a", edits=200, friction=5), b)[0], "contradicted")
        self.assertEqual(verdict(GroupStats("a", edits=10, friction=5), b)[0], "insufficient data")

    def test_wilson_bounds(self):
        lo, hi = GroupStats("a", edits=100, friction=30).wilson()
        self.assertTrue(0.2 < lo < 0.3 < hi < 0.4)


if __name__ == "__main__":
    unittest.main()
