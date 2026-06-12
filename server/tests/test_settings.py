import json
from io import BytesIO
import urllib.error

from app import db
from app import settings as settings_service


def test_settings_payload_masks_api_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        max_spend_usd = 20.0
        permission_tier = "theorem_translation"
        theorem_translation_max_retries = 4
        openai_api_key = "sk-secret-1234"
        """
    )

    payload = settings_service.settings_payload(config_path)

    assert payload["model"] == "gpt-4o"
    assert payload["max_turns"] == 12
    assert payload["max_spend_usd"] == 20.0
    assert payload["theorem_translation_max_retries"] == 4
    assert payload["api_keys"]["OPENAI_API_KEY"] == {"configured": True, "last4": "1234", "label": "OpenAI"}
    assert payload["api_keys"]["ANTHROPIC_API_KEY"] == {"configured": False, "last4": None, "label": "Anthropic"}


def test_update_settings_preserves_unrelated_config_and_updates_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", lambda family, value, config, model=None: None)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        lea_api_base_url = "http://127.0.0.1:8000"
        lea_root = "external/lea-prover"
        model = "gpt-4o"
        max_turns = 12
        permission_tier = "theorem_translation"
        theorem_translation_max_retries = 3
        openai_api_key = "old-secret"
        """
    )

    payload = settings_service.update_settings(
        {
            "model": "claude-sonnet-4-6",
            "max_turns": 30,
            "max_spend_usd": 9.5,
            "permission_tier": "stepwise",
            "theorem_translation_max_retries": 7,
            "api_keys": {
                "OPENAI_API_KEY": {"clear": True},
                "ANTHROPIC_API_KEY": {"value": "sk-ant-secret123456"},
            },
        },
        config_path,
    )
    text = config_path.read_text()

    assert payload["permission_tier"] == "stepwise"
    assert payload["theorem_translation_max_retries"] == 7
    assert 'lea_root = "external/lea-prover"' in text
    assert 'model = "claude-sonnet-4-6"' in text
    assert "max_turns = 30" in text
    assert "max_spend_usd = 9.5" in text
    assert 'permission_tier = "stepwise"' in text
    assert "theorem_translation_max_retries = 7" in text
    assert "openai_api_key" not in text
    assert 'anthropic_api_key = "sk-ant-secret123456"' in text


def test_update_settings_rejects_invalid_theorem_translation_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text("")

    try:
        settings_service.update_settings({"theorem_translation_max_retries": 0}, config_path)
    except ValueError as exc:
        assert str(exc) == "theorem_translation_max_retries must be at least 1"
    else:
        raise AssertionError("Expected ValueError")


def test_update_settings_rejects_selected_model_without_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    # The required-key name comes from LiteLLM (env-dependent); make the test
    # deterministic regardless of the developer's shell.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    try:
        settings_service.update_settings({"model": "claude-sonnet-4-6"}, config_path)
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "An API key (Anthropic) is required before saving this model."
        assert exc.field == "api_keys.ANTHROPIC_API_KEY"
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_update_settings_rejects_malformed_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    try:
        settings_service.update_settings(
            {
                "model": "gpt-4o",
                "api_keys": {"OPENAI_API_KEY": {"value": "not-a-real-key"}},
            },
            config_path,
        )
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "The OpenAI API key does not look valid. Check the key and try again."
        assert exc.field == "api_keys.openai"
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_update_settings_allows_anthropic_prefix_then_defers_to_provider_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    seen = {}

    def verify_key(family, value, config, model=None):
        seen["family"] = family
        seen["value"] = value
        seen["model"] = model

    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", verify_key)
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    settings_service.update_settings(
        {
            "model": "claude-sonnet-4-6",
            "api_keys": {"ANTHROPIC_API_KEY": {"value": "sk-ant-api03-test.value/with+chars"}},
        },
        config_path,
    )

    assert seen == {
        "family": "anthropic",
        "value": "sk-ant-api03-test.value/with+chars",
        "model": "claude-sonnet-4-6",
    }
    assert 'anthropic_api_key = "sk-ant-api03-test.value/with+chars"' in config_path.read_text()


def test_anthropic_verification_uses_messages_endpoint_with_selected_model(tmp_path):
    request = settings_service._provider_verification_request(
        "anthropic",
        "sk-ant-api03-test",
        settings_service.load_config(tmp_path / "missing.toml"),
        "claude-sonnet-4-6",
    )

    assert request is not None
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_method() == "POST"
    assert request.headers["X-api-key"] == "sk-ant-api03-test"
    assert request.headers["Anthropic-version"] == "2023-06-01"
    assert request.headers["Content-type"] == "application/json"
    body = json.loads(request.data.decode("utf-8"))
    assert body == {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }


def test_anthropic_model_error_is_reported_as_model_error(monkeypatch):
    body = json.dumps(
        {
            "error": {
                "type": "invalid_request_error",
                "message": "model: claude-missing does not exist",
            }
        }
    ).encode("utf-8")

    def fail_with_model_error(request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            BytesIO(body),
        )

    monkeypatch.setattr(settings_service.urllib.request, "urlopen", fail_with_model_error)

    try:
        settings_service._verify_api_key_credentials(
            "anthropic",
            "sk-ant-api03-test",
            settings_service.load_config(),
            "claude-missing",
        )
    except settings_service.SettingsValidationError as exc:
        assert exc.field == "model"
        assert "Anthropic rejected the selected model" in str(exc)
        assert "claude-missing" in str(exc)
    else:
        raise AssertionError("Expected SettingsValidationError")


def test_update_settings_rejects_provider_rejected_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite3")
    db.init_db()
    config_path = tmp_path / "lea.local.toml"
    config_path.write_text(
        """
        model = "gpt-4o"
        max_turns = 12
        """
    )

    def reject_key(family, value, config, model=None):
        raise settings_service.SettingsValidationError(
            "The OpenAI API key was rejected by the provider.",
            "api_keys.openai",
        )

    monkeypatch.setattr(settings_service, "_verify_api_key_credentials", reject_key)

    try:
        settings_service.update_settings(
            {
                "model": "gpt-4o",
                "api_keys": {"OPENAI_API_KEY": {"value": "sk-validlooking123456"}},
            },
            config_path,
        )
    except settings_service.SettingsValidationError as exc:
        assert str(exc) == "The OpenAI API key was rejected by the provider."
        assert exc.field == "api_keys.openai"
    else:
        raise AssertionError("Expected SettingsValidationError")
