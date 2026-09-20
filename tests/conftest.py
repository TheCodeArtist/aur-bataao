from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app import create_app


@pytest.fixture()
def app_factory(tmp_path: Path):
    """Create fully isolated application instances with explicit configuration."""

    sequence = 0

    def factory(
        overrides: Mapping[str, Any] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ):
        nonlocal sequence
        sequence += 1
        config = {
            "TESTING": True,
            "DATABASE": str(tmp_path / f"test-{sequence}.sqlite3"),
            "USER_TIMEZONE": "Asia/Kolkata",
            "LLM_MODEL": "",
        }
        if overrides:
            config.update(overrides)
        return create_app(config, environ={} if environ is None else environ)

    return factory


@pytest.fixture()
def app(app_factory):
    return app_factory()


@pytest.fixture()
def client(app):
    return app.test_client()
