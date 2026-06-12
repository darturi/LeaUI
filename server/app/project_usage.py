from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config import LeaConfig
from .project_unassignment import (
    IMPORT_RE,
    _entry_payload,
    _load_project_module,
    _project_markdown_path,
    _workspace_path,
)


def detect_used_project_formalizations(
    *,
    project: dict[str, Any] | None,
    config: LeaConfig,
    code: str,
    proof_path: str | None = None,
) -> list[dict[str, Any]]:
    if not project or not code.strip():
        return []
    try:
        project_module = _load_project_module(config)
        project_path = _project_markdown_path(_project_dict(project), config)
    except Exception:
        return []
    if not project_path.exists():
        return []

    project_payload = _project_payload(_project_dict(project))
    current_path = _normalize_rel_path(proof_path)
    imports = set(IMPORT_RE.findall(code))
    searchable_code = _without_import_lines(code)
    used: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in project_module.parse_project_entries(project_path.read_text()):
        if current_path and _normalize_rel_path(entry.proof_path) == current_path:
            continue
        if not entry.module_name or not _imports_module(imports, entry.module_name):
            continue
        if not _references_identifier(searchable_code, entry.name):
            continue
        key = (entry.name, entry.proof_path)
        if key in seen:
            continue
        seen.add(key)
        used.append({**_entry_payload(entry), **project_payload})
    return used


def detect_project_formalization_dependents(
    *,
    project: dict[str, Any] | None,
    config: LeaConfig,
    proof_path: str | None,
) -> list[dict[str, Any]]:
    if not project or not proof_path:
        return []
    try:
        project_module = _load_project_module(config)
        project_dict = _project_dict(project)
        project_path = _project_markdown_path(project_dict, config)
    except Exception:
        return []
    if not project_path.exists():
        return []

    try:
        entries = project_module.parse_project_entries(project_path.read_text())
    except Exception:
        return []
    current_path = _normalize_rel_path(proof_path)
    current = next(
        (entry for entry in entries if _normalize_rel_path(entry.proof_path) == current_path),
        None,
    )
    if current is None or not current.module_name:
        return []

    project_payload = _project_payload(project_dict)
    dependents: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if _normalize_rel_path(entry.proof_path) == current_path:
            continue
        code = _read_entry_code(config, entry)
        if not code:
            continue
        imports = set(IMPORT_RE.findall(code))
        searchable_code = _without_import_lines(code)
        if not _imports_module(imports, current.module_name):
            continue
        if not _references_identifier(searchable_code, current.name):
            continue
        key = (entry.name, entry.proof_path)
        if key in seen:
            continue
        seen.add(key)
        dependents.append({**_entry_payload(entry), **project_payload})
    return dependents


def _project_dict(project: dict[str, Any]) -> dict[str, Any]:
    path = project.get("path") or project.get("project_path")
    slug = project.get("slug") or project.get("project_id") or project.get("id")
    return {**project, "path": path, "slug": slug}


def _project_payload(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": project.get("id") or project.get("project_id"),
        "project_slug": project.get("slug") or project.get("project_id"),
        "project_title": project.get("title") or project.get("project_title") or project.get("slug") or project.get("project_id"),
        "project_path": project.get("path") or project.get("project_path"),
    }


def _imports_module(imports: set[str], module_name: str) -> bool:
    return module_name in imports or any(imported.startswith(f"{module_name}.") for imported in imports)


def _references_identifier(code: str, name: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_']){re.escape(name)}(?![A-Za-z0-9_'])"
    return re.search(pattern, code) is not None


def _without_import_lines(code: str) -> str:
    return re.sub(r"(?m)^\s*import\s+[A-Za-z_][A-Za-z0-9_'.]*(?:\.[A-Za-z_][A-Za-z0-9_']*)*\s*$", "", code)


def _read_entry_code(config: LeaConfig, entry: Any) -> str:
    try:
        path = _workspace_path(config, entry.proof_path)
    except Exception:
        return ""
    try:
        return path.read_text() if path.exists() else ""
    except OSError:
        return ""


def _normalize_rel_path(path: str | None) -> str | None:
    if not path:
        return None
    return str(Path(path))
