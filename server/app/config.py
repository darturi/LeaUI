from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]

# Legacy flat TOML key -> the LiteLLM env var it maps to. Kept for back-compat;
# any other provider key is stored under its uppercase env var name directly.
LEGACY_KEY_ENV = {
    "google_api_key": "GOOGLE_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
}
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*_API_KEY")


@dataclass(frozen=True)
class LeaConfig:
    model: str
    max_turns: int | None
    lea_api_base_url: str
    max_spend_usd: float | None = None
    lea_api_key: str | None = None
    lea_root: Path | None = None
    lea_job_timeout_seconds: int = 900
    google_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    # Provider API keys keyed by LiteLLM env var name (GOOGLE_API_KEY,
    # MISTRAL_API_KEY, …). Unifies the legacy three flat keys with any
    # additional providers so the prover env can be populated generically.
    api_keys: dict[str, str] = field(default_factory=dict)
    narrate_tool_steps: bool = False
    permission_tier: str = "none"
    theorem_translation_max_retries: int = 3


def load_config(path: Path | None = None) -> LeaConfig:
    config_path = path or ROOT / "config" / "lea.local.toml"
    data: dict = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text())

    lea_root = data.get("lea_root", "external/lea-prover")
    resolved_lea_root = None
    if lea_root:
        resolved_lea_root = Path(lea_root)
        if not resolved_lea_root.is_absolute():
            resolved_lea_root = ROOT / resolved_lea_root

    max_turns = data.get("max_turns")
    timeout = data.get("lea_job_timeout_seconds", 900)
    narrate_tool_steps = data.get("narrate_tool_steps", False)
    if not isinstance(narrate_tool_steps, bool):
        raise ValueError("narrate_tool_steps must be a boolean")
    permission_tier = data.get("permission_tier", "none")
    if not isinstance(permission_tier, str):
        raise ValueError("permission_tier must be a string")
    if permission_tier not in {"none", "theorem_translation", "stepwise"}:
        raise ValueError("permission_tier must be one of: none, theorem_translation, stepwise")
    theorem_translation_max_retries = data.get("theorem_translation_max_retries", 3)
    if (
        not isinstance(theorem_translation_max_retries, int)
        or isinstance(theorem_translation_max_retries, bool)
    ):
        raise ValueError("theorem_translation_max_retries must be an integer")
    if theorem_translation_max_retries < 1:
        raise ValueError("theorem_translation_max_retries must be at least 1")
    max_spend_usd = data.get("max_spend_usd")
    if max_spend_usd is not None:
        max_spend_usd = float(max_spend_usd)
        if max_spend_usd < 0:
            raise ValueError("max_spend_usd must be greater than or equal to 0")

    # Build the unified env-var-keyed key map: legacy flat keys first, then any
    # uppercase ENV_VAR_NAME root entries (additional providers).
    api_keys: dict[str, str] = {}
    for flat_key, env_name in LEGACY_KEY_ENV.items():
        if data.get(flat_key):
            api_keys[env_name] = str(data[flat_key])
    for key, value in data.items():
        if isinstance(key, str) and _ENV_KEY_RE.fullmatch(key) and value:
            api_keys[key] = str(value)

    return LeaConfig(
        model=data.get("model", "gemini/gemini-3.1-pro-preview"),
        max_turns=int(max_turns) if max_turns is not None else None,
        max_spend_usd=max_spend_usd,
        lea_api_base_url=_normalize_base_url(data.get("lea_api_base_url", "http://127.0.0.1:8000")),
        lea_api_key=data.get("lea_api_key"),
        lea_root=resolved_lea_root,
        lea_job_timeout_seconds=int(timeout),
        google_api_key=data.get("google_api_key"),
        anthropic_api_key=data.get("anthropic_api_key"),
        openai_api_key=data.get("openai_api_key"),
        openai_base_url=data.get("openai_base_url"),
        api_keys=api_keys,
        narrate_tool_steps=narrate_tool_steps,
        permission_tier=permission_tier,
        theorem_translation_max_retries=theorem_translation_max_retries,
    )


def _normalize_base_url(value: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("lea_api_base_url must be an absolute http(s) URL")
    return str(value).strip().rstrip("/")
