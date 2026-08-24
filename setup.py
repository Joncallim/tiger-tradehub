from __future__ import annotations

import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

COMMIT_FILE = Path("tradehub_research") / "_commit.py"


def git_commit_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def commit_source(sha: str) -> str:
    return f"__commit_sha__ = {sha!r}\n"


class SdistWithCommit(sdist):
    def run(self) -> None:
        path = Path(__file__).parent / COMMIT_FILE
        previous = path.read_bytes() if path.exists() else None
        sha = git_commit_sha()
        if sha is not None:
            path.write_text(commit_source(sha), encoding="utf-8")
        try:
            super().run()
        finally:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)


class BuildPyWithCommit(build_py):
    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "tradehub_research" / "_commit.py"
        sha = git_commit_sha()
        if sha is not None:
            target.write_text(commit_source(sha), encoding="utf-8")
        elif not target.exists():
            target.write_text(commit_source("source-archive"), encoding="utf-8")


setup(cmdclass={"build_py": BuildPyWithCommit, "sdist": SdistWithCommit})
