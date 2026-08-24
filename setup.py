from __future__ import annotations

import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithCommit(build_py):
    def run(self) -> None:
        super().run()
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).parent,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            sha = "source-archive"
        target = Path(self.build_lib) / "tradehub_research" / "_commit.py"
        target.write_text(f"__commit_sha__ = {sha!r}\n", encoding="utf-8")


setup(cmdclass={"build_py": BuildPyWithCommit})
