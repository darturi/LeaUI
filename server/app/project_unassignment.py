from __future__ import annotations

import importlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store
from .config import ROOT, LeaConfig


IMPORT_RE = re.compile(r"(?m)^\s*import\s+([A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\s*$")


@dataclass(frozen=True)
class ProjectUnassignmentError(Exception):
    status_code: int
    detail: dict[str, Any]


def check_project_theorem_unassignment(project: dict[str, Any], config: LeaConfig, theorem_name: str) -> dict[str, Any]:
    context = _build_context(project, config, theorem_name)
    return {
        "status": "safe",
        "theorem": _entry_payload(context["target"]),
        "planned_move": {
            "from_path": context["source_rel"],
            "to_path": context["dest_rel"],
            "from_module": context["target"].module_name,
            "to_module": context["dest_module"],
        },
    }


def unassign_project_theorem(project: dict[str, Any], config: LeaConfig, theorem_name: str) -> dict[str, Any]:
    context = _build_context(project, config, theorem_name)
    project_module = context["project_module"]
    source_path = context["source_path"]
    dest_path = context["dest_path"]
    project_path = context["project_path"]
    original_markdown = context["markdown"]
    original_text = source_path.read_text()
    moved = False
    code_steps: list[dict[str, Any]] = []

    try:
        updated_markdown, _ = project_module.remove_project_entry(original_markdown, theorem_name)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        rewritten = _rewrite_namespace(original_text, context["project_namespace"], "Lea.Misc")
        source_path.rename(dest_path)
        moved = True
        dest_path.write_text(rewritten)
        project_path.write_text(updated_markdown)
        _verify_lean_file(config, dest_path)
        from .project_usage import detect_used_project_formalizations

        message = f"Unassigned {context['target'].name} from {project['title']} and moved it to {context['dest_rel']}."
        code_steps = store.record_project_unassignment(
            context["affected_sessions"],
            str(project["id"]),
            context["dest_rel"],
            rewritten,
            message,
            used_project_formalizations=detect_used_project_formalizations(
                project=project,
                config=config,
                code=rewritten,
                proof_path=context["dest_rel"],
            ),
        )
    except Exception:
        if moved:
            if dest_path.exists():
                dest_path.unlink()
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(original_text)
        project_path.write_text(original_markdown)
        raise

    return {
        "status": "unassigned",
        "theorem": _entry_payload(context["target"]),
        "move": {
            "from_path": context["source_rel"],
            "to_path": context["dest_rel"],
            "from_module": context["target"].module_name,
            "to_module": context["dest_module"],
        },
        "code_step": code_steps[0] if code_steps else None,
        "affected_session_ids": [str(step["session_id"]) for step in code_steps],
    }


def project_theorem_for_proof_path(project: dict[str, Any], config: LeaConfig, proof_path: str | None) -> dict[str, Any] | None:
    if not proof_path:
        return None
    project_module = _load_project_module(config)
    project_path = _project_markdown_path(project, config)
    if not project_path.exists():
        return None
    requested = _normalize_rel_path(proof_path)
    for entry in project_module.parse_project_entries(project_path.read_text()):
        if _normalize_rel_path(entry.proof_path) == requested:
            return _entry_payload(entry)
    return None


def _build_context(project: dict[str, Any], config: LeaConfig, theorem_name: str) -> dict[str, Any]:
    project_module = _load_project_module(config)
    project_path = _project_markdown_path(project, config)
    if not project_path.exists():
        raise ProjectUnassignmentError(404, {"message": "Project markdown file not found."})

    markdown = project_path.read_text()
    target = project_module.project_entry_for_theorem(markdown, theorem_name)
    if target is None:
        raise ProjectUnassignmentError(404, {"message": f"Project theorem `{theorem_name}` was not found."})

    project_namespace = project_module.project_namespace(str(project["slug"]))
    project_part = project_module.project_namespace_part(str(project["slug"]))
    source_path = _workspace_path(config, target.proof_path)
    source_rel = _workspace_rel(config, source_path)
    if not source_path.exists():
        raise ProjectUnassignmentError(404, {"message": f"Proof file `{target.proof_path}` was not found."})
    if not _is_project_proof_path(config, source_path, project_part):
        raise ProjectUnassignmentError(
            409,
            {
                "conflict_type": "not_project_namespace",
                "message": f"`{target.name}` is not stored under `{project_namespace}` and cannot be unassigned from this project.",
                "theorem": _entry_payload(target),
            },
        )
    if target.module_name and not target.module_name.startswith(f"{project_namespace}."):
        raise ProjectUnassignmentError(
            409,
            {
                "conflict_type": "not_project_namespace",
                "message": f"`{target.name}` is recorded as `{target.module_name}`, not a `{project_namespace}` module.",
                "theorem": _entry_payload(target),
            },
        )

    affected_sessions = store.sessions_with_latest_code_path(source_rel)
    entries = [
        entry
        for entry in project_module.parse_project_entries(markdown)
        if entry.module_name and entry.module_name.startswith(f"{project_namespace}.")
    ]
    entry_text = {_entry_key(entry): _read_entry_text(config, entry) for entry in entries}
    imports_by_module = {
        entry.module_name: set(IMPORT_RE.findall(entry_text[_entry_key(entry)]))
        for entry in entries
        if entry.module_name
    }
    target_text = source_path.read_text()
    conflicts = _dependency_conflicts(
        entries=entries,
        target=target,
        target_text=target_text,
        entry_text=entry_text,
        imports_by_module=imports_by_module,
        project_namespace=project_namespace,
    )
    if conflicts:
        raise ProjectUnassignmentError(409, conflicts)

    dest_path = _misc_destination(config, source_path.name)
    dest_rel = _workspace_rel(config, dest_path)
    return {
        "project_module": project_module,
        "project_path": project_path,
        "project_namespace": project_namespace,
        "markdown": markdown,
        "target": target,
        "source_path": source_path,
        "source_rel": source_rel,
        "affected_sessions": affected_sessions,
        "dest_path": dest_path,
        "dest_rel": dest_rel,
        "dest_module": _module_from_proof_rel(dest_rel),
    }


def _dependency_conflicts(
    *,
    entries: list[Any],
    target: Any,
    target_text: str,
    entry_text: dict[tuple[str, str], str],
    imports_by_module: dict[str, set[str]],
    project_namespace: str,
) -> dict[str, Any] | None:
    target_module = target.module_name
    other_entries = [entry for entry in entries if entry.name != target.name]

    if target_module:
        dependents = []
        for entry in other_entries:
            reasons: list[str] = []
            if entry.module_name and _module_imports(entry.module_name, target_module, imports_by_module):
                reasons.append(f"imports `{target_module}`")
            if _references_theorem(entry_text.get(_entry_key(entry), ""), target.name):
                reasons.append(f"references `{target.name}`")
            if reasons:
                dependents.append({**_entry_payload(entry), "reasons": reasons})
        if dependents:
            first = dependents[0]
            return {
                "conflict_type": "used_by_project_theorems",
                "message": f"Cannot unassign `{target.name}`; `{first['name']}` {first['reasons'][0]}.",
                "theorem": _entry_payload(target),
                "conflicts": dependents,
            }

    used = []
    target_imports = set(IMPORT_RE.findall(target_text))
    for entry in other_entries:
        reasons: list[str] = []
        if entry.module_name and (
            entry.module_name in target_imports
            or any(imported.startswith(f"{entry.module_name}.") for imported in target_imports)
        ):
            reasons.append(f"imports `{entry.module_name}`")
        if _references_theorem(target_text, entry.name):
            reasons.append(f"references `{entry.name}`")
        if reasons:
            used.append({**_entry_payload(entry), "reasons": reasons})
    project_imports = sorted(module for module in target_imports if module.startswith(f"{project_namespace}."))
    if used or project_imports:
        first_reason = used[0]["reasons"][0] if used else f"imports `{project_imports[0]}`"
        return {
            "conflict_type": "uses_project_theorems",
            "message": f"Cannot unassign `{target.name}`; it {first_reason}.",
            "theorem": _entry_payload(target),
            "conflicts": used,
            "project_imports": project_imports,
        }
    return None


def _module_imports(module: str, target_module: str, imports_by_module: dict[str, set[str]]) -> bool:
    seen: set[str] = set()
    stack = [module]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        imports = imports_by_module.get(current, set())
        if target_module in imports:
            return True
        stack.extend(imported for imported in imports if imported in imports_by_module)
    return False


def _references_theorem(text: str, theorem_name: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_']){re.escape(theorem_name)}(?![A-Za-z0-9_'])"
    return re.search(pattern, text) is not None


def _rewrite_namespace(text: str, old_namespace: str, new_namespace: str) -> str:
    updated = re.sub(rf"(?m)^namespace\s+{re.escape(old_namespace)}\s*$", f"namespace {new_namespace}", text)
    updated = re.sub(rf"(?m)^end\s+{re.escape(old_namespace)}\s*$", f"end {new_namespace}", updated)
    if updated == text:
        raise ValueError(f"Proof file does not contain namespace wrapper `{old_namespace}`.")
    return updated


def _verify_lean_file(config: LeaConfig, path: Path) -> None:
    if config.lea_root is None:
        raise ValueError("lea_root is required for project unassignment.")
    try:
        result = subprocess.run(
            ["lake", "env", "lean", str(path)],
            cwd=config.lea_root / "workspace",
            capture_output=True,
            text=True,
            timeout=config.lea_job_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"Lean verification timed out after {config.lea_job_timeout_seconds}s.") from exc
    except FileNotFoundError as exc:
        raise ValueError("`lake` was not found while verifying the moved proof.") from exc
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise ValueError(output or f"Lean verification failed with exit code {result.returncode}.")


def _load_project_module(config: LeaConfig):
    if config.lea_root is None:
        raise ProjectUnassignmentError(422, {"message": "lea_root is required for project unassignment."})
    import_root = config.lea_root if (config.lea_root / "lea").exists() else ROOT / "external" / "lea-prover"
    root = str(import_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    module = importlib.import_module("lea.project")
    module.REPO_ROOT = config.lea_root
    module.WORKSPACE_ROOT = config.lea_root / "workspace"
    module.PROJECTS_ROOT = config.lea_root / "workspace" / "projects"
    return module


def _project_markdown_path(project: dict[str, Any], config: LeaConfig) -> Path:
    if config.lea_root is None:
        raise ProjectUnassignmentError(422, {"message": "lea_root is required for project unassignment."})
    path = Path(str(project["path"]))
    full_path = path if path.is_absolute() else config.lea_root / path
    _assert_inside(full_path, config.lea_root)
    if full_path.suffix != ".md":
        raise ProjectUnassignmentError(422, {"message": "Project path must be a markdown file."})
    return full_path


def _workspace_path(config: LeaConfig, path: str) -> Path:
    if config.lea_root is None:
        raise ProjectUnassignmentError(422, {"message": "lea_root is required for project unassignment."})
    candidate = Path(path)
    full_path = candidate if candidate.is_absolute() else config.lea_root / candidate
    _assert_inside(full_path, config.lea_root)
    return full_path


def _workspace_rel(config: LeaConfig, path: Path) -> str:
    if config.lea_root is None:
        return str(path)
    return str(path.resolve().relative_to(config.lea_root.resolve()))


def _assert_inside(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ProjectUnassignmentError(422, {"message": "Path must be inside lea_root."}) from exc


def _is_project_proof_path(config: LeaConfig, path: Path, project_part: str) -> bool:
    if config.lea_root is None:
        return False
    project_dir = (config.lea_root / "workspace" / "proofs" / "Lea" / project_part).resolve()
    try:
        path.resolve().relative_to(project_dir)
    except ValueError:
        return False
    return path.suffix == ".lean"


def _misc_destination(config: LeaConfig, filename: str) -> Path:
    if config.lea_root is None:
        raise ProjectUnassignmentError(422, {"message": "lea_root is required for project unassignment."})
    misc_dir = config.lea_root / "workspace" / "proofs" / "Lea" / "Misc"
    candidate = misc_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    index = 2
    while True:
        next_candidate = misc_dir / f"{stem}_unassigned_{index}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        index += 1


def _module_from_proof_rel(path: str) -> str | None:
    rel = Path(path)
    try:
        proof_rel = rel.relative_to(Path("workspace") / "proofs")
    except ValueError:
        return None
    if proof_rel.suffix != ".lean" or len(proof_rel.parts) < 2:
        return None
    return ".".join(proof_rel.with_suffix("").parts)


def _read_entry_text(config: LeaConfig, entry: Any) -> str:
    try:
        path = _workspace_path(config, entry.proof_path)
    except ProjectUnassignmentError:
        return ""
    return path.read_text() if path.exists() else ""


def _entry_payload(entry: Any) -> dict[str, Any]:
    return {
        "name": entry.name,
        "proof_path": entry.proof_path,
        "module_name": entry.module_name,
    }


def _entry_key(entry: Any) -> tuple[str, str]:
    return (entry.name, entry.proof_path)


def _normalize_rel_path(path: str) -> str:
    return str(Path(path))
