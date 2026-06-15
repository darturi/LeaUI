from app.config import LeaConfig
from app.project_usage import detect_project_formalization_dependents, detect_used_project_formalizations


def test_detects_imported_and_referenced_project_formalization(tmp_path):
    lea_root = _write_project(tmp_path)
    code = (
        "import Mathlib\n"
        "import Lea.Epsilon.helper\n\n"
        "namespace Lea.Epsilon\n\n"
        "theorem target : True := by\n"
        "  exact helper\n\n"
        "end Lea.Epsilon\n"
    )

    used = detect_used_project_formalizations(
        project=_project(),
        config=_config(lea_root),
        code=code,
        proof_path="workspace/proofs/Lea/Epsilon/target.lean",
    )

    assert used == [
        {
            "name": "helper",
            "proof_path": "workspace/proofs/Lea/Epsilon/helper.lean",
            "module_name": "Lea.Epsilon.helper",
            "project_id": "project-id",
            "project_slug": "epsilon",
            "project_title": "Epsilon",
            "project_path": "workspace/projects/epsilon.md",
        }
    ]


def test_import_only_does_not_count_as_used(tmp_path):
    lea_root = _write_project(tmp_path)
    code = (
        "import Mathlib\n"
        "import Lea.Epsilon.helper\n\n"
        "namespace Lea.Epsilon\n\n"
        "theorem target : True := by\n"
        "  trivial\n\n"
        "end Lea.Epsilon\n"
    )

    used = detect_used_project_formalizations(
        project=_project(),
        config=_config(lea_root),
        code=code,
        proof_path="workspace/proofs/Lea/Epsilon/target.lean",
    )

    assert used == []


def test_reference_without_import_does_not_count_as_used(tmp_path):
    lea_root = _write_project(tmp_path)
    code = (
        "import Mathlib\n\n"
        "namespace Lea.Epsilon\n\n"
        "theorem target : True := by\n"
        "  exact helper\n\n"
        "end Lea.Epsilon\n"
    )

    used = detect_used_project_formalizations(
        project=_project(),
        config=_config(lea_root),
        code=code,
        proof_path="workspace/proofs/Lea/Epsilon/target.lean",
    )

    assert used == []


def test_current_project_entry_is_ignored(tmp_path):
    lea_root = _write_project(tmp_path, names=("helper", "target"))
    code = (
        "import Mathlib\n"
        "import Lea.Epsilon.target\n\n"
        "namespace Lea.Epsilon\n\n"
        "theorem target : True := by\n"
        "  exact target\n\n"
        "end Lea.Epsilon\n"
    )

    used = detect_used_project_formalizations(
        project=_project(),
        config=_config(lea_root),
        code=code,
        proof_path="workspace/proofs/Lea/Epsilon/target.lean",
    )

    assert used == []


def test_detects_dependent_project_formalization(tmp_path):
    lea_root = _write_project(
        tmp_path,
        names=("helper", "target"),
        proofs={
            "target": (
                "import Mathlib\n"
                "import Lea.Epsilon.helper\n\n"
                "namespace Lea.Epsilon\n\n"
                "theorem target : True := by\n"
                "  exact helper\n\n"
                "end Lea.Epsilon\n"
            )
        },
    )

    dependents = detect_project_formalization_dependents(
        project=_project(),
        config=_config(lea_root),
        proof_path="workspace/proofs/Lea/Epsilon/helper.lean",
    )

    assert dependents == [
        {
            "name": "target",
            "proof_path": "workspace/proofs/Lea/Epsilon/target.lean",
            "module_name": "Lea.Epsilon.target",
            "project_id": "project-id",
            "project_slug": "epsilon",
            "project_title": "Epsilon",
            "project_path": "workspace/projects/epsilon.md",
        }
    ]


def test_dependent_import_only_does_not_count(tmp_path):
    lea_root = _write_project(
        tmp_path,
        names=("helper", "target"),
        proofs={
            "target": (
                "import Mathlib\n"
                "import Lea.Epsilon.helper\n\n"
                "namespace Lea.Epsilon\n\n"
                "theorem target : True := by\n"
                "  trivial\n\n"
                "end Lea.Epsilon\n"
            )
        },
    )

    dependents = detect_project_formalization_dependents(
        project=_project(),
        config=_config(lea_root),
        proof_path="workspace/proofs/Lea/Epsilon/helper.lean",
    )

    assert dependents == []


def test_dependent_reference_without_import_does_not_count(tmp_path):
    lea_root = _write_project(
        tmp_path,
        names=("helper", "target"),
        proofs={
            "target": (
                "import Mathlib\n\n"
                "namespace Lea.Epsilon\n\n"
                "theorem target : True := by\n"
                "  exact helper\n\n"
                "end Lea.Epsilon\n"
            )
        },
    )

    dependents = detect_project_formalization_dependents(
        project=_project(),
        config=_config(lea_root),
        proof_path="workspace/proofs/Lea/Epsilon/helper.lean",
    )

    assert dependents == []


def test_dependent_detection_ignores_missing_proof_file(tmp_path):
    lea_root = _write_project(tmp_path, names=("helper", "target"))

    dependents = detect_project_formalization_dependents(
        project=_project(),
        config=_config(lea_root),
        proof_path="workspace/proofs/Lea/Epsilon/helper.lean",
    )

    assert dependents == []


def _project():
    return {
        "id": "project-id",
        "slug": "epsilon",
        "title": "Epsilon",
        "path": "workspace/projects/epsilon.md",
    }


def _config(lea_root):
    return LeaConfig(
        model="o4-mini",
        max_turns=2,
        lea_api_base_url="http://127.0.0.1:8000",
        lea_root=lea_root,
    )


def _write_project(tmp_path, names=("helper",), proofs=None):
    proofs = proofs or {}
    lea_root = tmp_path / "lea"
    project_dir = lea_root / "workspace" / "projects"
    proof_dir = lea_root / "workspace" / "proofs" / "Lea" / "Epsilon"
    project_dir.mkdir(parents=True)
    proof_dir.mkdir(parents=True)
    entries = ['# Project epsilon\n\n<!-- lea:project id="epsilon" -->\n']
    for name in names:
        if name in proofs:
            (proof_dir / f"{name}.lean").write_text(proofs[name])
        entries.append(
            "\n".join(
                [
                    f"## Theorem: {name}",
                    "",
                    f'<!-- lea:theorem name="{name}" proof="workspace/proofs/Lea/Epsilon/{name}.lean" module="Lea.Epsilon.{name}" -->',
                    "",
                    "### Signature",
                    "",
                    "```lean",
                    f"theorem {name} : True := by",
                    "```",
                    "",
                ]
            )
        )
    (project_dir / "epsilon.md").write_text("\n".join(entries))
    return lea_root
