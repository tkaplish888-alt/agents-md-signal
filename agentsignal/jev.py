"""Client for Jev, TypeSafe's non-generative classifier model.

Jev doesn't write text. Given a "state" (here: one edit to an AGENTS.md file)
and a set of questions, it returns calibrated answers: a choice, a score, or a
probability ("noul"). That makes it cheap and fast for labeling thousands of
small things, and immune to the file talking it into writing anything else.
Docs: https://docs.typesafe.ai/introduction/quickstart
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable

from .github import open_url

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = os.environ.get("JEV_MODEL", "jev-latest")

Post = Callable[[str, dict, bytes], tuple[int, str]]


def _urllib_post(url: str, headers: dict, body: bytes) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with open_url(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class JevError(RuntimeError):
    pass


class Jev:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, post: Post | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key
        self.model = model
        self._post = post or _urllib_post
        self._sleep = sleep
        self.calls = 0
        self.input_tokens = 0

    def evaluate(self, state: str | dict, questions: dict) -> dict:
        """Returns the `answers` object keyed by question name."""
        body = json.dumps({"state": state, "model": self.model, "questions": questions}).encode()
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(4):
            status, text = self._post(API_URL, headers, body)
            self.calls += 1
            if status == 200:
                data = json.loads(text)
                self.input_tokens += (data.get("usage") or {}).get("input_tokens", 0)
                return data.get("answers", {})
            if status in (429, 529) or status >= 500:
                self._sleep(2 ** attempt)
                continue
            raise JevError(f"Jev {status}: {text[:200]}")
        raise JevError("Jev unavailable after retries")
