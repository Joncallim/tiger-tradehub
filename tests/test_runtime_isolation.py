from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy" / "systemd"


def unit_values(name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in (UNITS / name).read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key, []).append(value)
    return values


def test_research_unit_is_a_distinct_unprivileged_runtime():
    research = unit_values("tradehub-research.service")
    execution = unit_values("tradehub-execution.service")
    assert execution["WorkingDirectory"] == ["/opt/tiger-tradehub"]
    assert research["WorkingDirectory"] == ["/opt/tiger-tradehub"]
    assert "/home" not in execution["WorkingDirectory"][0]
    assert "/home" not in research["WorkingDirectory"][0]
    assert research["User"] == ["tradehub-research"]
    assert research["Group"] == ["tradehub-research"]
    assert execution["User"] == ["tradehub-execution"]
    assert research["User"] != execution["User"]
    assert research["EnvironmentFile"] != execution["EnvironmentFile"]
    assert research["ProtectHome"] == ["true"]
    inaccessible = {path.lstrip("-") for path in research["InaccessiblePaths"][0].split()}
    assert "/etc/tradehub/execution.env" in inaccessible
    assert "/etc/tradehub/tiger_private_key.pk8" in inaccessible
    assert "/home/jon/tiger-tradehub/.env" in inaccessible
    assert "/home/jon/tiger-tradehub/data/tiger_private_key.pk8.pem" in inaccessible
    assert research["ReadWritePaths"] == ["/var/lib/tradehub-research"]
    assert execution["ReadWritePaths"] == ["/var/lib/tradehub"]


def test_current_root_execution_secret_files_are_not_readable_by_unprivileged_runtime():
    if os.geteuid() != 0:
        return
    candidates = [ROOT / ".env", ROOT / "data" / "tiger_private_key.pk8.pem"]
    existing = [path for path in candidates if path.exists()]
    if not existing or subprocess.run(["id", "nobody"], capture_output=True).returncode != 0:
        return
    for path in existing:
        result = subprocess.run(
            ["runuser", "-u", "nobody", "--", "test", "-r", str(path)],
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0, path
