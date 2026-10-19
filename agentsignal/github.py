"""Minimal GitHub REST client: auth, caching, pagination and rate-limit handling.

Standard library only. The network call is injectable so the pipeline can be
tested offline against fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterator

API = "https://api.github.com"

# fetch(url, headers) -> (status, response_headers, body_text)
Fetch = Callable[[str, dict], tuple[int, dict, str]]


def _urllib_fetch(url: str, headers: dict) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode("utf-8", "replace")


def resolve_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class GitHubError(RuntimeError):
    pass


class GitHub:
    """Small cached client. Code search is throttled to stay under 10 req/min."""

    def __init__(
        self,
        token: str | None = None,
        cache_dir: str | Path | None = ".cache",
        search_interval: float = 6.5,
        fetch: Fetch | None = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
    ):
        self.token = token
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.search_interval = search_interval
        self._fetch = fetch or _urllib_fetch
        self._sleep = sleep
        self._log = log
        self._last_search = 0.0
        self.calls = 0

    # -- low level -----------------------------------------------------------
    def _headers(self) -> dict:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agents-md-signal",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def get(self, path: str, params: dict | None = None, search: bool = False):
        """GET a REST path. Returns parsed JSON, or None for 404/409/451."""
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        cp = self._cache_path(url)
        if cp and cp.exists():
            return json.loads(cp.read_text())

        for attempt in range(6):
            if search:
                wait = self.search_interval - (time.monotonic() - self._last_search)
                if wait > 0:
                    self._sleep(wait)
                self._last_search = time.monotonic()

            status, headers, body = self._fetch(url, self._headers())
            self.calls += 1
            h = {k.lower(): v for k, v in headers.items()}

            if status == 200:
                data = json.loads(body) if body else None
                if cp:
                    cp.write_text(json.dumps(data))
                return data
            if status in (404, 409, 451):  # missing, empty repo, legal block
                if cp:
                    cp.write_text("null")
                return None
            if status in (403, 429):
                if "retry-after" in h:
                    delay = float(h["retry-after"])
                elif h.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in h:
                    delay = max(1.0, float(h["x-ratelimit-reset"]) - time.time() + 1)
                else:
                    delay = 30.0 * (attempt + 1)
                self._log(f"  rate limited, waiting {int(delay)}s")
                self._sleep(delay)
                continue
            if status == 401:
                raise GitHubError("GitHub rejected the token (401). Run `gh auth login` or set GITHUB_TOKEN.")
            if status == 422:  # search query GitHub refuses
                self._log(f"  skipped query GitHub refused: {url}")
                return None
            if status >= 500:
                self._sleep(5.0 * (attempt + 1))
                continue
            raise GitHubError(f"GitHub {status} for {url}: {body[:200]}")
        raise GitHubError(f"Gave up after retries: {url}")

    # -- helpers -------------------------------------------------------------
    def search_code(self, query: str, max_pages: int = 10) -> Iterator[dict]:
        """Yield code-search items. GitHub caps results at 1,000 per query."""
        for page in range(1, max_pages + 1):
            data = self.get("/search/code", {"q": query, "per_page": 100, "page": page}, search=True)
            if not data:
                return
            items = data.get("items", [])
            yield from items
            if len(items) < 100 or page * 100 >= min(data.get("total_count", 0), 1000):
                return

    def paginate(self, path: str, params: dict | None = None, max_pages: int = 3) -> list:
        out: list = []
        params = dict(params or {}, per_page=100)
        for page in range(1, max_pages + 1):
            data = self.get(path, dict(params, page=page))
            if not data:
                break
            out.extend(data)
            if len(data) < 100:
                break
        return out
