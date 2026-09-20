"""Deployment provenance: manifest semantics, drift classification, rollback.

Guards the contract introduced when the stale ``DEPLOYED_COMMIT`` marker was
replaced by a machine-readable ``deployment.json`` manifest.

Everything here is deterministic and offline: each test builds a throwaway git
checkout in ``tmp_path``. The live systemd estate is never touched -- rollback
uses an injected restart hook.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "deploy" / "deployment_cli.py"


def _load_provenance():
    spec = importlib.util.spec_from_file_location(
        "tradehub_deployment_provenance", ROOT / "deploy" / "deployment_provenance.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves annotations via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prov = _load_provenance()


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return proc.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A tiny deployed checkout: two commits on branch ``main``."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "deploy@example.invalid")
    _git(repo, "config", "user.name", "Deployment Test")
    (repo / "service.py").write_text("VERSION = 1\n", encoding="utf-8")
    _commit(repo, "baseline")
    (repo / "service.py").write_text("VERSION = 2\n", encoding="utf-8")
    _commit(repo, "second")
    return repo


def _env_file(repo: Path) -> Path:
    """A declared host-local config file (contents are never read).

    Deliberately research-plane vocabulary: the execution-boundary scanner
    rejects execution env names anywhere under ``tests/`` (see
    ``tests/test_portfolio_boundary.py``).
    """
    path = repo.parent / "research.env"
    path.write_text("RESEARCH_DB_PATH=/var/lib/tradehub-research/research.db\n", encoding="utf-8")
    return path


def _recorded(repo: Path, **kwargs) -> dict:
    kwargs.setdefault("host_local_config", [str(_env_file(repo))])
    kwargs.setdefault("source_branch", "main")
    return prov.record(repo, **kwargs)


def _run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), "--repo", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


# --------------------------------------------------------------------------- #
# 1. metadata semantics
# --------------------------------------------------------------------------- #


def test_live_revision_matches_git_plumbing(checkout: Path):
    live = prov.live_revision(checkout)
    assert live.commit == _git(checkout, "rev-parse", "HEAD")
    assert live.tree == _git(checkout, "rev-parse", "HEAD^{tree}")
    assert live.branch == "main"
    assert live.detached is False
    assert live.subject == "second"


def test_record_writes_every_documented_field(checkout: Path):
    manifest = _recorded(checkout)
    for field in prov.REQUIRED_FIELDS:
        assert field in manifest, field
    assert manifest["deployed_commit"] == _git(checkout, "rev-parse", "HEAD")
    assert manifest["deployed_tree"] == _git(checkout, "rev-parse", "HEAD^{tree}")
    assert manifest["source_branch"] == "main"
    assert manifest["source_branch_tip"] == _git(checkout, "rev-parse", "HEAD")
    assert manifest["deploy_mechanism"] == prov.DEPLOY_MECHANISM
    assert manifest["deploy_mechanism_version"] == prov.DEPLOY_MECHANISM_VERSION
    assert manifest["dirty_at_deploy"] is False
    assert manifest["dirty_paths_at_deploy"] == []
    assert manifest["tree_state"] == "clean"
    assert prov.manifest_path(checkout).is_file()


def test_manifest_round_trips_through_validation(checkout: Path):
    written = _recorded(checkout)
    assert prov.read_manifest(checkout) == written


def test_manifest_records_dirty_state_when_explicitly_allowed(checkout: Path):
    (checkout / "service.py").write_text("VERSION = 3\n", encoding="utf-8")
    manifest = _recorded(checkout, allow_dirty=True)
    assert manifest["dirty_at_deploy"] is True
    assert manifest["dirty_paths_at_deploy"] == ["service.py"]
    assert manifest["tree_state"] == "dirty"


def test_record_is_atomic_and_leaves_no_temp_file(checkout: Path):
    _recorded(checkout)
    leftovers = [p.name for p in checkout.iterdir() if p.name.startswith(".deployment")]
    assert leftovers == []
    json.loads(prov.manifest_path(checkout).read_text(encoding="utf-8"))


def test_declared_host_local_config_is_paths_only(checkout: Path):
    secret = "#1 secret-value-that-must-never-be-recorded"
    path = _env_file(checkout)
    path.write_text(secret + "\n", encoding="utf-8")
    manifest = _recorded(checkout, host_local_config=[str(path)])
    assert manifest["host_local_config"] == [str(path)]
    payload = json.dumps(manifest)
    assert "secret-value-that-must-never-be-recorded" not in payload
    assert secret not in payload


# --------------------------------------------------------------------------- #
# 2. invalid / stale metadata is rejected, never silently accepted
# --------------------------------------------------------------------------- #


def _valid(checkout: Path) -> dict:
    return _recorded(checkout)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda m: m.pop("deployed_commit"), "missing required field"),
        (lambda m: m.update(deployed_commit="c05d753"), "40-character commit SHA"),
        (lambda m: m.update(deployed_commit="z" * 40), "40-character commit SHA"),
        (lambda m: m.update(manifest_version=99), "unsupported manifest_version"),
        (lambda m: m.update(manifest_version=True), "must be int"),
        (lambda m: m.update(deployed_tree="0" * 40), "does not match the tree of"),
        (lambda m: m.update(deployed_at="not-a-timestamp"), "ISO-8601"),
        (lambda m: m.update(dirty_at_deploy="no"), "must be bool"),
        (lambda m: m.update(previous_commit="deadbeef"), "40-character commit SHA"),
        (lambda m: m.update(equivalent_commits="abc"), "must be a list"),
        (lambda m: m.update(source_branch_tip=17), "40-character commit SHA"),
        (
            lambda m: m.update(previous_commit="0" * 40),
            "is not a commit in this clone",
        ),
    ],
)
def test_invalid_manifest_is_rejected(checkout: Path, mutate, expected: str):
    manifest = _valid(checkout)
    mutate(manifest)
    with pytest.raises(prov.ManifestError) as excinfo:
        prov.validate_manifest(manifest, repo=checkout)
    assert expected in str(excinfo.value), str(excinfo.value)


def test_manifest_naming_an_absent_commit_is_rejected(checkout: Path):
    manifest = _valid(checkout)
    manifest["deployed_commit"] = "a" * 40
    manifest["deployed_tree"] = _git(checkout, "rev-parse", "HEAD^{tree}")
    with pytest.raises(prov.ManifestError) as excinfo:
        prov.validate_manifest(manifest, repo=checkout)
    assert "not a commit in this clone" in str(excinfo.value)


def test_manifest_that_is_not_json_is_rejected(checkout: Path):
    prov.manifest_path(checkout).write_text("{not json", encoding="utf-8")
    with pytest.raises(prov.ManifestError) as excinfo:
        prov.read_manifest(checkout)
    assert "not valid JSON" in str(excinfo.value)


def test_manifest_that_is_not_an_object_is_rejected(checkout: Path):
    prov.manifest_path(checkout).write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(prov.ManifestError) as excinfo:
        prov.read_manifest(checkout)
    assert "must be a JSON object" in str(excinfo.value)


def test_missing_manifest_is_explicit_and_distinguishable(checkout: Path):
    with pytest.raises(prov.ManifestMissing) as excinfo:
        prov.read_manifest(checkout)
    assert "no deployment manifest" in str(excinfo.value)
    report = prov.verify(checkout)
    assert report.manifest_present is False
    assert report.ok is False
    assert "manifest missing" in report.failures


def test_verify_detects_a_manifest_recording_a_different_revision(checkout: Path):
    _recorded(checkout)
    (checkout / "other.py").write_text("x = 1\n", encoding="utf-8")
    _commit(checkout, "third")
    report = prov.verify(checkout)
    assert report.ok is False
    assert report.deployed_commit_matches_head is False
    assert any("is not the recorded deployed_commit" in f for f in report.failures)


def test_verify_detects_a_tree_that_no_longer_matches_the_manifest(checkout: Path):
    manifest = _recorded(checkout)
    manifest["deployed_tree"] = _git(checkout, "rev-parse", "HEAD~1^{tree}")
    prov.write_manifest(checkout, manifest)
    report = prov.verify(checkout)
    assert report.ok is False
    assert report.tree_matches_manifest is False


def test_corrupt_manifest_never_verifies_clean(checkout: Path):
    _recorded(checkout)
    prov.manifest_path(checkout).write_text('{"manifest_version": 1}', encoding="utf-8")
    report = prov.verify(checkout)
    assert report.ok is False
    assert report.manifest_valid is False
    assert report.errors


# --------------------------------------------------------------------------- #
# 3. drift classification
# --------------------------------------------------------------------------- #


def test_untracked_host_local_and_generated_paths_are_not_drift(checkout: Path):
    (checkout / ".env.local").write_text("A=1\n", encoding="utf-8")
    (checkout / "data").mkdir()
    (checkout / "data" / "acceptance.json").write_text("{}\n", encoding="utf-8")
    (checkout / ".pytest_cache").mkdir()
    (checkout / ".pytest_cache" / "CACHEDIR.TAG").write_text("x\n", encoding="utf-8")
    (checkout / "__pycache__").mkdir()
    (checkout / "__pycache__" / "service.cpython-310.pyc").write_text("x", encoding="utf-8")
    (checkout / "runtime.db").write_text("", encoding="utf-8")

    drift = prov.classify_drift(checkout)
    assert drift.genuine == ()
    assert drift.clean is True
    assert ".env.local" in drift.host_local
    assert "data/acceptance.json" in drift.generated
    assert "__pycache__/service.cpython-310.pyc" in drift.generated
    assert "runtime.db" in drift.generated


def test_modified_tracked_file_is_genuine_drift(checkout: Path):
    _recorded(checkout)
    (checkout / "service.py").write_text("VERSION = 99\n", encoding="utf-8")
    drift = prov.classify_drift(checkout)
    assert drift.genuine == ("service.py",)
    report = prov.verify(checkout)
    assert report.ok is False
    assert any("genuine working-tree drift" in f for f in report.failures)


def test_deleted_tracked_file_is_genuine_drift(checkout: Path):
    (checkout / "service.py").unlink()
    assert prov.classify_drift(checkout).genuine == ("service.py",)


def test_undeclared_untracked_file_is_genuine_drift(checkout: Path):
    (checkout / "sneaky.py").write_text("x = 1\n", encoding="utf-8")
    assert prov.classify_drift(checkout).genuine == ("sneaky.py",)


def test_nested_undeclared_file_cannot_hide_in_a_new_directory(checkout: Path):
    (checkout / "newdir").mkdir()
    (checkout / "newdir" / "deep.py").write_text("x = 1\n", encoding="utf-8")
    assert "newdir/deep.py" in prov.classify_drift(checkout).genuine


def test_manifest_itself_is_never_reported_as_drift(checkout: Path):
    _recorded(checkout)
    drift = prov.classify_drift(checkout)
    assert "deployment.json" in drift.host_local
    assert drift.genuine == ()


def test_record_refuses_to_stamp_over_genuine_drift(checkout: Path):
    (checkout / "service.py").write_text("VERSION = 99\n", encoding="utf-8")
    with pytest.raises(prov.DriftError) as excinfo:
        _recorded(checkout)
    assert "service.py" in str(excinfo.value)
    assert not prov.manifest_path(checkout).exists()


def test_cli_record_refuses_drift_with_a_distinct_exit_code(checkout: Path):
    (checkout / "service.py").write_text("VERSION = 99\n", encoding="utf-8")
    proc = _run_cli(checkout, "record", "--host-local-config", str(_env_file(checkout)))
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr


# --------------------------------------------------------------------------- #
# 4. rollback target integrity
# --------------------------------------------------------------------------- #


def test_previous_commit_is_inherited_from_the_prior_manifest(checkout: Path):
    prior_deployed = _git(checkout, "rev-parse", "HEAD")
    _recorded(checkout, previous_commit=None)
    assert prov.read_manifest(checkout)["previous_commit"] is None

    (checkout / "service.py").write_text("VERSION = 3\n", encoding="utf-8")
    _commit(checkout, "third")
    inherited = _recorded(checkout)
    assert inherited["previous_commit"] == prior_deployed
    assert inherited["previous_deployed_at"] is not None


def test_rerecord_of_the_same_revision_keeps_the_original_rollback_target(checkout: Path):
    first = _git(checkout, "rev-parse", "HEAD~1")
    _recorded(checkout, previous_commit=first)
    again = _recorded(checkout)
    assert again["previous_commit"] == first, "a re-record must not make rollback a no-op"
    assert again["deployed_commit"] == _git(checkout, "rev-parse", "HEAD")


def test_plan_rollback_resolves_the_recorded_target(checkout: Path):
    first = _git(checkout, "rev-parse", "HEAD~1")
    _recorded(checkout, previous_commit=first)
    plan = prov.plan_rollback(checkout)
    assert plan["to_commit"] == first
    assert plan["from_commit"] == _git(checkout, "rev-parse", "HEAD")
    assert plan["to_tree"] == _git(checkout, "rev-parse", f"{first}^{{tree}}")
    assert plan["target_present"] is True
    assert plan["changes_content"] is True


def test_rollback_target_absent_from_the_clone_fails_verification(checkout: Path):
    manifest = _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    manifest["previous_commit"] = "b" * 40
    prov.write_manifest(checkout, manifest)
    report = prov.verify(checkout)
    assert report.ok is False
    assert report.rollback_resolvable is False
    # The manifest is refused outright rather than "verified with a bad target".
    assert report.manifest_valid is False
    assert any("not a commit in this clone" in e for e in report.errors)
    assert any("rollback target" in f for f in report.failures)


def test_missing_rollback_target_is_explicit_policy_failure(checkout: Path):
    _recorded(checkout, previous_commit=None)
    assert prov.verify(checkout).ok is False
    assert prov.verify(checkout, require_rollback=False).ok is True
    detail = prov.verify(checkout).rollback_detail
    assert detail == "no rollback target recorded"


def test_plan_rollback_refuses_without_a_target(checkout: Path):
    _recorded(checkout, previous_commit=None)
    with pytest.raises(prov.ManifestError) as excinfo:
        prov.plan_rollback(checkout)
    assert "no rollback target recorded" in str(excinfo.value)


def test_perform_rollback_restores_content_and_rerecords_provenance(checkout: Path):
    first = _git(checkout, "rev-parse", "HEAD~1")
    second = _git(checkout, "rev-parse", "HEAD")
    _recorded(checkout, previous_commit=first)

    restarts: list[str] = []
    result = prov.perform_rollback(
        checkout, confirm=True, restart_fn=lambda: restarts.append("restart")
    )
    assert result["rolled_back_from"] == second
    assert result["rolled_back_to"] == first
    assert result["restarted"] is True
    assert restarts == ["restart"], "the injected restart hook must be used, not systemctl"
    assert _git(checkout, "rev-parse", "HEAD") == first
    # detached: a rolled-back checkout must not be a mutable branch again
    assert prov.live_revision(checkout).detached is True
    # provenance now describes the rolled-back revision, reversibly
    manifest = prov.read_manifest(checkout)
    assert manifest["deployed_commit"] == first
    assert manifest["previous_commit"] == second
    assert prov.verify(checkout).ok is True

    back = prov.perform_rollback(checkout, confirm=True, restart_fn=lambda: None)
    assert back["rolled_back_to"] == second
    assert _git(checkout, "rev-parse", "HEAD") == second


def test_perform_rollback_can_skip_the_restart(checkout: Path):
    first = _git(checkout, "rev-parse", "HEAD~1")
    _recorded(checkout, previous_commit=first)

    def _explode() -> None:  # pragma: no cover - must never be called
        raise AssertionError("restart hook must not run when restart=False")

    result = prov.perform_rollback(checkout, confirm=True, restart=False, restart_fn=_explode)
    assert result["restarted"] is False
    assert _git(checkout, "rev-parse", "HEAD") == first


def test_perform_rollback_requires_explicit_confirmation(checkout: Path):
    first = _git(checkout, "rev-parse", "HEAD~1")
    _recorded(checkout, previous_commit=first)
    with pytest.raises(prov.DeploymentError) as excinfo:
        prov.perform_rollback(checkout)
    assert "confirm" in str(excinfo.value)
    assert _git(checkout, "rev-parse", "HEAD") == _git(checkout, "rev-parse", "main")


def test_cli_rollback_without_confirmation_is_refused(checkout: Path):
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    head = _git(checkout, "rev-parse", "HEAD")
    proc = _run_cli(checkout, "rollback")
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert _git(checkout, "rev-parse", "HEAD") == head


# --------------------------------------------------------------------------- #
# 5. equivalence claims are proven, not asserted
# --------------------------------------------------------------------------- #


def test_equivalent_commit_with_an_identical_tree_is_accepted(checkout: Path):
    """Squash-merge equivalence: two different commits, byte-identical trees."""
    reviewed = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "squash-equivalent")
    merged = _git(checkout, "rev-parse", "HEAD")
    assert merged != reviewed
    assert _git(checkout, "rev-parse", f"{merged}^{{tree}}") == _git(
        checkout, "rev-parse", f"{reviewed}^{{tree}}"
    )
    manifest = _recorded(checkout, equivalent_commits=[reviewed])
    assert manifest["equivalent_commits"] == [reviewed]


def test_equivalent_commit_with_a_different_tree_is_rejected(checkout: Path):
    other = _git(checkout, "rev-parse", "HEAD~1")
    with pytest.raises(prov.ManifestError) as excinfo:
        _recorded(checkout, equivalent_commits=[other])
    assert "not identical to the deployed tree" in str(excinfo.value)


def test_equivalence_is_never_asserted_without_validation(checkout: Path):
    """A manifest claiming equivalence must re-validate when read back."""
    reviewed = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "squash-equivalent")
    manifest = _recorded(
        checkout,
        equivalent_commits=[reviewed],
        previous_commit=_git(checkout, "rev-parse", "HEAD~2"),
    )

    # Tamper: point the equivalence claim at a commit with a different tree.
    different = _git(checkout, "rev-parse", "HEAD~2")
    assert _git(checkout, "rev-parse", f"{different}^{{tree}}") != manifest["deployed_tree"]
    manifest["equivalent_commits"] = [different]
    prov.write_manifest(checkout, manifest)
    report = prov.verify(checkout)
    assert report.ok is False
    assert report.manifest_valid is False
    assert any("not identical to the deployed tree" in e for e in report.errors)


# --------------------------------------------------------------------------- #
# 6. the superseded DEPLOYED_COMMIT marker
# --------------------------------------------------------------------------- #


def test_record_retires_the_legacy_marker(checkout: Path):
    marker = checkout / "DEPLOYED_COMMIT"
    marker.write_text(_git(checkout, "rev-parse", "HEAD~1"), encoding="utf-8")
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    assert not marker.exists()
    assert prov.verify(checkout).ok is True


def test_leftover_legacy_marker_is_reported_not_ignored(checkout: Path):
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    (checkout / "DEPLOYED_COMMIT").write_text("0" * 40, encoding="utf-8")
    report = prov.verify(checkout)
    assert report.ok is False
    assert report.drift is not None and report.drift.legacy_marker_present is True
    assert any(prov.LEGACY_MARKER_NAME in f for f in report.failures)
    # Exactly one problem: the leftover marker. Nothing else is wrong.
    assert len(report.failures) == 1, report.failures
    # ...and it is never miscounted as genuine source drift.
    assert report.drift.genuine == ()
    assert prov.LEGACY_MARKER_NAME in report.drift.host_local


def test_legacy_marker_is_a_declared_host_local_name():
    assert prov.LEGACY_MARKER_NAME in prov.HOST_LOCAL_NAMES
    assert prov.MANIFEST_NAME in prov.HOST_LOCAL_NAMES


# --------------------------------------------------------------------------- #
# 7. declared host-local configuration must exist
# --------------------------------------------------------------------------- #


def test_missing_declared_configuration_fails_verification(checkout: Path):
    missing = checkout.parent / "not-installed.env"
    _recorded(checkout, host_local_config=[str(missing)])
    report = prov.verify(checkout)
    assert report.ok is False
    assert any(str(missing) in f for f in report.failures)


def test_declared_host_local_config_is_reported(checkout: Path):
    env = _env_file(checkout)
    _recorded(checkout, host_local_config=[str(env)])
    report = prov.verify(checkout)
    assert report.host_local_config[0]["path"] == str(env)
    assert report.host_local_config[0]["present"] is True


# --------------------------------------------------------------------------- #
# 8. operator surface
# --------------------------------------------------------------------------- #


def test_cli_status_answers_what_is_running(checkout: Path):
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    proc = _run_cli(checkout, "status")
    assert proc.returncode == 0
    assert f"TradeHub is running {_git(checkout, 'rev-parse', 'HEAD')[:12]}" in proc.stdout
    assert "second" in proc.stdout


def test_cli_status_flags_a_stale_manifest(checkout: Path):
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    (checkout / "other.py").write_text("x = 1\n", encoding="utf-8")
    _commit(checkout, "third")
    proc = _run_cli(checkout, "status")
    assert proc.returncode == 1
    assert "manifest records" in proc.stdout


def test_cli_verify_exit_codes(checkout: Path):
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    assert _run_cli(checkout, "verify").returncode == 0
    assert _run_cli(checkout, "verify", "--json").returncode == 0
    (checkout / "service.py").write_text("VERSION = 99\n", encoding="utf-8")
    proc = _run_cli(checkout, "verify")
    assert proc.returncode == 1
    assert "genuine working-tree drift" in proc.stdout


def test_cli_verify_json_is_machine_readable(checkout: Path):
    _recorded(checkout, previous_commit=_git(checkout, "rev-parse", "HEAD~1"))
    proc = _run_cli(checkout, "verify", "--json")
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["live"]["commit"] == _git(checkout, "rev-parse", "HEAD")
    assert payload["drift"]["clean"] is True
    assert payload["rollback_resolvable"] is True


def test_cli_record_then_verify_round_trip(checkout: Path):
    proc = _run_cli(
        checkout,
        "record",
        "--previous-commit",
        _git(checkout, "rev-parse", "HEAD~1"),
        "--source-branch",
        "main",
        "--host-local-config",
        str(_env_file(checkout)),
    )
    assert proc.returncode == 0, proc.stderr
    assert _run_cli(checkout, "verify").returncode == 0


# --------------------------------------------------------------------------- #
# 9. the installed health-watch script keeps a committed source
# --------------------------------------------------------------------------- #


class TestInstalledHostScriptsHaveCommittedSources:
    """Operational scripts outside the service tree must not drift from Git."""

    INSTALLED = "/var/lib/hermes/scripts/tradehub-health-watch.sh"
    SOURCE = "deploy/hermes/tradehub-health-watch.sh"

    def test_installed_watch_script_equals_repo_source(self):
        source = ROOT / self.SOURCE
        assert source.exists(), f"canonical source missing: {self.SOURCE}"
        installed = Path(self.INSTALLED)
        if not installed.exists():
            pytest.skip(f"{self.INSTALLED} not present on this host (e.g. CI)")
        assert installed.read_bytes() == source.read_bytes(), (
            f"the deployed health-watch script differs from {self.SOURCE}; "
            "reinstall it from the repo instead of editing the host copy"
        )

    def test_health_watch_source_is_tracked_by_git(self):
        """It must be committed source, not a host-only file with a repo copy."""
        tracked = _git(ROOT, "ls-files", "--error-unmatch", self.SOURCE)
        assert tracked == self.SOURCE
        assert not Path(self.SOURCE).is_absolute()
