"""Minimal OpenAI-compatible chat client (any provider configured via AI_BASE_URL / AI_API_KEY / AI_MODEL)."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("vrbot.ai")


class AIUnavailable(Exception):
    pass


class ChatClient:
    def __init__(self, base_url: str, api_key: str | None, models: list[str], timeout: float = 90):
        self.base_url = base_url.rstrip("/")
        self.models = models
        self.http = httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        self.last_model: str | None = None
        self.last_error: str | None = None

    async def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        errors = []
        # sticky: the model that answered last goes first (keeps one conversation on one model)
        order = ([self.last_model] if self.last_model in self.models else []) + [m for m in self.models if m != self.last_model]
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(4)  # free-tier 429s are usually momentary
            for model in order:
                try:
                    return await self._one(model, messages, tools)
                except (httpx.HTTPError, AIUnavailable, KeyError, ValueError) as e:
                    errors.append(str(e)[:200])
                    log.warning("AI model %s failed: %s", model, e)
        self.last_error = " | ".join(errors[-3:])
        raise AIUnavailable(self.last_error)

    async def _one(self, model: str, messages: list[dict], tools: list[dict] | None) -> dict:
        body = {"model": model, "messages": messages, "temperature": 0.2}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        r = await self.http.post(f"{self.base_url}/chat/completions", json=body)
        if r.status_code >= 400:
            raise AIUnavailable(f"{model}: HTTP {r.status_code} {r.text[:160]}")
        msg = r.json()["choices"][0]["message"]
        self.last_model, self.last_error = model, None
        return msg

    async def health(self) -> bool:
        try:
            r = await self.http.get(f"{self.base_url}/models", timeout=10)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self):
        await self.http.aclose()
