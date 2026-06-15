from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, StrictInt

from .config import load_config
from .db import init_db
from .lea_api_client import LeaApiClient, LeaApiError
from .project_assignment import (
    ProjectAssignmentError,
    assign_project,
    check_project_assignment,
)
from .project_unassignment import (
    ProjectUnassignmentError,
    check_project_theorem_unassignment,
    project_theorem_for_proof_path,
    unassign_project_theorem,
)
from .runner import RunnerContext, run_lea
from . import safe_verify as safe_verify_service
from . import settings as settings_service
from . import store


app = FastAPI(title="Lea Interface API")
logger = logging.getLogger("lea-interface.settings")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    message: str
    session_id: str | None = None
    project_id: str | None = None


class ProjectRequest(BaseModel):
    slug: str | None = None
    title: str | None = None
    path: str | None = None


class ProjectAssignmentRequest(BaseModel):
    project_id: str


class ApprovalDecisionRequest(BaseModel):
    decision: str
    feedback: str | None = None


class ApiKeyUpdateRequest(BaseModel):
    value: str | None = None
    clear: bool = False


class SettingsRequest(BaseModel):
    model: str | None = None
    permission_tier: str | None = None
    theorem_translation_max_retries: StrictInt | None = None
    max_turns: int | None = None
    max_spend_usd: float | None = None
    api_keys: dict[str, ApiKeyUpdateRequest] | None = None


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/sessions")
def sessions() -> dict:
    return {"sessions": store.list_sessions()}


@app.get("/api/stats")
def stats() -> dict:
    return store.usage_stats()


@app.get("/api/projects")
def projects() -> dict:
    return {"projects": store.list_projects()}


@app.post("/api/projects")
def create_project(request: ProjectRequest) -> dict:
    slug = request.slug or request.title or ""
    try:
        return store.create_project(slug=slug, title=request.title, path=request.path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/projects/{project_id}")
def project_detail(project_id: str) -> dict:
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.post("/api/projects/{project_id}/theorems/{theorem_name}/unassignment-check")
def project_theorem_unassignment_check(project_id: str, theorem_name: str) -> dict:
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return check_project_theorem_unassignment(project, load_config(), theorem_name)
    except ProjectUnassignmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@app.post("/api/projects/{project_id}/theorems/{theorem_name}/unassign")
def project_theorem_unassign(project_id: str, theorem_name: str) -> dict:
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return unassign_project_theorem(project, load_config(), theorem_name)
    except ProjectUnassignmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc


@app.put("/api/projects/{project_id}")
def update_project(project_id: str, request: ProjectRequest) -> dict:
    project = store.update_project(project_id, title=request.title, path=request.path)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.get("/api/settings")
def settings() -> dict:
    return settings_service.settings_payload()


@app.get("/api/models")
def models() -> dict:
    """Full LiteLLM chat-model catalog for the searchable model picker."""
    return {"models": settings_service.model_catalog()}


@app.get("/api/models/requirements")
def model_requirements(model: str) -> dict:
    """Which API key(s) a model needs and whether they're configured."""
    return settings_service.model_requirements(model)


@app.put("/api/settings")
def update_settings(request: SettingsRequest) -> dict:
    try:
        return settings_service.update_settings(request.dict(exclude_unset=True))
    except settings_service.SettingsValidationError as exc:
        logger.warning("Settings validation failed: field=%s message=%s", exc.field, str(exc))
        raise HTTPException(status_code=422, detail={"message": str(exc), "field": exc.field}) from exc
    except ValueError as exc:
        logger.warning("Settings update failed: %s", str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    detail = store.session_detail(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    detail["project_theorem"] = _project_theorem_for_session(detail)
    return detail


@app.post("/api/sessions/{session_id}/project-assignment-check")
def session_project_assignment_check(session_id: str, request: ProjectAssignmentRequest) -> dict:
    project = store.get_project(request.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return check_project_assignment(session_id, project, load_config())
    except ProjectAssignmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ProjectUnassignmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc


@app.post("/api/sessions/{session_id}/assign-project")
def session_assign_project(session_id: str, request: ProjectAssignmentRequest) -> dict:
    project = store.get_project(request.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        return assign_project(session_id, project, load_config())
    except ProjectAssignmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ProjectUnassignmentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc


@app.post("/api/runs")
def create_run(request: RunRequest) -> dict:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    config = load_config()
    if settings_service.spend_limit_reached(config.max_spend_usd):
        raise HTTPException(status_code=402, detail="Max spend limit has been reached.")
    project = None
    if request.project_id:
        project = store.get_project(request.project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

    if request.session_id:
        session = store.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if project and session.get("project_id") not in {None, project["id"]}:
            raise HTTPException(status_code=409, detail="Session already belongs to a different project")
    else:
        session = store.create_session(message, project_id=project["id"] if project else None)

    project_id = project["id"] if project else session.get("project_id")
    if project and session.get("project_id") is None:
        store.assign_session_project(session["id"], project["id"])
        session = store.get_session(session["id"]) or session
    run = store.create_run(session["id"], config.model, None, config.max_turns, project_id=project_id)
    user_message = store.add_message(session["id"], "user", message, run["id"])
    return {
        "session_id": session["id"],
        "run_id": run["id"],
        "message": user_message,
    }


_TRANSLATION_APPROVAL_MARKER = "-theorem-translation-"


def _cleanup_translation_proposals(approval_id: str, config) -> None:
    """Best-effort removal of throwaway theorem-translation proposal files.

    The Lea prover writes each preflight skeleton to
    ``<lea_root>/workspace/proofs/.lea_proposals/<session>_theorem_translation_<n>.lean``
    purely as a ``lean_check`` target. The ``approval_id`` is
    ``<session>-theorem-translation-<candidate>``, so we can recover the session
    prefix and drop the whole session's candidates. Once accepted the prover
    uses the in-memory code and never reads these files again, so deletion is
    safe and never fails the request.
    """
    idx = approval_id.rfind(_TRANSLATION_APPROVAL_MARKER)
    if idx <= 0 or config.lea_root is None:
        return
    session_prefix = approval_id[:idx]
    proposals_dir = config.lea_root / "workspace" / "proofs" / ".lea_proposals"
    try:
        for path in proposals_dir.glob(f"{session_prefix}_theorem_translation_*.lean"):
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to clean up theorem-translation proposals: %s", exc)


@app.post("/api/runs/{run_id}/approvals/{approval_id}")
def resolve_approval(run_id: str, approval_id: str, request: ApprovalDecisionRequest) -> dict:
    if request.decision not in {"accept", "reject"}:
        raise HTTPException(status_code=422, detail="decision must be 'accept' or 'reject'")
    feedback = request.feedback.strip() if request.feedback is not None else None
    if request.decision == "reject" and not feedback:
        raise HTTPException(status_code=422, detail="feedback is required when rejecting an approval")

    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    api_run_id = run.get("api_run_id")
    if not api_run_id:
        raise HTTPException(status_code=409, detail="Run is not ready for approval")

    config = load_config()
    try:
        result = LeaApiClient(config).resolve_approval(
            str(api_run_id),
            approval_id,
            request.decision,
            feedback if request.decision == "reject" else None,
        )
    except LeaApiError as exc:
        status_code = exc.status if exc.status in {400, 401, 403, 404, 409, 422} else 502
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    if request.decision == "accept":
        _cleanup_translation_proposals(approval_id, config)
    return result


_SAFE_VERIFY_TERMINAL = {"passed", "failed", "unavailable", "error"}


@app.post("/api/runs/{run_id}/safe-verify")
def safe_verify_run(run_id: str) -> dict:
    """Run SafeVerify on a finished run's proof (kernel replay + axiom audit).

    Idempotent: a cached terminal verdict is returned without re-running the
    ~100s check. Concurrent callers get ``running`` instead of queuing a second.
    """
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    cached = run.get("safe_verify_status")
    if cached in _SAFE_VERIFY_TERMINAL:
        return {"status": cached, "detail": run.get("safe_verify_detail")}

    config = load_config()
    if not safe_verify_service.is_available(config):
        store.set_run_safe_verify(run_id, "unavailable", "SafeVerify is not built on this server.")
        return {"status": "unavailable", "detail": "SafeVerify is not built on this server."}

    step = store.last_lean_code_step_for_run(run_id)
    if not step:
        store.set_run_safe_verify(run_id, "error", "No Lean proof file was found for this run.")
        return {"status": "error", "detail": "No Lean proof file was found for this run."}

    if not safe_verify_service.try_acquire():
        store.set_run_safe_verify(run_id, "running", None)
        return {"status": "running", "detail": "SafeVerify is already running."}
    try:
        store.set_run_safe_verify(run_id, "running", None)
        result = safe_verify_service.verify(config, str(step.get("code") or ""), str(step.get("path") or ""))
    finally:
        safe_verify_service.release()
    store.set_run_safe_verify(run_id, result["status"], result.get("detail"))
    return result


def sse(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] not in {"pending", "running"}:
        raise HTTPException(status_code=409, detail="Run has already completed")

    queue: Queue[dict] = Queue()
    session = store.session_detail(run["session_id"])
    if not session or not session["messages"]:
        raise HTTPException(status_code=404, detail="Run task not found")

    task = next(
        (message["content"] for message in reversed(session["messages"]) if message["run_id"] == run_id and message["role"] == "user"),
        None,
    )
    if not task:
        raise HTTPException(status_code=404, detail="Run task not found")

    config = load_config()
    context = RunnerContext(
        session_id=run["session_id"],
        run_id=run_id,
        task=task,
        config=config,
        events=queue,
        project=_project_payload(run.get("project_id"), config),
    )
    thread = Thread(target=run_lea, args=(context,), daemon=True)
    thread.start()

    async def stream_events():
        while True:
            try:
                item = queue.get_nowait()
            except Empty:
                if not thread.is_alive():
                    break
                await asyncio.sleep(0.1)
                continue

            yield sse(item["type"], item["payload"])
            if item["type"] == "done":
                break

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _project_payload(project_id: str | None, config) -> dict | None:
    if not project_id:
        return None
    project = store.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if config.lea_root is None:
        raise HTTPException(status_code=422, detail="lea_root is required for project context")
    path = Path(str(project["path"]))
    full_path = path if path.is_absolute() else config.lea_root / path
    try:
        full_path.resolve().relative_to(config.lea_root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Project path must be inside lea_root") from exc
    if full_path.suffix != ".md":
        raise HTTPException(status_code=422, detail="Project path must be a markdown file")
    context = full_path.read_text() if full_path.exists() else ""
    return {
        "project_id": project["slug"],
        "project_path": str(path),
        "project_context": context,
        "record_on_success": True,
    }


def _project_theorem_for_session(detail: dict) -> dict | None:
    project = detail.get("project")
    if not project:
        return None
    code_steps = detail.get("code_steps") or []
    proof_path = None
    for step in reversed(code_steps):
        if step.get("kind", "code") == "code" and str(step.get("path") or "").endswith(".lean"):
            proof_path = str(step["path"])
            break
    if not proof_path:
        return None
    try:
        return project_theorem_for_proof_path(project, load_config(), proof_path)
    except Exception:
        logger.debug("Unable to resolve project theorem for session %s", detail.get("id"), exc_info=True)
        return None


# --- Static frontend (bundled / single-container deploy) --------------------
# In dev, Vite (:5173) serves the UI and proxies /api here, so this is skipped
# (LEA_WEB_DIST is unset). In the Docker image LEA_WEB_DIST points at the built
# `dist/`; the adapter then serves it on :8001 with SPA fallback. This route is
# registered last, so every /api/* route above takes priority over it.
_WEB_DIST = os.environ.get("LEA_WEB_DIST")
if _WEB_DIST and Path(_WEB_DIST).is_dir():
    _web_root = Path(_WEB_DIST).resolve()

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # API paths are handled by the routes above; never hand them index.html.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        # Serve a real built asset when it exists and stays inside the dist root.
        candidate = (_web_root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(_web_root):
            return FileResponse(candidate)
        # Otherwise hand back index.html for the SPA / client-side routing.
        return FileResponse(_web_root / "index.html")
