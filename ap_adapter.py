"""AgenticPay model adapter backed by the project's cached Anthropic client.

AgenticPay agents call model.generate(prompt, temperature=0.0, max_tokens=2048)
with the whole prompt in one user message and no system prompt. Temperature is
ignored here: the API no longer accepts it. The adapter keeps the last raw
response so the driver can harvest tags the agent's own parser throws away.
"""
from __future__ import annotations

from agenticpay.models.base_llm import BaseLLM

from llm import Client


class AnthropicLLM(BaseLLM):
    def __init__(self, client: Client):
        self.client = client
        self.tag = ""          # set by the driver before each call, for the cache key
        self.last_raw = ""
        self.log: list[dict] = []   # every prompt and raw response, for transcripts

    def generate(self, prompt: str, temperature: float = 0.0,
                 max_tokens: int | None = None, **kwargs) -> str:
        raw = self.client.complete("", [{"role": "user", "content": prompt}], tag=self.tag)
        self.last_raw = raw
        self.log.append({"tag": self.tag, "prompt": prompt, "raw": raw})
        return raw

    def __repr__(self) -> str:
        return f"AnthropicLLM({self.client.model}, effort={self.client.effort})"
