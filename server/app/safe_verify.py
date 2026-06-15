"""SafeVerify integration — kernel-level audit of a finished proof.

Plain `lean_check` (and the API's `/v1/verify`) only confirm a file *compiles*.
SafeVerify additionally replays the proof through the Lean kernel and checks it
depends only on the three standard axioms — so it catches `sorry`, `axiom`/
`opaque` smuggling, `native_decide`, namespace/notation shadowing, and
`partial`/`unsafe` tricks that a plain compile lets through.

This wraps the prover's `eval/utils/verify.py:verify_proof`, which is
comparison-style: it checks a *submission* file against a *target* signature.
We derive the target from the proof's own main theorem (header + `:= by sorry`),
so the check is universal (works regardless of permission tier). Catching a
tampered *statement* additionally needs a trusted target (the approved
theorem-translation skeleton) — a planned follow-up for `theorem_translation`
runs.

The check is expensive (~100s: two `lake env lean` compiles + a kernel replay),
so it is only ever run once, on the final artifact, off the proof hot path.

The SafeVerify binary must be pre-built (`lake build safe_verify` in
`third_party/SafeVerify`, pinned to the workspace's Lean toolchain). If it is
absent — e.g. a Docker image built without it — `is_available` returns False and
callers degrade gracefully instead of failing the run.
"""

from __future__ import annotations

import importlib
import re
import sys
import threading
from pathlib import Path

from .config import ROOT, LeaConfig

# SafeVerify shells out to `lake` for two full Mathlib compiles; only let one run
# at a time so concurrent requests don't thrash the toolchain.
_sv_lock = threading.Lock()

# A top-level `theorem`/`lemma` signature: from the keyword up to the proof body
# `:=`. Theorem types don't contain `:=`, so the first one is the body delimiter.
_THEOREM_RE = re.compile(
    r"(?ms)^\s*(?:@\[[^\]]*\]\s*)?(?:theorem|lemma)\s+[A-Za-z_][\w']*.*?:=",
)


def _safe_verify_binary(config: LeaConfig) -> Path | None:
    if config.lea_root is None:
        return None
    binary = (
        config.lea_root
        / "third_party"
        / "SafeVerify"
        / ".lake"
        / "build"
        / "bin"
        / "safe_verify"
    )
    return binary if binary.exists() else None


def is_available(config: LeaConfig) -> bool:
    """True iff the SafeVerify binary is built and the workspace is resolvable."""
    return _safe_verify_binary(config) is not None


def _load_verify_module(config: LeaConfig):
    import_root = config.lea_root if (config.lea_root / "lea").exists() else ROOT / "external" / "lea-prover"
    root = str(import_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("eval.utils.verify")


def _theorem_signature(code: str) -> str | None:
    """The proof's main theorem signature (last top-level theorem/lemma), with
    the proof body stripped — i.e. everything up to but excluding `:=`."""
    matches = list(_THEOREM_RE.finditer(code))
    if not matches:
        return None
    decl = matches[-1].group(0)
    return decl[: decl.rfind(":=")].rstrip()


def try_acquire() -> bool:
    """Non-blocking lock so a duplicate request returns 'running' rather than
    queuing a second ~100s check. Caller must `release()` when done."""
    return _sv_lock.acquire(blocking=False)


def release() -> None:
    if _sv_lock.locked():
        _sv_lock.release()


def verify(config: LeaConfig, proof_code: str, proof_filename: str) -> dict:
    """Run SafeVerify on a finished proof.

    Returns ``{"status": ..., "detail": ...}`` where status is one of
    ``passed`` / ``failed`` / ``error`` / ``unavailable``. Never raises — any
    failure is reported as an ``error`` verdict so it can be surfaced, not
    swallowed.
    """
    if config.lea_root is None or _safe_verify_binary(config) is None:
        return {"status": "unavailable", "detail": "SafeVerify is not built on this server."}

    signature = _theorem_signature(proof_code)
    if not signature:
        return {"status": "error", "detail": "Could not find a theorem/lemma to verify in the proof."}

    workspace = (config.lea_root / "workspace").resolve()
    scratch = workspace / ".sv_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    stem = Path(proof_filename).stem or "proof"
    target = scratch / f"{stem}_sv_target.lean"
    submission = scratch / f"{stem}_sv_submission.lean"
    target.write_text("import Mathlib\n\n" + signature + " := by\n  sorry\n")
    submission.write_text(proof_code if proof_code.endswith("\n") else proof_code + "\n")

    try:
        verify_mod = _load_verify_module(config)
        ok, detail = verify_mod.verify_proof(target, submission, workspace)
        return {"status": "passed" if ok else "failed", "detail": detail}
    except Exception as exc:  # noqa: BLE001 — report any failure as an error verdict
        return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        target.unlink(missing_ok=True)
        submission.unlink(missing_ok=True)
