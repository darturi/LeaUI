from __future__ import annotations

import json
import inspect
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Any

from . import safe_verify as safe_verify_service
from . import settings as settings_service
from . import store
from .config import ROOT, LeaConfig
from .lea_api_client import LeaApiClient, LeaApiError
from .project_usage import detect_used_project_formalizations


active_run_lock = Lock()
RAW_EVENT_LOG_DIR = ROOT / "data" / "lea-api-events"
WRITE_TOOL_NAMES = {"write_file", "edit_file"}
PATH_KEYS = ("path", "file", "file_path", "relative_path", "filename", "name")
CODE_KEYS = ("code", "content", "source", "text")
APPROVAL_KEYS = (
    "type",
    "approval_id",
    "tier",
    "candidate",
    "lean_code",
    "theorem_name",
    "check_result",
    "decision",
    "feedback",
    "seq",
    "schema_version",
)


@dataclass
class RunnerContext:
    session_id: str
    run_id: str
    task: str
    config: LeaConfig
    events: Queue[dict[str, Any]]
    client: LeaApiClient | None = None
    project: dict[str, Any] | None = None


@dataclass
class ToolTracker:
    last_write_or_edit_path: str | None = None
    pending_write_path: str | None = None
    pending_write_had_content: bool = False
    pending_edit_path: str | None = None
    pending_lean_check_path: str | None = None
    last_lean_check_path: str | None = None
    last_lean_check_succeeded: bool | None = None
    unchecked_write_after_lean_check: bool = False
    drift_warnings: set[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.drift_warnings is None:
            self.drift_warnings = set()


@dataclass
class UsageBreakdownCollector:
    current_turn: int | None = None
    rows: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = []

    def notice_turn(self, frame: dict[str, Any]) -> None:
        self.current_turn = _first_int(frame, "turn")

    def notice_approval(self, frame: dict[str, Any]) -> None:
        candidate = _first_int(frame, "candidate")
        preflight = self._last_unlabeled_preflight()
        if preflight is not None and candidate is not None:
            preflight["candidate"] = candidate
            preflight["label"] = f"Theorem translation preflight candidate {candidate}"

    def add_usage(self, input_tokens: int | None, output_tokens: int | None, cost_usd: float | None) -> None:
        if not input_tokens and not output_tokens and not cost_usd:
            return
        if self.current_turn is None:
            row = self._last_unlabeled_preflight()
            if row is None:
                row = self._new_row("theorem_translation", "Theorem translation preflight", None, None)
                self.rows.append(row)
        else:
            row = next(
                (
                    item for item in self.rows
                    if item.get("phase") == "proof_turn" and item.get("turn") == self.current_turn
                ),
                None,
            )
            if row is None:
                row = self._new_row("proof_turn", f"Turn {self.current_turn}", self.current_turn, None)
                self.rows.append(row)
        row["input_tokens"] += int(input_tokens or 0)
        row["output_tokens"] += int(output_tokens or 0)
        row["cost_usd"] += float(cost_usd or 0)
        row["event_count"] += 1

    def final_rows(
        self,
        input_total: int | None,
        output_total: int | None,
        cost_total: float | None,
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.rows]
        input_seen = sum(int(row.get("input_tokens") or 0) for row in rows)
        output_seen = sum(int(row.get("output_tokens") or 0) for row in rows)
        cost_seen = sum(float(row.get("cost_usd") or 0) for row in rows)
        input_delta = max(0, int(input_total or 0) - input_seen)
        output_delta = max(0, int(output_total or 0) - output_seen)
        cost_delta = max(0.0, float(cost_total or 0) - cost_seen)
        if input_delta or output_delta or cost_delta >= 0.000000001:
            row = self._new_row("unattributed", "Unattributed usage", None, None)
            row["input_tokens"] = input_delta
            row["output_tokens"] = output_delta
            row["cost_usd"] = cost_delta
            rows.append(row)
        for ordinal, row in enumerate(rows, start=1):
            row["ordinal"] = ordinal
        return rows

    def _last_unlabeled_preflight(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        row = self.rows[-1]
        if row.get("phase") == "theorem_translation" and row.get("candidate") is None:
            return row
        return None

    @staticmethod
    def _new_row(phase: str, label: str, turn: int | None, candidate: int | None) -> dict[str, Any]:
        return {
            "phase": phase,
            "label": label,
            "turn": turn,
            "candidate": candidate,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "event_count": 0,
        }


def emit(events: Queue[dict[str, Any]], event_type: str, payload: dict[str, Any]) -> None:
    events.put({"type": event_type, "payload": payload})


def log_status(context: RunnerContext, message: str, **payload: Any) -> None:
    status = payload.get("status")
    step_number = payload.get("step_number")
    event = store.add_status_event(
        context.session_id,
        context.run_id,
        message,
        status=str(status) if status is not None else None,
        step_number=int(step_number) if isinstance(step_number, int) else None,
    )
    data = {**event, **payload, "message": message}
    print(f"[lea-run:{context.run_id}] {message}", flush=True)
    emit(context.events, "status", data)


def _log_api_frame(context: RunnerContext, api_run_id: str, frame: dict[str, Any]) -> None:
    RAW_EVENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RAW_EVENT_LOG_DIR / f"{context.run_id}.jsonl"
    preview = _truncate_for_log(frame)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "api_run_id": api_run_id,
                    "local_run_id": context.run_id,
                    "seq": _seq(frame),
                    "type": _event_type(frame),
                    "payload": preview,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _truncate_for_log(value: Any, limit: int = 2000) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _relative_path(path: str, lea_root: Path | None) -> str:
    candidate = Path(path).expanduser()
    if lea_root is None:
        return str(candidate)

    lea_root = lea_root.resolve()
    if not candidate.is_absolute():
        candidate = (lea_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        return str(candidate.relative_to(lea_root))
    except ValueError:
        return str(candidate)


def _resolve_lea_path(path: str, lea_root: Path | None) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_absolute() or lea_root is None:
        return candidate.resolve()
    return (lea_root / candidate).resolve()


def _is_proposal_path(path: str | Path) -> bool:
    return ".lea_proposals" in Path(path).parts


def _emit_file_snapshot(
    *,
    context: RunnerContext,
    path: Path,
    emitted: set[tuple[str, int, int]],
) -> dict[str, Any] | None:
    if _is_proposal_path(path):
        return None
    if not path.exists() or path.suffix != ".lean":
        return None
    stat = path.stat()
    emitted_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if emitted_key in emitted:
        return None
    emitted.add(emitted_key)
    code = path.read_text()
    relative_path = _relative_path(str(path), context.config.lea_root)
    step = store.add_code_step(
        context.session_id,
        context.run_id,
        relative_path,
        code,
        kind="code",
        turn=getattr(context, "current_turn", None),
        used_project_formalizations=_used_project_formalizations(
            context,
            relative_path,
            code,
        ),
    )
    emit(context.events, "code_step", step)
    log_status(
        context,
        f"Captured Lean file update: {step['path']}",
        status="code_step",
        step_number=step["step_number"],
    )
    return step


def _emit_no_code_step(
    *,
    context: RunnerContext,
    turn: int,
    had_tool_call: bool,
    latest_path: str | None,
    latest_code: str,
) -> dict[str, Any]:
    summary = (
        f"Turn {turn}: no Lean file changes after tool calls."
        if had_tool_call
        else f"Turn {turn}: no tool calls and no Lean file changes."
    )
    step = store.add_code_step(
        context.session_id,
        context.run_id,
        latest_path or "No Lean file yet",
        latest_code,
        kind="no_code",
        summary=summary,
        turn=turn,
        used_project_formalizations=_used_project_formalizations(context, latest_path, latest_code),
    )
    emit(context.events, "code_step", step)
    log_status(context, summary, status="no_code_step", turn=turn, step_number=step["step_number"])
    return step


def _emit_chat_message(
    context: RunnerContext, role: str, content: str, kind: str = "assistant"
) -> dict[str, Any]:
    message = store.add_message(context.session_id, role, content, context.run_id, kind=kind)
    emit(context.events, "message", message)
    return message


def _flush_assistant_turn(
    context: RunnerContext,
    chunks: list[str],
    persisted_texts: list[str],
    kind: str = "assistant",
) -> dict[str, Any] | None:
    text = "".join(chunks).strip()
    chunks.clear()
    if not text:
        return None
    if persisted_texts and persisted_texts[-1] == text:
        return None
    persisted_texts.append(text)
    return _emit_chat_message(context, "assistant", text, kind=kind)


def _event_type(frame: dict[str, Any]) -> str:
    for candidate in _walk_dicts(frame):
        value = candidate.get("type") or candidate.get("event") or candidate.get("kind")
        if isinstance(value, str) and value:
            return value.strip().lower()
    return ""


def _text_delta(frame: dict[str, Any]) -> str | None:
    kind = _event_type(frame)
    if kind in {"assistant_delta", "assistant_text_delta", "text_delta", "delta", "token", "assistant_text"}:
        return _first_string(frame, "text", "delta", "content", "message")
    if kind in {
        "status",
        "tool_call",
        "tool_called",
        "tool_result",
        "tool_resulted",
        "turn_started",
        "usage_updated",
        "project_entry_updated",
        "finished",
        "done",
        "completed",
        "error",
    }:
        return None
    return _first_string(frame, "text_delta", "assistant_delta")


def _status_message(frame: dict[str, Any]) -> str | None:
    kind = _event_type(frame)
    if kind == "project_entry_updated":
        project_id = _first_string(frame, "project_id") or "project"
        theorem_name = _first_string(frame, "theorem_name") or "theorem"
        action = _first_string(frame, "entry_action") or "updated"
        return f"Project {project_id} {action} entry for {theorem_name}."
    if kind in {
        "status",
        "progress",
        "tool_call",
        "tool_called",
        "tool_result",
        "tool_resulted",
        "turn_started",
        "usage_updated",
        "project_entry_updated",
        "started",
    }:
        return _first_string(frame, "message", "status", "name", "tool", "text")
    return None


def _terminal_status(frame: dict[str, Any]) -> str | None:
    kind = _event_type(frame)
    if kind in {"finished", "done", "completed"}:
        reason = str(frame.get("reason") or frame.get("status") or "").lower()
        if reason in {"max_turns", "max-turns"}:
            return "max_turns"
        if reason in {"failed", "error", "cancelled", "canceled", "theorem_translation_failed"}:
            return "failed"
        return "success"
    if kind in {"failed", "cancelled", "canceled"}:
        return "failed"
    if kind == "error":
        return "failed"
    return None


def _final_text(frame: dict[str, Any]) -> str | None:
    direct = _first_string_from(frame, "final_text", "summary", "message", "text")
    if direct:
        return direct
    for candidate in _walk_dicts(frame):
        value = _first_string_from(candidate, "final_text", "summary", "message", "text")
        if value:
            return value
    return None


def _display_terminal_text(status: str | None, final_text: str | None) -> str | None:
    if not final_text:
        return final_text
    if status == "failed" and "theorem translation failed" in final_text.lower():
        attempts = re.search(r"after\s+(\d+)\s+attempts", final_text, re.IGNORECASE)
        attempt_text = f" after {attempts.group(1)} attempts" if attempts else ""
        return (
            f"The theorem translation preflight failed{attempt_text}. "
            "Lea could not produce a Lean statement that typechecked. You can retry this request."
        )
    return final_text


def _first_string(frame: dict[str, Any], *keys: str) -> str | None:
    for candidate in _walk_dicts(frame):
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _first_int(frame: dict[str, Any], key: str) -> int | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get(key)
        if isinstance(value, int):
            return value
    return None


def _usage(frame: dict[str, Any]) -> tuple[int | None, int | None]:
    for candidate in _walk_dicts(frame):
        usage = candidate.get("usage") if isinstance(candidate.get("usage"), dict) else candidate
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens")
        if isinstance(input_tokens, int | float) or isinstance(output_tokens, int | float):
            return (
                int(input_tokens) if isinstance(input_tokens, int | float) else None,
                int(output_tokens) if isinstance(output_tokens, int | float) else None,
            )
    return None, None


def _cost(frame: dict[str, Any]) -> float | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get("cost")
        if isinstance(value, int | float):
            return float(value)
        value = candidate.get("cost_usd")
        if isinstance(value, int | float):
            return float(value)
    return None


def _merge_usage_total(current: int | None, incoming: int | None) -> int | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    return max(current, incoming)


def _merge_cost_total(current: float | None, incoming: float | None) -> float | None:
    if incoming is None:
        return current
    if current is None:
        return incoming
    return max(current, incoming)


def _seq(frame: dict[str, Any]) -> int | None:
    for candidate in _walk_dicts(frame):
        value = candidate.get("seq")
        if isinstance(value, int):
            return int(value)
    return None


def _walk_dicts(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            found.append(item)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return found


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _tool_name(candidate: dict[str, Any]) -> str:
    function = candidate.get("function") if isinstance(candidate.get("function"), dict) else {}
    value = (
        candidate.get("tool_name")
        or candidate.get("tool")
        or candidate.get("name")
        or candidate.get("function_name")
        or function.get("name")
    )
    return str(value or "").strip()


def _tool_args(candidate: dict[str, Any]) -> dict[str, Any]:
    function = candidate.get("function") if isinstance(candidate.get("function"), dict) else {}
    for key in ("args", "arguments", "input", "parameters", "params"):
        parsed = _as_dict(candidate.get(key))
        if parsed:
            return parsed
    parsed = _as_dict(function.get("arguments"))
    return parsed


def _primary_event_dict(frame: dict[str, Any]) -> dict[str, Any]:
    return frame.get("payload") if isinstance(frame.get("payload"), dict) else frame


def _approval_payload(frame: dict[str, Any], frame_type: str) -> dict[str, Any]:
    event = _primary_event_dict(frame)
    payload = {key: event[key] for key in APPROVAL_KEYS if key in event}
    payload["type"] = frame_type
    return payload


def _code_payloads(frame: dict[str, Any]) -> list[tuple[str, str, int | None]]:
    payloads: list[tuple[str, str, int | None]] = []

    for candidate in _walk_dicts(frame):
        kind = str(candidate.get("type") or candidate.get("event") or candidate.get("kind") or "").strip().lower()
        tool_name = _tool_name(candidate)
        args = _tool_args(candidate)
        path = _first_string_from(args, *PATH_KEYS) or _first_string_from(candidate, *PATH_KEYS)
        code = _first_string_from(args, *CODE_KEYS) or _first_string_from(candidate, *CODE_KEYS)
        turn = candidate.get("turn") or args.get("turn")
        is_tool_result = kind in {"tool_result", "tool_resulted"}
        is_tool_call = kind in {"tool_call", "tool_called"}
        is_file_event = kind in {
            "code_step",
            "file_snapshot",
            "file_written",
            "file_created",
            "file_updated",
            "artifact",
            "artifact_created",
        }

        if is_tool_result:
            continue

        is_write_tool_call = kind == "write_file" or (is_tool_call and tool_name == "write_file")
        is_implicit_artifact = (
            not kind
            and path
            and code
            and _looks_like_lean_path(path)
            and _looks_like_lean_code(code)
        )

        if is_write_tool_call or is_file_event or is_implicit_artifact:
            if not path and not code:
                continue
            if path and not _looks_like_lean_path(path):
                continue
            if is_write_tool_call and not code:
                continue
            payloads.append((path or "Lea.lean", code or "", int(turn) if isinstance(turn, int) else None))

    return _dedupe_code_payloads(payloads)


def _first_string_from(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _looks_like_lean_path(path: str) -> bool:
    return str(path).strip().endswith(".lean")


def _looks_like_lean_code(code: str) -> bool:
    stripped = code.lstrip()
    if stripped.startswith(("import ", "theorem ", "example ", "lemma ", "def ", "/--", "--")):
        return True
    return "\n" in code and any(token in code for token in (" := by", " by\n", "theorem ", "example "))


def _dedupe_code_payloads(payloads: list[tuple[str, str, int | None]]) -> list[tuple[str, str, int | None]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[tuple[str, str, int | None]] = []
    for path, code, turn in payloads:
        key = (path, code)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((path, code, turn))
    return deduped


def _emit_code_payload(
    *,
    context: RunnerContext,
    frame: dict[str, Any],
    emitted: set[tuple[str, int, int]],
    emitted_payloads: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    payloads = _code_payloads(frame)
    steps: list[dict[str, Any]] = []

    for path, code, turn in payloads:
        if path and _is_proposal_path(path):
            continue
        if not code and path:
            file_path = _resolve_lea_path(path, context.config.lea_root)
            step = _emit_file_snapshot(context=context, path=file_path, emitted=emitted)
            if step:
                if emitted_payloads is not None:
                    emitted_payloads.add((step["path"], step["code"]))
                steps.append(step)
            continue

        relative_path = _relative_path(path, context.config.lea_root)
        payload_key = (relative_path, code)
        if emitted_payloads is not None:
            if payload_key in emitted_payloads:
                continue
            emitted_payloads.add(payload_key)

        step = store.add_code_step(
            context.session_id,
            context.run_id,
            relative_path,
            code,
            kind="code",
            turn=turn,
            used_project_formalizations=_used_project_formalizations(context, relative_path, code),
        )
        emit(context.events, "code_step", step)
        log_status(
            context,
            f"Captured Lean file update: {step['path']}",
            status="code_step",
            step_number=step["step_number"],
        )
        steps.append(step)

    return steps


def _track_tool_frame(
    *,
    context: RunnerContext,
    api_run_id: str,
    frame: dict[str, Any],
    tracker: ToolTracker,
    emitted: set[tuple[str, int, int]],
    emitted_payloads: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    event = _primary_event_dict(frame)
    kind = _event_type(event)
    tool_name = _tool_name(event)
    args = _tool_args(event)
    path = _first_string_from(args, *PATH_KEYS) or _first_string_from(event, *PATH_KEYS)
    steps: list[dict[str, Any]] = []

    if kind in {"tool_call", "tool_called"}:
        if tool_name in WRITE_TOOL_NAMES and path:
            tracker.last_write_or_edit_path = path
            if tool_name == "write_file":
                tracker.pending_write_path = path
                tracker.pending_write_had_content = bool(_first_string_from(args, *CODE_KEYS))
            else:
                tracker.pending_edit_path = path
        elif tool_name == "lean_check" and path:
            tracker.pending_lean_check_path = path
            tracker.last_lean_check_path = path
            _emit_path_drift_if_needed(context, api_run_id, tracker, checked_path=path)
        return steps

    if kind not in {"tool_result", "tool_resulted"}:
        return steps

    content = _first_string_from(event, "content", "preview") or ""
    succeeded = _tool_result_succeeded(content)
    if tool_name == "write_file":
        if succeeded:
            tracker.unchecked_write_after_lean_check = True
        if succeeded and tracker.pending_write_path and not tracker.pending_write_had_content:
            file_path = _resolve_lea_path(tracker.pending_write_path, context.config.lea_root)
            step = _emit_file_snapshot(context=context, path=file_path, emitted=emitted)
            if step:
                emitted_payloads.add((step["path"], step["code"]))
                steps.append(step)
        tracker.pending_write_path = None
        tracker.pending_write_had_content = False
    elif tool_name == "edit_file":
        if succeeded:
            tracker.unchecked_write_after_lean_check = True
        if succeeded and tracker.pending_edit_path:
            file_path = _resolve_lea_path(tracker.pending_edit_path, context.config.lea_root)
            step = _emit_file_snapshot(context=context, path=file_path, emitted=emitted)
            if step:
                emitted_payloads.add((step["path"], step["code"]))
                steps.append(step)
        tracker.pending_edit_path = None
    elif tool_name == "lean_check":
        tracker.last_lean_check_succeeded = _lean_check_succeeded(content)
        tracker.unchecked_write_after_lean_check = False
        if tracker.pending_lean_check_path:
            file_path = _resolve_lea_path(tracker.pending_lean_check_path, context.config.lea_root)
            step = _emit_file_snapshot(context=context, path=file_path, emitted=emitted)
            if step:
                emitted_payloads.add((step["path"], step["code"]))
                steps.append(step)
        tracker.pending_lean_check_path = None

    return steps


def _tool_result_succeeded(content: str) -> bool:
    stripped = content.strip()
    return bool(stripped) and not stripped.lower().startswith("error:")


def _lean_check_succeeded(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith("error:"):
        return False
    if re.search(r"(^|\n).*?:\d+:\d+:\s+error:", stripped):
        return False
    if "declaration uses `sorry`" in lowered or "declaration uses 'sorry'" in lowered:
        return False
    return True


def _terminal_status_after_tool_checks(
    status: str | None,
    tracker: ToolTracker,
) -> tuple[str | None, str | None]:
    if status != "success":
        return status, None
    if tracker.last_lean_check_succeeded is False:
        detail = "Lea API reported completion, but the most recent lean_check failed."
        if tracker.last_lean_check_path:
            detail = f"{detail} Checked file: {tracker.last_lean_check_path}."
        return "failed", detail
    if tracker.unchecked_write_after_lean_check:
        detail = "Lea API reported completion, but it changed a Lean file after the most recent lean_check."
        if tracker.last_write_or_edit_path:
            detail = f"{detail} Unchecked file: {tracker.last_write_or_edit_path}."
        return "failed", detail
    if tracker.last_lean_check_succeeded is not True:
        return "failed", "Lea API reported completion, but no successful lean_check was observed."
    return status, None


def _emit_path_drift_if_needed(
    context: RunnerContext,
    api_run_id: str,
    tracker: ToolTracker,
    *,
    checked_path: str,
) -> None:
    written_path = tracker.last_write_or_edit_path
    if not written_path:
        return
    written_root = _proof_root_key(written_path)
    checked_root = _proof_root_key(checked_path)
    if not written_root or not checked_root or written_root == checked_root:
        return

    key = (written_root, checked_root)
    assert tracker.drift_warnings is not None
    if key in tracker.drift_warnings:
        return
    tracker.drift_warnings.add(key)

    message = f"Path drift detected: wrote {written_path} but checked {checked_path}."
    payload = {
        "type": "path_drift",
        "message": message,
        "written_path": written_path,
        "checked_path": checked_path,
    }
    _log_api_frame(context, api_run_id, payload)
    log_status(
        context,
        message,
        status="path_drift",
        written_path=written_path,
        checked_path=checked_path,
    )


def _proof_root_key(path: str) -> str | None:
    normalized = str(Path(path).expanduser())
    marker = "/workspace/proofs/"
    if marker in normalized:
        return normalized.split(marker, 1)[0]
    if normalized.endswith(".lean"):
        return str(Path(normalized).parent)
    return None


def _reconcile_terminal_artifacts(
    *,
    context: RunnerContext,
    run_status: dict[str, Any] | None,
    transcript: Any,
    final_text: str | None,
    emitted: set[tuple[str, int, int]],
    emitted_payloads: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if run_status:
        steps.extend(
            _emit_code_payload(
                context=context,
                frame=run_status,
                emitted=emitted,
                emitted_payloads=emitted_payloads,
            )
        )
    if transcript is not None:
        transcript_frame = {"type": "transcript", "transcript": transcript}
        steps.extend(
            _emit_code_payload(
                context=context,
                frame=transcript_frame,
                emitted=emitted,
                emitted_payloads=emitted_payloads,
            )
        )

    for filename in _lean_filenames_from_text(final_text or ""):
        file_path = _resolve_lea_path(filename, context.config.lea_root)
        if file_path.exists() and file_path.suffix == ".lean":
            payload_key = (_relative_path(str(file_path), context.config.lea_root), file_path.read_text())
            if payload_key in emitted_payloads:
                continue
        step = _emit_file_snapshot(context=context, path=file_path, emitted=emitted)
        if step:
            emitted_payloads.add((step["path"], step["code"]))
            steps.append(step)
    return steps


def _lean_filenames_from_text(text: str) -> list[str]:
    matches = re.findall(r"[`'\"]?([A-Za-z0-9_./ -]+\.lean)[`'\"]?", text)
    return [match.strip() for match in matches if match.strip()]


def _assistant_text_from_transcript(transcript: Any) -> str:
    for candidate in _walk_dicts(transcript):
        if candidate.get("role") != "assistant":
            continue
        content = candidate.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            chunks = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ]
            text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()
            if text:
                return text
    return ""


def _emit_terminal_no_code_step(context: RunnerContext, api_run_id: str | None) -> dict[str, Any]:
    detail = (
        "Lea API completed, but it did not expose a readable Lean file or code artifact."
        " Configure lea_root to the API workspace, or enable code/file events in the Lea API."
    )
    step = store.add_code_step(
        context.session_id,
        context.run_id,
        "No Lean artifact exposed",
        "",
        kind="no_code",
        summary=detail,
        turn=None,
    )
    emit(context.events, "code_step", step)
    log_status(
        context,
        detail,
        status="no_code_step",
        api_run_id=api_run_id,
        step_number=step["step_number"],
    )
    return step


def _used_project_formalizations(
    context: RunnerContext,
    proof_path: str | None,
    code: str,
) -> list[dict[str, Any]]:
    return detect_used_project_formalizations(
        project=context.project,
        config=context.config,
        code=code,
        proof_path=proof_path,
    )


def _api_run_status_to_local(status: str | None) -> str | None:
    normalized = str(status or "").lower()
    if normalized in {"completed", "success", "succeeded"}:
        return "success"
    if normalized in {"max_turns", "max-turns"}:
        return "max_turns"
    if normalized in {"max_spend", "max-spend", "spend_limit", "spend-limit"}:
        return "max_spend"
    if normalized in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    return None


def _api_run_terminal_status(run_status: dict[str, Any]) -> str | None:
    result = run_status.get("result")
    if isinstance(result, dict):
        reason = str(result.get("reason") or "").lower()
        if reason in {"theorem_translation_failed", "failed", "error"}:
            return "failed"
        if reason in {"max_turns", "max-turns"}:
            return "max_turns"
    return _api_run_status_to_local(run_status.get("status"))


def _start_api_run(
    client: LeaApiClient,
    task: str,
    project: dict[str, Any] | None,
    resume: bool | str = False,
) -> dict[str, Any]:
    signature = inspect.signature(client.start_run)
    params = signature.parameters
    has_var_kw = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    kwargs: dict[str, Any] = {}
    if "project" in params or has_var_kw:
        kwargs["project"] = project
    if resume and ("resume" in params or has_var_kw):
        kwargs["resume"] = resume
    return client.start_run(task, **kwargs)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError | socket.timeout) or "timed out" in str(exc).lower()


def run_lea(context: RunnerContext) -> None:
    if not active_run_lock.acquire(blocking=False):
        store.update_run(context.run_id, "failed", final_text="Another Lea run is already active.")
        store.touch_session(context.session_id, "failed")
        emit(context.events, "run_error", {"message": "Another Lea run is already active."})
        emit(context.events, "done", {"status": "failed"})
        return

    api_run_id: str | None = None
    client = context.client or LeaApiClient(context.config)
    assistant_chunks: list[str] = []
    assistant_turn_chunks: list[str] = []
    persisted_assistant_texts: list[str] = []
    final_text: str | None = None
    last_seq = -1
    emitted_files: set[tuple[str, int, int]] = set()
    emitted_payloads: set[tuple[str, str]] = set()
    terminal_status: str | None = None
    terminal_error: str | None = None
    api_session_id: str | None = None
    assistant_mode = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    code_step_emitted = False
    terminal_run_status: dict[str, Any] | None = None
    terminal_transcript: Any = None
    transcript_url: str | None = None
    tool_tracker = ToolTracker()
    usage_breakdown = UsageBreakdownCollector()

    try:
        store.update_run(context.run_id, "running")
        store.touch_session(context.session_id, "running")
        log_status(
            context,
            f"Starting Lea API run at {context.config.lea_api_base_url}",
            status="running",
            model=context.config.model,
            max_turns=context.config.max_turns,
        )

        session_row = store.get_session(context.session_id)
        resume = (session_row or {}).get("api_session_id") or False
        run = _start_api_run(client, context.task, context.project, resume=resume)
        api_run_id = str(run["run_id"])
        store.set_run_api_run_id(context.run_id, api_run_id)
        log_status(context, f"Lea API run started: {api_run_id}", status="api_run_started", api_run_id=api_run_id)
        if context.config.permission_tier == "theorem_translation":
            log_status(
                context,
                "Waiting for theorem translation preflight to produce an approvable Lean statement.",
                status="theorem_translation_preflight",
                api_run_id=api_run_id,
            )

        started = time.monotonic()
        attempts = 0
        while terminal_status is None and attempts < 2:
            attempts += 1
            timeout = max(1.0, context.config.lea_job_timeout_seconds - (time.monotonic() - started))
            for frame in client.stream_events(api_run_id, from_seq=last_seq + 1, timeout=timeout):
                _log_api_frame(context, api_run_id, frame)
                seq = _seq(frame)
                if seq is not None:
                    last_seq = max(last_seq, seq)

                frame_type = _event_type(frame)
                captured_session = _first_string(frame, "session_id")
                if captured_session:
                    api_session_id = captured_session
                if frame_type in {"finished", "done", "completed"} and str(
                    frame.get("reason") or ""
                ).lower() == "assistant":
                    assistant_mode = True
                if frame_type == "approval_requested":
                    usage_breakdown.notice_approval(frame)
                    _flush_assistant_turn(context, assistant_turn_chunks, persisted_assistant_texts)
                    payload = _approval_payload(frame, frame_type)
                    store.set_run_pending_approval(context.run_id, payload)
                    emit(context.events, "approval_requested", payload)
                    candidate = payload.get("candidate")
                    candidate_suffix = f" candidate {candidate}" if candidate is not None else ""
                    log_status(
                        context,
                        f"Waiting for theorem translation approval{candidate_suffix}.",
                        status="approval_requested",
                    )
                    continue
                if frame_type == "approval_resolved":
                    payload = _approval_payload(frame, frame_type)
                    store.set_run_pending_approval(context.run_id, None)
                    emit(context.events, "approval_resolved", payload)
                    decision = payload.get("decision") or "resolved"
                    log_status(
                        context,
                        f"Theorem translation {decision}.",
                        status="approval_resolved",
                    )
                    continue

                if frame_type == "turn_started":
                    usage_breakdown.notice_turn(frame)
                    _flush_assistant_turn(context, assistant_turn_chunks, persisted_assistant_texts)

                delta = _text_delta(frame)
                if delta:
                    assistant_chunks.append(delta)
                    assistant_turn_chunks.append(delta)
                    emit(context.events, "assistant_delta", {"text": delta})

                if frame_type in {"tool_call", "tool_called"}:
                    _flush_assistant_turn(context, assistant_turn_chunks, persisted_assistant_texts)

                frame_steps = _emit_code_payload(
                    context=context,
                    frame=frame,
                    emitted=emitted_files,
                    emitted_payloads=emitted_payloads,
                )
                if frame_steps:
                    code_step_emitted = True

                tracked_steps = _track_tool_frame(
                    context=context,
                    api_run_id=api_run_id,
                    frame=frame,
                    tracker=tool_tracker,
                    emitted=emitted_files,
                    emitted_payloads=emitted_payloads,
                )
                if tracked_steps:
                    frame_steps.extend(tracked_steps)
                    code_step_emitted = True

                message = _status_message(frame)
                if message:
                    status_payload: dict[str, Any] = {"status": _event_type(frame) or "status"}
                    if frame_steps:
                        status_payload["step_number"] = frame_steps[-1]["step_number"]
                    log_status(context, message, **status_payload)

                frame_input_tokens, frame_output_tokens = _usage(frame)
                frame_cost_usd = _cost(frame)
                if frame_type == "usage_updated":
                    usage_breakdown.add_usage(frame_input_tokens, frame_output_tokens, frame_cost_usd)
                    if frame_input_tokens is not None:
                        input_tokens = (input_tokens or 0) + frame_input_tokens
                    if frame_output_tokens is not None:
                        output_tokens = (output_tokens or 0) + frame_output_tokens
                    if frame_cost_usd is not None:
                        cost_usd = (cost_usd or 0.0) + frame_cost_usd
                    store.update_run(
                        context.run_id,
                        "running",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost_usd,
                    )
                    store.replace_run_usage_breakdown(
                        context.run_id,
                        usage_breakdown.final_rows(input_tokens, output_tokens, cost_usd),
                    )
                    emit(
                        context.events,
                        "usage_updated",
                        {
                            "run_id": context.run_id,
                            "session_id": context.session_id,
                            "input_tokens": input_tokens or 0,
                            "output_tokens": output_tokens or 0,
                            "total_tokens": int(input_tokens or 0) + int(output_tokens or 0),
                            "cost_usd": cost_usd or 0.0,
                            "delta_input_tokens": frame_input_tokens or 0,
                            "delta_output_tokens": frame_output_tokens or 0,
                            "delta_cost_usd": frame_cost_usd or 0.0,
                        },
                    )
                    if settings_service.spend_limit_reached(context.config.max_spend_usd, cost_usd):
                        terminal_status = "max_spend"
                        final_text = "Max spend limit reached. Lea run was cancelled."
                        log_status(context, final_text, status="max_spend", api_run_id=api_run_id)
                        try:
                            client.cancel_run(api_run_id)
                        except Exception as cancel_exc:
                            log_status(
                                context,
                                f"Failed to cancel Lea API run {api_run_id}: {cancel_exc}",
                                status="cancel_failed",
                            )
                        break
                else:
                    input_tokens = _merge_usage_total(input_tokens, frame_input_tokens)
                    output_tokens = _merge_usage_total(output_tokens, frame_output_tokens)
                    cost_usd = _merge_cost_total(cost_usd, frame_cost_usd)

                if terminal_status is None:
                    terminal_status = _terminal_status(frame)
                if terminal_status is not None:
                    final_text = _final_text(frame) or final_text
                    transcript_url = _first_string(frame, "transcript_url") or transcript_url
                    if terminal_status == "failed" and _event_type(frame) == "error":
                        terminal_error = final_text or "Lea API reported an error."
                    break

            if terminal_status is None:
                terminal_run_status = client.get_run(api_run_id)
                _log_api_frame(context, api_run_id, {"type": "run_status", **terminal_run_status})
                terminal_status = _api_run_terminal_status(terminal_run_status)
                status_input_tokens, status_output_tokens = _usage(terminal_run_status)
                input_tokens = _merge_usage_total(input_tokens, status_input_tokens)
                output_tokens = _merge_usage_total(output_tokens, status_output_tokens)
                status_cost_usd = _cost(terminal_run_status)
                cost_usd = _merge_cost_total(cost_usd, status_cost_usd)
                final_text = _final_text(terminal_run_status) or final_text
                transcript_url = _first_string(terminal_run_status, "transcript_url") or transcript_url
                if terminal_status is None:
                    log_status(context, f"Resuming Lea API stream from seq {last_seq + 1}.", status="stream_resume")

        if terminal_status is None:
            terminal_status = "failed"
            final_text = final_text or "Lea API stream ended before a terminal event."

        final_text = final_text or "".join(assistant_chunks).strip()
        if api_run_id:
            try:
                terminal_run_status = client.get_run(api_run_id)
                _log_api_frame(context, api_run_id, {"type": "run_status", **terminal_run_status})
                status_input_tokens, status_output_tokens = _usage(terminal_run_status)
                input_tokens = _merge_usage_total(input_tokens, status_input_tokens)
                output_tokens = _merge_usage_total(output_tokens, status_output_tokens)
                status_cost_usd = _cost(terminal_run_status)
                cost_usd = _merge_cost_total(cost_usd, status_cost_usd)
                transcript_url = _first_string(terminal_run_status, "transcript_url") or transcript_url
                api_session_id = _first_string(terminal_run_status, "session_id") or api_session_id
                result = terminal_run_status.get("result")
                if isinstance(result, dict) and str(result.get("reason") or "").lower() == "assistant":
                    assistant_mode = True
            except Exception as status_exc:
                log_status(
                    context,
                    f"Could not fetch final Lea API run status: {status_exc}",
                    status="status_fetch_failed",
                )

            try:
                terminal_transcript = client.get_transcript(api_run_id, transcript_url=transcript_url)
                _log_api_frame(context, api_run_id, {"type": "transcript", "transcript": terminal_transcript})
            except Exception as transcript_exc:
                log_status(
                    context,
                    f"Could not fetch final Lea API transcript: {transcript_exc}",
                    status="transcript_fetch_failed",
                )

        if _reconcile_terminal_artifacts(
            context=context,
            run_status=terminal_run_status,
            transcript=terminal_transcript,
            final_text=final_text,
            emitted=emitted_files,
            emitted_payloads=emitted_payloads,
        ):
            code_step_emitted = True

        # An assistant/QA turn (explain, look up a lemma) legitimately produces no Lean
        # file and runs no lean_check — it is not a proof, so the proof-completion
        # guards below must not fire. Persist the captured agent session so the next
        # turn in this chat resumes with full context.
        if api_session_id:
            store.set_session_api_session_id(context.session_id, api_session_id)

        if assistant_mode:
            if terminal_status not in {"failed", "max_spend"}:
                terminal_status = "success"
        else:
            if terminal_status == "success" and not code_step_emitted:
                _emit_terminal_no_code_step(context, api_run_id)
                terminal_status = "failed"
                terminal_error = (
                    "Lea API reported completion, but no readable Lean file or code artifact was exposed, "
                    "so the proof could not be verified."
                )
                log_status(context, terminal_error, status="missing_lean_artifact", api_run_id=api_run_id)
            elif terminal_status == "max_turns" and not code_step_emitted:
                _emit_terminal_no_code_step(context, api_run_id)

            terminal_status, tool_check_failure = _terminal_status_after_tool_checks(terminal_status, tool_tracker)
            if tool_check_failure:
                terminal_error = tool_check_failure
                log_status(context, tool_check_failure, status="lean_check_failed", api_run_id=api_run_id)

        _flush_assistant_turn(
            context,
            assistant_turn_chunks,
            persisted_assistant_texts,
            kind="chat" if assistant_mode else "assistant",
        )
        if assistant_mode:
            # Tag every assistant message from this run as a plain chat bubble so the
            # timeline doesn't pair it with a (nonexistent) code step.
            store.set_run_messages_kind(context.run_id, "chat")
        assistant_text = "".join(assistant_chunks).strip()
        if not persisted_assistant_texts and not assistant_text and terminal_transcript is not None:
            transcript_assistant_text = _assistant_text_from_transcript(terminal_transcript)
            if transcript_assistant_text:
                _emit_chat_message(
                    context,
                    "assistant",
                    transcript_assistant_text,
                    kind="chat" if assistant_mode else "assistant",
                )
                persisted_assistant_texts.append(transcript_assistant_text)
        terminal_notice = terminal_error or (
            final_text if terminal_status in {"failed", "max_turns", "max_spend"} else None
        )
        display_text = _display_terminal_text(terminal_status, terminal_error or final_text)

        if display_text and display_text not in persisted_assistant_texts:
            _emit_chat_message(
                context,
                "system" if terminal_notice else "assistant",
                display_text,
                kind="chat" if (assistant_mode and not terminal_notice) else "assistant",
            )
        if terminal_status == "failed":
            emit(context.events, "run_error", {"message": display_text or "Lea API run failed."})

        store.update_run(
            context.run_id,
            terminal_status,
            final_text=final_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        store.replace_run_usage_breakdown(
            context.run_id,
            usage_breakdown.final_rows(input_tokens, output_tokens, cost_usd),
        )
        store.set_run_pending_approval(context.run_id, None)
        store.touch_session(context.session_id, terminal_status)
        # A successful proof run gets a kernel-level SafeVerify audit. We only
        # flag it as pending here; the browser fires the (~100s) check via
        # /api/runs/{id}/safe-verify so it never blocks the `done` event.
        safe_verify_pending = (
            terminal_status == "success"
            and not assistant_mode
            and safe_verify_service.is_available(context.config)
        )
        if safe_verify_pending:
            store.set_run_safe_verify(context.run_id, "pending", None)
        emit(
            context.events,
            "done",
            {
                "status": terminal_status,
                "api_run_id": api_run_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "safe_verify": "pending" if safe_verify_pending else None,
            },
        )
    except Exception as exc:
        if api_run_id and _is_timeout(exc):
            try:
                client.cancel_run(api_run_id)
            except Exception as cancel_exc:
                log_status(context, f"Failed to cancel Lea API run {api_run_id}: {cancel_exc}", status="cancel_failed")
        elif api_run_id and isinstance(exc, LeaApiError):
            try:
                run_status = client.get_run(api_run_id)
                mapped_status = _api_run_terminal_status(run_status)
                if mapped_status:
                    final_text = _final_text(run_status) or str(exc)
                    status_input_tokens, status_output_tokens = _usage(run_status)
                    status_cost_usd = _cost(run_status)
                    merged_input_tokens = _merge_usage_total(input_tokens, status_input_tokens)
                    merged_output_tokens = _merge_usage_total(output_tokens, status_output_tokens)
                    merged_cost_usd = _merge_cost_total(cost_usd, status_cost_usd)
                    store.update_run(
                        context.run_id,
                        mapped_status,
                        final_text=final_text,
                        input_tokens=merged_input_tokens,
                        output_tokens=merged_output_tokens,
                        cost_usd=merged_cost_usd,
                    )
                    store.replace_run_usage_breakdown(
                        context.run_id,
                        usage_breakdown.final_rows(merged_input_tokens, merged_output_tokens, merged_cost_usd),
                    )
                    store.set_run_pending_approval(context.run_id, None)
                    store.touch_session(context.session_id, mapped_status)
                    emit(context.events, "run_error", {"message": final_text})
                    emit(context.events, "done", {"status": mapped_status, "api_run_id": api_run_id})
                    return
            except Exception:
                pass

        message = f"{type(exc).__name__}: {exc}"
        store.add_message(context.session_id, "system", message, context.run_id)
        store.update_run(context.run_id, "failed", final_text=message)
        store.replace_run_usage_breakdown(
            context.run_id,
            usage_breakdown.final_rows(input_tokens, output_tokens, cost_usd),
        )
        store.set_run_pending_approval(context.run_id, None)
        store.touch_session(context.session_id, "failed")
        emit(context.events, "run_error", {"message": message})
        emit(context.events, "done", {"status": "failed", "api_run_id": api_run_id})
    finally:
        active_run_lock.release()
