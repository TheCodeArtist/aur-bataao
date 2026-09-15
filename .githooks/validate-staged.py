"""Validate content that is staged in Git, rather than working-tree files."""

from __future__ import annotations

import os
from pathlib import PurePosixPath
import subprocess
import sys


MAX_FILE_SIZE = 100 * 1024
FORBIDDEN_DIRECTORIES = {
    ".pytest_cache",
    ".pytest-tmp",
    ".venv",
    "__pycache__",
    "instance",
}


def run_git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def staged_paths() -> list[str]:
    result = run_git(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def is_forbidden(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        any(part in FORBIDDEN_DIRECTORIES for part in parts)
        or any(part.endswith(".egg-info") for part in parts)
        or path.endswith(".pyc")
    )


def main() -> int:
    failed = False

    whitespace = run_git("diff", "--cached", "--check", check=False)
    if whitespace.returncode:
        sys.stderr.buffer.write(whitespace.stdout)
        sys.stderr.buffer.write(whitespace.stderr)
        failed = True

    for path in staged_paths():
        if is_forbidden(path):
            print(f"Forbidden generated or local file is staged: {path}", file=sys.stderr)
            failed = True
            continue

        content = run_git("show", f":{path}").stdout
        if len(content) > MAX_FILE_SIZE:
            print(
                f"Staged file exceeds 100 KiB ({len(content)} bytes): {path}",
                file=sys.stderr,
            )
            failed = True

        if path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except (SyntaxError, ValueError) as error:
                print(f"Invalid Python syntax in staged file {path}: {error}", file=sys.stderr)
                failed = True

    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
