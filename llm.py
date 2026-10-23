"""Model adapter with aggressive disk caching.

The cache is what makes a 62-run grid affordable and makes reruns free. Keyed
on (model, effort, messages, tag), so changing a prompt invalidates only the
cells whose prompt changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
ENV_FILE = Path(__file__).parent / ".env"


def _env(name: str) -> str | None:
    """Shell variable wins; otherwise read NAME=... from a gitignored .env
    beside this file. Never logged, never cached."""
    val = os.environ.get(name)
    if val:
        return val
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _api_key() -> str:
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "No API key: export ANTHROPIC_API_KEY or put ANTHROPIC_API_KEY=... in .env"
        )
    return key


def _extra_headers() -> dict[str, str]:
    """Keys not scoped to a workspace must name one on every request."""
    ws = _env("ANTHROPIC_WORKSPACE_ID")
    return {"anthropic-workspace-id": ws} if ws else {}


REFUSAL_MARKER = "__REFUSAL__"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_TIMEOUT_S = 180
OPENROUTER_RETRIES = 5
OPENROUTER_BACKOFF_S = 3.0   # 3, 6, 12, 24 s between attempts


def _key(model: str, effort: str, messages: list[dict], tag: str) -> str:
    blob = json.dumps(
        {"m": model, "e": effort, "msgs": messages, "tag": tag},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class Client:
    """Thin wrapper. Swap _call for another provider without touching callers.

    Refusals are returned as REFUSAL_MARKER and counted, never retried on a
    different model: a model swap mid-grid would break the one-family design,
    and a refusal is itself data for the framing cell."""

    def __init__(self, model: str, effort: str = "low", max_tokens: int = 4000,
                 use_cache: bool = True):
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.use_cache = use_cache
        self.calls = 0
        self.cache_hits = 0
        self.refusals = 0
        self._sdk = None

    def complete(self, system: str, messages: list[dict], tag: str = "") -> str:
        payload = [{"role": "system", "content": system}] + messages
        k = _key(self.model, self.effort, payload, tag)
        path = CACHE_DIR / f"{k}.json"

        if self.use_cache and path.exists():
            self.cache_hits += 1
            rec = json.loads(path.read_text())
            if rec.get("stop_reason") == "refusal":
                self.refusals += 1
            return rec["text"]

        text, stop_reason = self._call(system, messages)
        self.calls += 1
        if stop_reason == "refusal":
            self.refusals += 1
        path.write_text(json.dumps({
            "text": text, "stop_reason": stop_reason, "model": self.model,
            "effort": self.effort, "tag": tag, "ts": time.time(),
        }))
        return text

    def _client(self):
        if self._sdk is None:
            import anthropic  # imported lazily so the module loads without the SDK
            self._sdk = anthropic.Anthropic(
                api_key=_api_key(), default_headers=_extra_headers(), max_retries=4,
            )
        return self._sdk

    def _call(self, system: str, messages: list[dict]) -> tuple[str, str]:
        if self.model.startswith("openrouter/"):
            return self._call_openrouter(system, messages)
        resp = self._client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=messages,
            output_config={"effort": self.effort},
        )
        if resp.stop_reason == "refusal":
            return REFUSAL_MARKER, "refusal"
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, resp.stop_reason or ""

    def _call_openrouter(self, system: str, messages: list[dict]) -> tuple[str, str]:
        """Non-Claude models for the pilot responsiveness check only. Model id
        is everything after 'openrouter/'. Effort is not sent. Hard deadline
        on the request: the wargame harness hung on idle sockets here."""
        import urllib.request

        key = _env("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY missing from shell and .env")
        body = json.dumps({
            "model": self.model.removeprefix("openrouter/"),
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "max_tokens": self.max_tokens,
        }).encode()
        import urllib.error

        last: Exception | None = None
        for attempt in range(OPENROUTER_RETRIES):
            req = urllib.request.Request(
                OPENROUTER_URL, data=body, method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=OPENROUTER_TIMEOUT_S) as r:
                    data = json.loads(r.read())
                choice = data["choices"][0]
                return choice["message"]["content"] or "", choice.get("finish_reason") or ""
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429:
                    raise                      # client error: do not retry
                last = e
            except (urllib.error.URLError, TimeoutError, OSError, KeyError) as e:
                last = e
            time.sleep(OPENROUTER_BACKOFF_S * (2 ** attempt))
        raise RuntimeError(f"OpenRouter failed after {OPENROUTER_RETRIES} attempts: {last}")


def parse_turn(raw: str) -> dict:
    """Agents are asked for strict JSON. Recover from fenced blocks and stray
    prose rather than losing an episode to a formatting slip."""
    s = raw.strip()
    if s == REFUSAL_MARKER:
        return {"bloc_message": "", "cross_message": "", "price": None, "accept": None,
                "reasoning": "", "parse_error": "refusal"}
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        return {"bloc_message": "", "cross_message": "", "price": None, "accept": None,
                "reasoning": "", "parse_error": raw[:200]}
    try:
        d = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return {"bloc_message": "", "cross_message": "", "price": None, "accept": None,
                "reasoning": "", "parse_error": raw[:200]}
    d.setdefault("bloc_message", "")
    d.setdefault("cross_message", "")
    d.setdefault("reasoning", "")
    acc = d.get("accept")
    d["accept"] = acc.strip().upper() if isinstance(acc, str) and acc.strip() else None
    try:
        d["price"] = float(d.get("price")) if d.get("price") is not None else None
    except (TypeError, ValueError):
        d["price"] = None
    return d
