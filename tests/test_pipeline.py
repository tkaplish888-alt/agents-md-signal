"""Offline tests: a fake GitHub + fake Claude exercise the whole pipeline."""

import base64
import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agentsignal.__main__ import run
from agentsignal.github import GitHub
from agentsignal.scan import Candidate, Result, _identity, analyze, score

NOW = datetime.now(timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit(login, days_ago, name=None, email=None, type_="User"):
    return {
        "author": {"login": login, "type": type_} if login else None,
        "commit": {"author": {"name": name or login, "email": email or f"{login}@x.dev", "date": iso(days_ago)}},
    }


REPOS = {
    # team of 4 actively co-editing -> Tier A
    "acme/platform": dict(owner_type="Organization", fork=False, path="AGENTS.md", commits=[
        commit("ana", 2), commit("ben", 5), commit("cy", 9), commit("dee", 20), commit("ana", 40),
        commit("dependabot[bot]", 3, type_="Bot"), commit(None, 1, name="Claude", email="noreply@anthropic.com"),
    ]),
    # two people -> Tier B
    "beta/app": dict(owner_type="Organization", fork=False, path="CLAUDE.md", commits=[
        commit("eve", 10), commit("fay", 30), commit("eve", 200),
    ]),
    # single author -> filtered out by min_authors
    "solo/tool": dict(owner_type="Organization", fork=False, path="AGENTS.md", commits=[commit("gus", 3)]),
    # personal repo -> dropped at discovery
    "jane/dotfiles": dict(owner_type="User", fork=False, path="AGENTS.md", commits=[commit("jane", 1)]),
    # fork -> dropped at discovery
    "fork/platform": dict(owner_type="Organization", fork=True, path="AGENTS.md", commits=[]),
}


def fake_fetch(url, headers):
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    path = u.path

    def ok(data):
        return 200, {}, json.dumps(data)

    if path == "/search/code":
        fn = q["q"].split()[0].split(":")[1]
        if not q["q"].endswith("4000..6000") or q.get("page") != "1":
            return ok({"total_count": 0, "items": []})
        items = [
            {
                "path": spec["path"],
                "html_url": f"https://github.com/{full}/blob/main/{spec['path']}",
                "repository": {"full_name": full, "fork": spec["fork"],
                               "owner": {"login": full.split("/")[0], "type": spec["owner_type"]}},
            }
            for full, spec in REPOS.items() if spec["path"] == fn
        ]
        return ok({"total_count": len(items), "items": items})

    parts = path.strip("/").split("/")
    if parts[0] == "repos":
        full = "/".join(parts[1:3])
        spec = REPOS.get(full)
        if not spec:
            return 404, {}, ""
        if len(parts) == 3:
            return ok({"stargazers_count": 42, "pushed_at": iso(1), "archived": False, "fork": spec["fork"],
                       "description": f"{full} repo", "homepage": ""})
        if parts[3] == "commits":
            return ok(spec["commits"] if q.get("page") == "1" else [])
        if parts[3] == "contents":
            body = base64.b64encode(b"# Conventions\nAll agents must run make test before PRs.\n").decode()
            return ok({"encoding": "base64", "content": body, "size": 5200})
    if parts[0] == "orgs":
        return ok({"name": parts[1].title() + " Inc", "blog": f"https://{parts[1]}.dev", "location": "Seattle"})
    return 404, {}, ""


def fake_claude(url, headers, body):
    req = json.loads(body)
    assert "file_excerpt" in req["messages"][0]["content"]
    text = json.dumps({"segment": "product_company", "what_they_build": "Payments platform",
                       "context_signal": "Shared rule that agents run tests before PRs",
                       "opener_hook": "Saw your AGENTS.md makes every agent run make test before a PR."})
    return 200, json.dumps({"content": [{"type": "text", "text": text}]})


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gh = GitHub(token="t", cache_dir=None, search_interval=0, fetch=fake_fetch,
                         sleep=lambda s: None, log=lambda m: None)

    def tearDown(self):
        self.tmp.cleanup()

    def args(self, **kw):
        base = dict(filenames=["AGENTS.md", "CLAUDE.md"], min_size=4000, since_days=90, max_repos=50,
                    min_authors=2, enrich_top=10, model="test", include_users=False,
                    out=self.tmp.name, cache=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_end_to_end(self):
        info = run(self.args(), self.gh, api_key="k", post=fake_claude)
        data = json.load(open(f"{self.tmp.name}/targets.json"))
        targets = {t["repo"]: t for t in data["targets"]}

        self.assertEqual(set(targets), {"acme/platform", "beta/app"})  # solo, user and fork dropped
        acme = targets["acme/platform"]
        self.assertEqual(acme["authors_90d"], 4)        # bots and agent commit excluded
        self.assertEqual(acme["tier"], "A")
        self.assertEqual(targets["beta/app"]["tier"], "B")
        self.assertEqual(acme["segment"], "product_company")
        self.assertEqual(data["targets"][0]["repo"], "acme/platform")  # ranked first
        self.assertEqual(info["count"], 2)

        report = open(f"{self.tmp.name}/report.md").read()
        self.assertIn("Acme Inc", report)
        self.assertNotIn("ana", report)  # no individual identities in outputs
        self.assertNotIn("ana@x.dev", open(f"{self.tmp.name}/targets.csv").read())

    def test_runs_without_claude(self):
        run(self.args(), self.gh, api_key=None)
        data = json.load(open(f"{self.tmp.name}/targets.json"))
        self.assertTrue(all(t["segment"] == "" for t in data["targets"]))

    def test_bot_detection(self):
        self.assertIsNone(_identity(commit("renovate[bot]", 1)))
        self.assertIsNone(_identity(commit("x", 1, type_="Bot")))
        self.assertIsNone(_identity(commit(None, 1, name="Claude", email="noreply@anthropic.com")))
        self.assertEqual(_identity(commit("Ana", 1)), "u:ana")

    def test_score_prefers_team_over_size(self):
        common = dict(repo="o/r", owner="o", path="AGENTS.md", file_url="", commits_all=5,
                      last_edit=iso(5), stars=0, pushed_at=iso(5), repo_description="", org_name="O",
                      org_website="https://o.dev", org_location="", org_description="")
        big_solo = Result(size_bytes=60000, authors_90d=1, authors_all=1, commits_90d=8, **common)
        small_team = Result(size_bytes=5000, authors_90d=4, authors_all=5, commits_90d=8, **common)
        score(big_solo, 90)
        score(small_team, 90)
        self.assertGreater(small_team.score, big_solo.score)


class RateLimitTest(unittest.TestCase):
    def test_retries_after_rate_limit(self):
        calls, sleeps = [], []

        def fetch(url, headers):
            calls.append(url)
            if len(calls) == 1:
                return 403, {"Retry-After": "7"}, "{}"
            return 200, {}, json.dumps({"ok": True})

        gh = GitHub(token="t", cache_dir=None, fetch=fetch, sleep=sleeps.append, log=lambda m: None)
        self.assertEqual(gh.get("/x"), {"ok": True})
        self.assertIn(7.0, sleeps)

    def test_caches_responses(self):
        n = []

        def fetch(url, headers):
            n.append(1)
            return 200, {}, json.dumps([1, 2])

        with tempfile.TemporaryDirectory() as d:
            gh = GitHub(token="t", cache_dir=d, fetch=fetch, log=lambda m: None)
            gh.get("/y")
            gh.get("/y")
        self.assertEqual(len(n), 1)


if __name__ == "__main__":
    unittest.main()
