from datetime import datetime, timezone

import pytest

from app import get_db
from llm_profiles import LlmProfileStore
from llm_provider import LlmConfigurationError


NOW = datetime(2026, 9, 20, 9, 30, tzinfo=timezone.utc)


def profile_values(name="Local", **overrides):
    values = {
        "name": name,
        "base_url": "http://localhost:1234/v1/chat/completions",
        "model": "qwen-local",
        "api_key_env": None,
        "timeout_seconds": 45,
        "supports_tools": True,
    }
    values.update(overrides)
    return values


def test_first_profile_becomes_default_and_normalizes_url(app):
    with app.app_context():
        store = LlmProfileStore(get_db(), environ={})

        profile = store.create(profile_values(), now=NOW)
        get_db().commit()

        assert profile.is_default is True
        assert profile.base_url == "http://localhost:1234/v1"
        assert store.get().id == profile.id


def test_switches_default_without_allowing_zero_defaults(app):
    with app.app_context():
        store = LlmProfileStore(get_db(), environ={})
        local = store.create(profile_values(), now=NOW)
        remote = store.create(
            profile_values(
                "Remote", base_url="https://llm.example.test/v1", model="remote"
            ),
            now=NOW,
        )

        updated = store.update(remote.id, {}, make_default=True, now=NOW)

        assert updated.is_default is True
        assert store.get(local.id).is_default is False
        with pytest.raises(ValueError, match="Choose another default"):
            store.update(remote.id, {}, make_default=False, now=NOW)


def test_resolves_secret_only_when_building_provider_config(app):
    with app.app_context():
        store = LlmProfileStore(get_db(), environ={"REMOTE_LLM_KEY": "secret-value"})
        profile = store.create(
            profile_values(
                "Remote",
                base_url="https://llm.example.test/v1",
                api_key_env="REMOTE_LLM_KEY",
            ),
            now=NOW,
        )

        public = profile.public_dict(store.environ)
        config = store.provider_config(profile.id)

        assert public["api_key_configured"] is True
        assert "secret-value" not in repr(public)
        assert config.api_key == "secret-value"
        columns = {
            row[1]
            for row in get_db().execute("PRAGMA table_info(llm_profiles)").fetchall()
        }
        assert "api_key" not in columns


def test_missing_secret_environment_variable_is_actionable(app):
    with app.app_context():
        store = LlmProfileStore(get_db(), environ={})
        profile = store.create(
            profile_values(api_key_env="MISSING_LLM_KEY"), now=NOW
        )

        with pytest.raises(LlmConfigurationError, match="MISSING_LLM_KEY"):
            store.provider_config(profile.id)


def test_profile_updates_are_validated_before_write(app):
    with app.app_context():
        store = LlmProfileStore(get_db(), environ={})
        profile = store.create(profile_values(), now=NOW)

        with pytest.raises(ValueError, match="Unsupported field"):
            store.update(profile.id, {"api_key": "must-not-be-stored"}, now=NOW)
        with pytest.raises(ValueError, match="environment variable"):
            store.update(profile.id, {"api_key_env": "bad name"}, now=NOW)

        assert store.get(profile.id).api_key_env is None
