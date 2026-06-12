from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .config import LeaConfig
from .project_unassignment import (
    _load_project_module,
    _module_from_proof_rel,
    _project_markdown_path,
    _rewrite_namespace,
    _verify_lean_file,
    _workspace_path,
    _workspace_rel,
)
from .project_usage import detect_used_project_formalizations


@dataclass(frozen=True)
class ProjectAssignmentError(Exception):
    status_code: int
    detail: dict[str, Any]


def check_project_assignment(session_id: str, project: dict[str, Any], config: LeaConfig) -> dict[str, Any]:
    context = _build_context(session_id, project, config)
    return _assignment_payload("safe", context, context["entry_action"])


def assign_project(session_id: str, project: dict[str, Any], config: LeaConfig) -> dict[str, Any]:
    context = _build_context(session_id, project, config)
    project_module = context["project_module"]
    source_path = context["source_path"]
    dest_path = context["dest_path"]
    project_path = context["project_path"]
    original_markdown = context["markdown"]
    project_existed = project_path.exists()
    original_text = source_path.read_text()
    moved = False
    previous_project_id = context["session"].get("project_id")

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_namespace(original_text, "Lea.Misc", context["project_namespace"])
        source_path.rename(dest_path)
        moved = True
        dest_path.write_text(rewritten)
        updated_markdown, action = project_module.upsert_entry(
            original_markdown,
            context["theorem_name"],
            context["entry"],
        )
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(updated_markdown)
        _verify_lean_file(config, dest_path)
        store.assign_session_project(session_id, project["id"])
        code_step = store.add_code_step(
            session_id,
            context["run_id"],
            context["dest_rel"],
            rewritten,
            summary=f"Moved proof into project namespace {context['project_namespace']}.",
            used_project_formalizations=detect_used_project_formalizations(
                project=project,
                config=config,
                code=rewritten,
                proof_path=context["dest_rel"],
            ),
        )
        message = f"Assigned {context['theorem_name']} to {project['title']} and moved it to {context['dest_rel']}."
        store.add_message(session_id, "system", message, context["run_id"])
        store.add_status_event(
            session_id,
            context["run_id"],
            message,
            status="project_assigned",
            step_number=code_step["step_number"],
        )
    except Exception:
        if moved:
            if dest_path.exists():
                dest_path.unlink()
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(original_text)
        if project_existed:
            project_path.write_text(original_markdown)
        elif project_path.exists():
            project_path.unlink()
        store.assign_session_project(session_id, previous_project_id)
        raise

    context = {**context, "entry_action": action}
    return {
        **_assignment_payload("assigned", context, action),
        "code_step": code_step,
    }


def failed_assignment_guidance() -> dict[str, Any]:
    return {
        "status": "retry_required",
        "message": (
            "This proof did not complete successfully. Retry the proof in this chat with a project selected; "
            "if the retry succeeds, the formalization will join that project. Failed formalizations are not moved."
        ),
    }


def _build_context(session_id: str, project: dict[str, Any], config: LeaConfig) -> dict[str, Any]:
    detail = store.session_detail(session_id)
    if not detail:
        raise ProjectAssignmentError(404, {"message": "Session not found."})
    if detail.get("active_run"):
        raise ProjectAssignmentError(409, {"message": "Cannot assign a project while the session is running."})
    if detail.get("status") != "success":
        raise ProjectAssignmentError(409, failed_assignment_guidance())

    final_step = _final_lean_code_step(detail)
    if final_step is None:
        raise ProjectAssignmentError(409, {"message": "No completed Lean proof file was found for this session."})

    project_module = _load_project_module(config)
    project_path = _project_markdown_path(project, config)
    markdown = project_path.read_text() if project_path.exists() else _project_header(project_module, project)
    project_namespace = project_module.project_namespace(str(project["slug"]))
    project_part = project_module.project_namespace_part(str(project["slug"]))
    source_path = _workspace_path(config, str(final_step["path"]))
    source_rel = _workspace_rel(config, source_path)
    if not source_path.exists():
        raise ProjectAssignmentError(404, {"message": f"Proof file `{source_rel}` was not found."})
    if not _is_misc_proof_path(config, source_path):
        if detail.get("project_id"):
            raise ProjectAssignmentError(
                409,
                {
                    "conflict_type": "already_associated",
                    "message": "This formalization is already associated with a project. Reassignment is not supported yet.",
                },
            )
        raise ProjectAssignmentError(
            409,
            {
                "conflict_type": "not_unassigned",
                "message": "Only successful proofs stored under `Lea.Misc` can be assigned retroactively.",
            },
        )

    theorem_name = (
        project_module.theorem_name_from_file(source_rel)
        or project_module.theorem_name_from_signature(str(final_step.get("code") or ""))
        or source_path.stem
    )
    if project_module.project_entry_for_theorem(markdown, theorem_name):
        raise ProjectAssignmentError(
            409,
            {
                "conflict_type": "duplicate_theorem",
                "message": f"Project `{project['title']}` already has an entry for `{theorem_name}`.",
                "theorem": {"name": theorem_name, "proof_path": source_rel, "module_name": None},
            },
        )

    dest_path = (
        config.lea_root / "workspace" / "proofs" / "Lea" / project_part / source_path.name
        if config.lea_root
        else source_path
    )
    dest_rel = _workspace_rel(config, dest_path)
    dest_module = _module_from_proof_rel(dest_rel)
    if dest_path.exists():
        raise ProjectAssignmentError(
            409,
            {
                "conflict_type": "destination_exists",
                "message": f"`{dest_rel}` already exists. Choose a different theorem filename before assigning it.",
                "theorem": {"name": theorem_name, "proof_path": source_rel, "module_name": None},
            },
        )
    if dest_module is None or not dest_module.startswith(f"{project_namespace}."):
        raise ProjectAssignmentError(
            409,
            {"message": f"Could not derive a `{project_namespace}` module for `{dest_rel}`."},
        )

    signature = (
        project_module.signature_from_file(source_rel)
        or project_module.normalize_signature(str(final_step.get("code") or ""))
        or f"theorem {theorem_name} : _ := by"
    )
    description = _first_user_message(detail) or str(detail.get("title") or theorem_name)
    entry = project_module.render_project_entry(
        theorem_name=theorem_name,
        proof_path=dest_rel,
        module_name=dest_module,
        signature=signature,
        description=description,
        solving_process="This proof was completed successfully, then retroactively assigned to this project.",
    )
    _, entry_action = project_module.upsert_entry(markdown, theorem_name, entry)
    return {
        "session": detail,
        "run_id": str(final_step["run_id"]),
        "project_module": project_module,
        "project_path": project_path,
        "project_namespace": project_namespace,
        "markdown": markdown,
        "source_path": source_path,
        "source_rel": source_rel,
        "dest_path": dest_path,
        "dest_rel": dest_rel,
        "dest_module": dest_module,
        "theorem_name": theorem_name,
        "signature": signature,
        "entry": entry,
        "entry_action": entry_action,
    }


def _assignment_payload(status: str, context: dict[str, Any], entry_action: str) -> dict[str, Any]:
    return {
        "status": status,
        "theorem": {
            "name": context["theorem_name"],
            "proof_path": context["dest_rel"],
            "module_name": context["dest_module"],
        },
        "planned_move": {
            "from_path": context["source_rel"],
            "to_path": context["dest_rel"],
            "from_module": _module_from_proof_rel(context["source_rel"]),
            "to_module": context["dest_module"],
        },
        "entry_action": entry_action,
    }


def _final_lean_code_step(detail: dict[str, Any]) -> dict[str, Any] | None:
    for step in reversed(detail.get("code_steps") or []):
        if step.get("kind", "code") == "code" and str(step.get("path") or "").endswith(".lean"):
            return step
    return None


def _first_user_message(detail: dict[str, Any]) -> str | None:
    for message in detail.get("messages") or []:
        if message.get("role") == "user" and str(message.get("content") or "").strip():
            return str(message["content"]).strip()
    return None


def _is_misc_proof_path(config: LeaConfig, path: Path) -> bool:
    if config.lea_root is None:
        return False
    misc_dir = (config.lea_root / "workspace" / "proofs" / "Lea" / "Misc").resolve()
    try:
        path.resolve().relative_to(misc_dir)
    except ValueError:
        return False
    return path.suffix == ".lean"


def _project_header(project_module: Any, project: dict[str, Any]) -> str:
    project_id = project_module.validate_project_id(str(project["slug"]))
    return f"# Project {project['slug']}\n\n<!-- lea:project id=\"{project_id}\" -->\n"
