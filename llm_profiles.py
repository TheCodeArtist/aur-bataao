from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from llm_provider import LlmConfig, LlmConfigurationError, normalize_base_url


ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROFILE_FIELDS = {
    "name",
    "base_url",
    "model",
    "api_key_env",
    "timeout_seconds",
    "supports_tools",
}


class LlmProfileNotFoundError(LookupError):
    """Raised when a configured LLM profile does not exist."""


@dataclass(frozen=True)
class LlmProfile:
    id: int
    name: str
    base_url: str
    model: str
    api_key_env: str | None
    timeout_seconds: float
    supports_tools: bool
    is_default: bool
    created_at: str
    updated_at: str

    def public_dict(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        environment = os.environ if environ is None else environ
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_configured": bool(
                self.api_key_env and environment.get(self.api_key_env)
            ),
            "timeout_seconds": self.timeout_seconds,
            "supports_tools": self.supports_tools,
            "is_default": self.is_default,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LlmProfileStore:
    """Persists endpoint settings while keeping credentials out of SQLite."""

    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.db = db
        self.environ = os.environ if environ is None else environ

    def list(self) -> list[LlmProfile]:
        rows = self.db.execute(
            "SELECT * FROM llm_profiles ORDER BY is_default DESC, name COLLATE NOCASE, id"
        ).fetchall()
        return [_profile(row) for row in rows]

    def get(self, profile_id: int | None = None) -> LlmProfile:
        if profile_id is None:
            row = self.db.execute(
                "SELECT * FROM llm_profiles WHERE is_default = 1"
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM llm_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            target = "default LLM profile" if profile_id is None else "LLM profile"
            raise LlmProfileNotFoundError(f"{target} not found")
        return _profile(row)

    def create(
        self,
        values: Mapping[str, Any],
        *,
        make_default: bool = False,
        now: datetime | None = None,
    ) -> LlmProfile:
        normalized = _validated_values(values)
        timestamp = _iso_utc(now)
        if make_default or not self.list():
            self.db.execute("UPDATE llm_profiles SET is_default = 0 WHERE is_default = 1")
            make_default = True
        try:
            cursor = self.db.execute(
                """
                INSERT INTO llm_profiles (
                    name, base_url, model, api_key_env, timeout_seconds,
                    supports_tools, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized["name"],
                    normalized["base_url"],
                    normalized["model"],
                    normalized["api_key_env"],
                    normalized["timeout_seconds"],
                    int(normalized["supports_tools"]),
                    int(make_default),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "llm_profiles.name" in str(exc):
                raise ValueError("Profile name is already in use") from exc
            raise
        return self.get(int(cursor.lastrowid))

    def update(
        self,
        profile_id: int,
        values: Mapping[str, Any],
        *,
        make_default: bool | None = None,
        now: datetime | None = None,
    ) -> LlmProfile:
        current = self.get(profile_id)
        unknown = set(values) - PROFILE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")
        merged = {
            field: values.get(field, getattr(current, field)) for field in PROFILE_FIELDS
        }
        normalized = _validated_values(merged)
        timestamp = _iso_utc(now)
        if make_default is True:
            self.db.execute(
                "UPDATE llm_profiles SET is_default = 0 WHERE is_default = 1 AND id <> ?",
                (profile_id,),
            )
        default_value = current.is_default if make_default is None else make_default
        if current.is_default and default_value is False:
            raise ValueError("Choose another default profile before unsetting this one")
        try:
            self.db.execute(
                """
                UPDATE llm_profiles
                SET name = ?, base_url = ?, model = ?, api_key_env = ?,
                    timeout_seconds = ?, supports_tools = ?, is_default = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized["name"],
                    normalized["base_url"],
                    normalized["model"],
                    normalized["api_key_env"],
                    normalized["timeout_seconds"],
                    int(normalized["supports_tools"]),
                    int(default_value),
                    timestamp,
                    profile_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "llm_profiles.name" in str(exc):
                raise ValueError("Profile name is already in use") from exc
            raise
        return self.get(profile_id)

    def provider_config(self, profile_id: int | None = None) -> LlmConfig:
        profile = self.get(profile_id)
        api_key = None
        if profile.api_key_env:
            api_key = self.environ.get(profile.api_key_env)
            if not api_key:
                raise LlmConfigurationError(
                    f"Environment variable {profile.api_key_env} is not set"
                )
        return LlmConfig(
            base_url=profile.base_url,
            model=profile.model,
            api_key=api_key,
            timeout_seconds=profile.timeout_seconds,
            supports_tools=profile.supports_tools,
        )


def _validated_values(values: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(values) - PROFILE_FIELDS
    if unknown:
        raise ValueError(f"Unsupported field: {sorted(unknown)[0]}")

    name = values.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("Profile name must be between 1 and 100 characters")
    model = values.get("model")
    if not isinstance(model, str) or not 1 <= len(model.strip()) <= 200:
        raise ValueError("Model must be between 1 and 200 characters")
    api_key_env = values.get("api_key_env") or None
    if api_key_env is not None:
        if not isinstance(api_key_env, str) or not ENVIRONMENT_NAME.fullmatch(
            api_key_env.strip()
        ):
            raise ValueError("API key environment variable name is invalid")
        api_key_env = api_key_env.strip()
    timeout = values.get("timeout_seconds", 60)
    supports_tools = values.get("supports_tools", True)
    if not isinstance(supports_tools, bool):
        raise ValueError("supports_tools must be a boolean")

    config = LlmConfig(
        base_url=values.get("base_url", ""),
        model=model,
        timeout_seconds=timeout,
        supports_tools=supports_tools,
    )
    return {
        "name": name.strip(),
        "base_url": normalize_base_url(config.base_url),
        "model": config.model,
        "api_key_env": api_key_env,
        "timeout_seconds": config.timeout_seconds,
        "supports_tools": supports_tools,
    }


def _profile(row: sqlite3.Row) -> LlmProfile:
    return LlmProfile(
        id=int(row["id"]),
        name=row["name"],
        base_url=row["base_url"],
        model=row["model"],
        api_key_env=row["api_key_env"],
        timeout_seconds=float(row["timeout_seconds"]),
        supports_tools=bool(row["supports_tools"]),
        is_default=bool(row["is_default"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _iso_utc(value: datetime | None) -> str:
    instant = value or datetime.now(timezone.utc)
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds")
