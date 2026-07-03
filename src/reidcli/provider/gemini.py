"""Gemini Developer API provider.

Speaks the REST `generateContent` endpoint with stdlib urllib, matching the
same no-extra-dependencies provider style as the other clients.

Env vars consumed:
  GEMINI_API_KEY   - auth key
  GEMINI_BASE_URL  - API base (defaults to https://generativelanguage.googleapis.com)
  GEMINI_MODEL     - default model name
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from reidcli.diagnostics.logger import get_logger
from reidcli.provider.base import BaseProvider, Message, ProviderResponse, ToolCall, Usage

log = get_logger("reidcli.provider.gemini")

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-3-flash"
TIMEOUT_SECONDS = 120


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(self, api_key: str, base_url: str = "", default_model: str = "") -> None:
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.default_model = default_model or DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> GeminiProvider | None:
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            return None
        base = os.environ.get("GEMINI_BASE_URL", DEFAULT_BASE_URL).strip()
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip()
        return cls(api_key=key, base_url=base, default_model=model)

    def _to_gemini_contents(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        system: str | None = None
        contents: list[dict] = []
        pending_tool_results: list[dict] = []
        tool_names_by_id: dict[str, str] = {}

        for m in messages:
            if m.role == "system":
                system = m.content
                continue
            if m.role == "tool":
                name = tool_names_by_id.get(m.tool_call_id or "", m.tool_call_id or "tool_result")
                pending_tool_results.append({
                    "functionResponse": {
                        "name": name,
                        "response": {"output": m.content},
                    }
                })
                continue
            if pending_tool_results:
                contents.append({"role": "user", "parts": pending_tool_results})
                pending_tool_results = []

            if m.role == "assistant":
                parts: list[dict] = []
                if m.content:
                    parts.append({"text": m.content})
                for tc in m.tool_calls:
                    tool_names_by_id[tc.id] = tc.name
                    parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            contents.append({"role": "user", "parts": [{"text": m.content}]})

        if pending_tool_results:
            contents.append({"role": "user", "parts": pending_tool_results})
        return system, contents

    def _to_gemini_tools(self, tools: list[dict[str, Any]] | None) -> list[dict]:
        if not tools:
            return []
        declarations: list[dict] = []
        for tool in tools:
            fn = tool.get("function", tool)
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return [{"functionDeclarations": declarations}] if declarations else []

    def _parse(self, body: dict) -> ProviderResponse:
        candidates = body.get("candidates") or [{}]
        content = (candidates[0] or {}).get("content", {})
        parts = content.get("parts") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for i, part in enumerate(parts):
            if "text" in part:
                text_parts.append(part.get("text") or "")
            call = part.get("functionCall")
            if call:
                tool_calls.append(ToolCall(
                    id=f"gemini-{i}",
                    name=call.get("name", ""),
                    arguments=call.get("args") or {},
                ))
        usage_raw = body.get("usageMetadata", {})
        return ProviderResponse(
            text="\n".join(p for p in text_parts if p),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=usage_raw.get("promptTokenCount", 0),
                completion_tokens=usage_raw.get("candidatesTokenCount", 0),
            ),
            stop_reason=(candidates[0] or {}).get("finishReason", "stop"),
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model
        system, contents = self._to_gemini_contents(messages)
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        gemini_tools = self._to_gemini_tools(tools)
        if gemini_tools:
            payload["tools"] = gemini_tools

        query = urllib.parse.urlencode({"key": self.api_key})
        url = f"{self.base_url}/v1beta/models/{model}:generateContent?{query}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
            log.error("Gemini API error %s: %s", exc.code, err_body)
            raise RuntimeError(f"Gemini API error {exc.code}: {err_body}") from exc
        except urllib.error.URLError as exc:
            log.error("Gemini connection error: %s", exc)
            raise RuntimeError(f"connection error: {exc}") from exc

        return self._parse(json.loads(raw))
