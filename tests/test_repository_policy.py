from __future__ import annotations

import json
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_python_test_policy_cannot_drift_silently():
    config = tomllib.loads(read_text("pyproject.toml"))
    pytest_config = config["tool"]["pytest"]["ini_options"]
    coverage_run = config["tool"]["coverage"]["run"]
    coverage_report = config["tool"]["coverage"]["report"]

    assert {"--strict-config", "--strict-markers"}.issubset(
        pytest_config["addopts"]
    )
    assert pytest_config["xfail_strict"] is True
    assert pytest_config["filterwarnings"] == ["error"]
    assert coverage_run["branch"] is True
    assert coverage_run["source"] == ["."]
    assert coverage_report["fail_under"] == 100
    assert coverage_report["show_missing"] is True


def test_javascript_test_policy_cannot_drift_silently():
    package = json.loads(read_text("package.json"))
    scripts = package["scripts"]
    unit_runner = read_text("scripts/run-js-unit-tests.mjs")

    assert scripts["test"] == "npm run test:unit && npm run test:e2e:coverage"
    assert scripts["test:unit"] == "node scripts/run-js-unit-tests.mjs"
    assert scripts["test:e2e:coverage"] == "playwright test"
    for threshold in ("lines", "branches", "functions"):
        assert f"--test-coverage-{threshold}=100" in unit_runner
    for source in (
        "static/markdown.js",
        "static/http.js",
        "static/task_logic.js",
        "static/agent_logic.js",
        "tests-e2e/browser_coverage.js",
        "tests-e2e/browser_coverage_reporter.js",
        "tests-e2e/unit_coverage_gate.js",
    ):
        assert source in unit_runner

    assert "coverageGateError" in unit_runner

    browser_config = read_text("playwright.config.js")
    browser_reporter = read_text("tests-e2e/browser_coverage_reporter.js")
    assert 'process.env.npm_lifecycle_event === "test:e2e:coverage"' in browser_config
    assert '"./tests-e2e/browser_coverage_reporter.js"' in browser_config
    assert "retries: process.env.CI ? 1 : 0" in browser_config
    assert "assert = assertBrowserCoverage" in browser_reporter
    assert "this.assert(summaries)" in browser_reporter
    assert 'result.status !== "passed"' in browser_reporter


def test_local_hooks_keep_fast_and_authoritative_gates():
    pre_commit = read_text(".githooks/pre-commit")
    pre_push = read_text(".githooks/pre-push")

    assert '"$python_command" .githooks/validate-staged.py' in pre_commit
    assert '"$python_command" -m pytest' in pre_commit
    assert "npm run test:unit" in pre_commit
    assert '"$python_command" -m pytest --cov --cov-report=term-missing' in pre_push
    assert "npm test" in pre_push


def test_ci_repeats_authoritative_gates_on_both_platforms():
    workflow = read_text(".github/workflows/tests.yml")

    for expected in (
        "push:",
        "pull_request:",
        "windows-latest",
        "ubuntu-latest",
        "python -m pytest --cov --cov-report=term-missing --cov-report=xml",
        "python -m pytest",
        "npm test",
        "actions/upload-artifact@v4",
        "playwright-report/",
        "test-results/playwright/",
    ):
        assert expected in workflow


def test_pull_requests_prompt_assertion_quality_review():
    template = read_text(".github/pull_request_template.md")

    for expected in (
        "failed for the expected reason",
        "meaningful postconditions",
        "rollback/cleanup",
        "complete local gate",
        "No coverage threshold or gate was weakened",
        "Platform-specific behavior",
    ):
        assert expected in template
