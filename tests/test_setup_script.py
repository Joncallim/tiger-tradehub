import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "setup.sh"


def test_setup_script_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(SETUP_SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_setup_script_help_is_available():
    result = subprocess.run(
        [str(SETUP_SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "./setup.sh [--no-run]" in result.stdout
    assert "local setup UI" in result.stdout
