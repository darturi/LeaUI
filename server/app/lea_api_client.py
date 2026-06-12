from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .config import LeaConfig


class LeaApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LeaApiResponse:
    status: int
    body: Any


Transport = Callable[[Request, float | None], Any]


class LeaApiClient:
    def __init__(self, config: LeaConfig, transport: Transport | None = None):
        self.config = config
        self.transport = transport or (lambda request, timeout=None: urlopen(request, timeout=timeout))

    def start_run(
        self,
        task: str,
        project: dict[str, Any] | None = None,
        resume: bool | str = False,
    ) -> dict[str, Any]:
        payload = {
            "task": task,
            "config": self._run_config(),
        }
        if project is not None:
            payload["project"] = project
        if resume:
            payload["resume"] = resume
        response = self._json_request("/v1/runs", method="POST", body=payload)
        if not isinstance(response.body, dict) or not response.body.get("run_id"):
            raise LeaApiError("Lea API did not return a run_id.")
        return response.body

    def get_run(self, api_run_id: str) -> dict[str, Any]:
        response = self._json_request(f"/v1/runs/{api_run_id}")
        if not isinstance(response.body, dict):
            raise LeaApiError("Lea API returned a non-object run status.")
        return response.body

    def get_transcript(self, api_run_id: str, transcript_url: str | None = None) -> Any:
        path = transcript_url or f"/v1/runs/{api_run_id}/transcript"
        if path.startswith("http://") or path.startswith("https://"):
            parsed = urlparse(path)
            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"
        response = self._json_request(path)
        return response.body

    def cancel_run(self, api_run_id: str) -> dict[str, Any] | None:
        response = self._json_request(f"/v1/runs/{api_run_id}/cancel", method="POST")
        return response.body if isinstance(response.body, dict) else None

    def resolve_approval(
        self,
        api_run_id: str,
        approval_id: str,
        decision: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": decision}
        if feedback is not None:
            body["feedback"] = feedback
        response = self._json_request(
            f"/v1/runs/{api_run_id}/approvals/{approval_id}",
            method="POST",
            body=body,
        )
        if not isinstance(response.body, dict):
            raise LeaApiError("Lea API returned a non-object approval response.")
        return response.body

    def stream_events(
        self,
        api_run_id: str,
        from_seq: int = 0,
        timeout: float | None = None,
    ) -> Iterable[dict[str, Any]]:
        query = urlencode({"from_seq": from_seq})
        headers = {"Last-Event-ID": str(from_seq - 1)} if from_seq > 0 else None
        request = self._request(f"/v1/runs/{api_run_id}/events?{query}", headers=headers)
        try:
            with self.transport(request, timeout) as response:
                yield from parse_sse_lines(response)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LeaApiError(f"Lea API stream returned HTTP {exc.code}: {detail}", status=exc.code) from exc

    def _json_request(self, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> LeaApiResponse:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if body is not None else None
        request = self._request(path, method=method, body=data, headers=headers)
        try:
            with self.transport(request, 30) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else None
                return LeaApiResponse(status=getattr(response, "status", 200), body=payload)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LeaApiError(f"Lea API returned HTTP {exc.code}: {detail}", status=exc.code) from exc

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Request:
        url = f"{self.config.lea_api_base_url}{path}"
        request_headers = dict(headers or {})
        if self.config.lea_api_key:
            request_headers["Authorization"] = f"Bearer {self.config.lea_api_key}"
        return Request(url, data=body, headers=request_headers, method=method)

    def _run_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "agent": {
                "prompt_variant": "interactive",
                "max_turns": self.config.max_turns,
                "narrate_tool_steps": self.config.narrate_tool_steps,
                "permission_tier": self.config.permission_tier,
                "theorem_translation_max_retries": self.config.theorem_translation_max_retries,
            },
        }
        if self.config.model:
            config["model"] = {"name": self.config.model}
            model_kwargs = self._provider_model_kwargs()
            if model_kwargs:
                config["model"]["model_kwargs"] = model_kwargs
        if self.config.max_turns is None:
            config["agent"] = {
                "prompt_variant": "interactive",
                "narrate_tool_steps": self.config.narrate_tool_steps,
                "permission_tier": self.config.permission_tier,
                "theorem_translation_max_retries": self.config.theorem_translation_max_retries,
            }
        return config

    def _provider_model_kwargs(self) -> dict[str, str]:
        family = _model_family(self.config.model)
        if family == "anthropic" and self.config.anthropic_api_key:
            return {"api_key": self.config.anthropic_api_key}
        if family == "openai" and self.config.openai_api_key:
            return {"api_key": self.config.openai_api_key}
        if family == "google" and self.config.google_api_key:
            return {"api_key": self.config.google_api_key}
        return {}


def _model_family(model: str | None) -> str | None:
    normalized = str(model or "").lower()
    if normalized.startswith(("claude-", "anthropic/")):
        return "anthropic"
    if normalized.startswith(("gpt-", "o1", "o3", "o4", "openai/")):
        return "openai"
    if normalized.startswith(("gemini", "google/")):
        return "google"
    return None


def parse_sse_lines(response: Any) -> Iterable[dict[str, Any]]:
    event_type: str | None = None
    data_lines: list[str] = []

    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.rstrip("\r\n")
        if line == "":
            frame = _parse_sse_frame(event_type, data_lines)
            event_type = None
            data_lines = []
            if frame is not None:
                yield frame
            continue
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())

    frame = _parse_sse_frame(event_type, data_lines)
    if frame is not None:
        yield frame


def _parse_sse_frame(event_type: str | None, data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        payload = {"raw": data}
    if event_type and isinstance(payload, dict) and "type" not in payload:
        payload = {**payload, "type": event_type}
    return payload if isinstance(payload, dict) else {"type": event_type or "message", "data": payload}
