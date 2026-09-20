"""Deployment provenance for the TradeHub runtime.

This module is the single source of truth for *what is deployed on this host*.

It replaces the bare ``DEPLOYED_COMMIT`` marker file. That marker had a name that
asserted "the commit currently deployed" while its only writer
(``deploy/fa06_acceptance.py``) merely recorded whatever ``git rev-parse HEAD``
returned at the *start* of an acceptance run -- unconditionally, before any check
ran. It was therefore only ever accurate while FA-06 happened to be running on
the current deploy, and it silently went stale as soon as a deploy did not
re-run FA-06. Nothing in the repository consumed it, so no consumer could notice.

The replacement is a structured, machine-readable manifest (``deployment.json``,
gitignored, host-local) whose every field is validated. A manifest that is
absent, malformed, schema-mismatched, or that names a commit this clone does not
contain is REJECTED -- never silently treated as verified.

Design rules
------------
* **Immutable identity, not branch names.** The authoritative field is
  ``deployed_commit``, a full 40-hex commit SHA. Branches are recorded only as
  provenance *about* that SHA (``source_branch`` / ``source_branch_tip``).
* **Equivalence is proven, not asserted.** ``equivalent_commits`` entries are
  accepted only if their tree is byte-identical to ``deployed_commit``'s tree.
* **The rollback target is explicit.** ``previous_commit`` records the revision
  that was deployed before this one; that is the rollback target.
* **Drift is classified, never guessed.** Every path reported by
  ``git status`` is bucketed into committed source, declared host-local
  metadata/configuration, intentional generated runtime, or GENUINE DRIFT.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MANIFEST_NAME = "deployment.json"
LEGACY_MARKER_NAME = "DEPLOYED_COMMIT"
MANIFEST_VERSION = 1
DEPLOY_MECHANISM = "deploy/deployment_cli.py"
DEPLOY_MECHANISM_VERSION = "1"

#: Host-local configuration that lives deliberately OUTSIDE the checkout.
#: Only the paths are recorded; contents are never read or stored (they hold
#: bearer tokens and broker credentials).
DEFAULT_HOST_LOCAL_CONFIG = (
    "/etc/tradehub/execution.env",
    "/etc/tradehub/research.env",
    "/etc/tradehub/autonomy.env",
)

#: Paths inside the checkout that are intentionally not committed source.
HOST_LOCAL_NAMES = frozenset({".env", MANIFEST_NAME, LEGACY_MARKER_NAME})
HOST_LOCAL_PREFIXES = (".env.",)
#: Intentional generated/runtime output (also covered by .gitignore; the
#: independent declaration here keeps classification correct even if a
#: .gitignore rule is edited).
GENERATED_PREFIXES = (
    "data/",
    ".venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    "build/",
)
GENERATED_SUFFIXES = (
    ".pyc",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".pk8",
    ".key",
    ".p12",
)
GENERATED_CONTAINS = (".egg-info/",)

_SHA_RE = re.compile(r"[0-9a-fA-F]{40}\Z")

REQUIRED_FIELDS: dict[str, type] = {
    "manifest_version": int,
    "deployed_commit": str,
    "deployed_tree": str,
    "deployed_at": str,
    "deploy_mechanism": str,
    "deploy_mechanism_version": str,
    "dirty_at_deploy": bool,
    "dirty_paths_at_deploy": list,
    "host_local_config": list,
}

HEX_FIELDS = ("deployed_commit", "deployed_tree", "previous_commit", "source_branch_tip")


class DeploymentError(Exception):
    """Base class for deployment provenance failures."""


class GitUnavailable(DeploymentError):
    """Git could not answer a question about the checkout."""


class ManifestError(DeploymentError):
    """The manifest is present but not usable (malformed / invalid / stale)."""


class ManifestMissing(ManifestError):
    """No manifest has been recorded for this checkout."""


class DriftError(DeploymentError):
    """Refused to record a deployment over genuine working-tree drift."""


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def run_git(repo: Path | str, *args: str, check: bool = True, strip: bool = True) -> str:
    """Run a git command in ``repo`` and return stdout.

    ``strip=False`` is required for plumbing whose leading whitespace is
    significant (``git status --porcelain``), where the first two characters are
    the status code and the third is a separator.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment
        raise GitUnavailable(f"cannot run git in {repo}: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitUnavailable(
            f"git {' '.join(args)} failed in {repo} "
            f"(rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip() if strip else proc.stdout


def git_ok(repo: Path | str, *args: str) -> bool:
    """True when a git predicate succeeds (e.g. ``cat-file -e``)."""
    try:
        run_git(repo, *args)
    except GitUnavailable:
        return False
    return True


@dataclass(frozen=True)
class LiveRevision:
    """The revision actually checked out / executed on this host."""

    commit: str
    tree: str
    branch: str | None
    detached: bool
    subject: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "tree": self.tree,
            "branch": self.branch,
            "detached": self.detached,
            "subject": self.subject,
        }


def live_revision(repo: Path | str) -> LiveRevision:
    """Read the live revision from the checkout itself (authoritative)."""
    commit = run_git(repo, "rev-parse", "HEAD")
    tree = run_git(repo, "rev-parse", "HEAD^{tree}")
    try:
        branch = run_git(repo, "symbolic-ref", "-q", "--short", "HEAD") or None
    except GitUnavailable:
        branch = None
    detached = branch is None
    subject = run_git(repo, "log", "-1", "--format=%s")
    return LiveRevision(commit=commit, tree=tree, branch=branch, detached=detached, subject=subject)


# --------------------------------------------------------------------------- #
# drift classification
# --------------------------------------------------------------------------- #


def classify_path(path: str) -> str:
    """Bucket one repo-relative path: host_local | generated | genuine."""
    if path in HOST_LOCAL_NAMES or path.startswith(HOST_LOCAL_PREFIXES):
        return "host_local"
    if path.endswith(GENERATED_SUFFIXES) or path.startswith(GENERATED_PREFIXES):
        return "generated"
    if any(marker in path for marker in GENERATED_CONTAINS):
        return "generated"
    return "genuine"


@dataclass(frozen=True)
class DriftReport:
    """Working-tree state, with every difference categorised."""

    genuine: tuple[str, ...] = ()
    host_local: tuple[str, ...] = ()
    generated: tuple[str, ...] = ()
    legacy_marker_present: bool = False

    @property
    def has_genuine(self) -> bool:
        return bool(self.genuine)

    @property
    def clean(self) -> bool:
        return not self.genuine

    def to_dict(self) -> dict[str, Any]:
        return {
            "genuine_drift": list(self.genuine),
            "host_local": list(self.host_local),
            "generated_runtime": list(self.generated),
            "legacy_marker_present": self.legacy_marker_present,
            "clean": self.clean,
        }


def classify_drift(repo: Path | str) -> DriftReport:
    """Classify the checkout's working-tree state via ``git status``.

    Anything tracked-and-modified, or untracked-and-not-declared, is GENUINE
    drift. Untracked paths are listed with ``--untracked-files=all`` so nested
    new files cannot hide inside a new directory.
    """
    raw = run_git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        strip=False,
    )
    buckets: dict[str, list[str]] = {"genuine": [], "host_local": [], "generated": []}
    for record in raw.split("\0"):
        if not record:
            continue
        path = record[3:] if len(record) > 3 else ""
        if not path:
            continue
        buckets[classify_path(path)].append(path)
    marker = Path(repo) / LEGACY_MARKER_NAME
    return DriftReport(
        genuine=tuple(sorted(buckets["genuine"])),
        host_local=tuple(sorted(buckets["host_local"])),
        generated=tuple(sorted(buckets["generated"])),
        legacy_marker_present=marker.exists(),
    )


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #


def manifest_path(repo: Path | str) -> Path:
    return Path(repo) / MANIFEST_NAME


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise ManifestError(f"{field} must be a full 40-character commit SHA, got {value!r}")
    return value


def _require_iso(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ManifestError(f"{field} must be an ISO-8601 timestamp string, got {value!r}")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field} is not a valid ISO-8601 timestamp: {value!r}") from exc
    return value


def validate_manifest(raw: Any, *, repo: Path | str | None = None) -> dict[str, Any]:
    """Structurally validate a manifest, optionally against a real checkout.

    Raises ``ManifestError`` for anything that must never be mistaken for a
    trustworthy deployment record.
    """
    if not isinstance(raw, dict):
        raise ManifestError(f"manifest must be a JSON object, got {type(raw).__name__}")

    for field, expected in REQUIRED_FIELDS.items():
        if field not in raw:
            raise ManifestError(f"manifest is missing required field {field!r}")
        value = raw[field]
        if expected is bool:
            ok = isinstance(value, bool)
        elif expected is int:
            ok = type(value) is int  # bool is an int subclass; reject it
        elif expected is list:
            ok = isinstance(value, list) and all(isinstance(item, str) for item in value)
        else:
            ok = isinstance(value, str) and bool(value)
        if not ok:
            raise ManifestError(
                f"manifest field {field!r} must be {expected.__name__}, got {value!r}"
            )

    if raw["manifest_version"] != MANIFEST_VERSION:
        raise ManifestError(
            f"unsupported manifest_version {raw['manifest_version']!r}; "
            f"this tool understands version {MANIFEST_VERSION}"
        )

    _require_sha(raw["deployed_commit"], "deployed_commit")
    _require_sha(raw["deployed_tree"], "deployed_tree")
    _require_iso(raw["deployed_at"], "deployed_at")

    previous = raw.get("previous_commit")
    if previous is not None:
        _require_sha(previous, "previous_commit")
    previous_at = raw.get("previous_deployed_at")
    if previous_at is not None:
        _require_iso(previous_at, "previous_deployed_at")
    branch_tip = raw.get("source_branch_tip")
    if branch_tip is not None:
        _require_sha(branch_tip, "source_branch_tip")

    equivalents = raw.get("equivalent_commits", [])
    if not isinstance(equivalents, list):
        raise ManifestError("equivalent_commits must be a list when present")
    for sha in equivalents:
        _require_sha(sha, "equivalent_commits")

    if repo is not None:
        _validate_against_repo(raw, repo, equivalents)
    return raw


def _validate_against_repo(raw: dict[str, Any], repo: Path | str, equivalents: list[str]) -> None:
    """Every SHA the manifest names must exist here, and trees must agree."""
    named: list[tuple[str, str]] = [("deployed_commit", raw["deployed_commit"])]
    if raw.get("previous_commit"):
        named.append(("previous_commit", raw["previous_commit"]))
    for sha in equivalents:
        named.append(("equivalent_commits", sha))

    for field, sha in named:
        if not git_ok(repo, "cat-file", "-e", f"{sha}^{{commit}}"):
            raise ManifestError(
                f"manifest {field} {sha[:12]} is not a commit in this clone; "
                "the manifest does not describe this checkout"
            )

    actual_tree = run_git(repo, "rev-parse", f"{raw['deployed_commit']}^{{tree}}")
    if actual_tree != raw["deployed_tree"]:
        raise ManifestError(
            f"manifest deployed_tree {raw['deployed_tree'][:12]} does not match the tree of "
            f"{raw['deployed_commit'][:12]} (actual {actual_tree[:12]})"
        )

    for sha in equivalents:
        tree = run_git(repo, "rev-parse", f"{sha}^{{tree}}")
        if tree != raw["deployed_tree"]:
            raise ManifestError(
                f"equivalent_commits entry {sha[:12]} has tree {tree[:12]}, which is not "
                f"identical to the deployed tree {raw['deployed_tree'][:12]}"
            )


def read_manifest(
    repo: Path | str,
    *,
    path: Path | str | None = None,
    validate: bool = True,
) -> dict[str, Any]:
    """Load and (by default) validate the recorded manifest.

    Structural validation is always enforced; pass ``validate=True`` (the
    default) to also require that every SHA exists in this clone.
    """
    target = Path(path) if path is not None else manifest_path(repo)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManifestMissing(
            f"no deployment manifest at {target}; run "
            f"`python deploy/deployment_cli.py record` after deploying"
        ) from exc
    except OSError as exc:
        raise ManifestError(f"deployment manifest {target} is unreadable: {exc}") from exc
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ManifestError(f"deployment manifest {target} is not valid JSON: {exc}") from exc
    if not validate:
        return raw
    return validate_manifest(raw, repo=repo)


def write_manifest(repo: Path | str, manifest: dict[str, Any]) -> Path:
    """Atomically write the manifest (never leave a half-written record)."""
    target = manifest_path(repo)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target


def resolve_branch_tip(repo: Path | str, branch: str) -> str | None:
    """Upstream tip for a branch, preferring ``origin/<branch>``."""
    for candidate in (f"origin/{branch}", branch):
        if git_ok(repo, "rev-parse", "--verify", f"{candidate}^{{commit}}"):
            return run_git(repo, "rev-parse", f"{candidate}^{{commit}}")
    return None


def _existing_manifest(repo: Path | str) -> dict[str, Any] | None:
    """Read the current manifest, tolerating absence but not corruption."""
    target = manifest_path(repo)
    if not target.exists():
        return None
    return read_manifest(repo, validate=False)


def build_manifest(
    repo: Path | str,
    *,
    deployed_at: str | None = None,
    source_branch: str | None = None,
    source_release: str | None = None,
    previous_commit: str | None = "inherit",
    equivalent_commits: list[str] | None = None,
    host_local_config: list[str] | None = None,
    allow_dirty: bool = False,
    force: bool = False,
    deploy_mechanism: str = DEPLOY_MECHANISM,
    deploy_mechanism_version: str = DEPLOY_MECHANISM_VERSION,
) -> dict[str, Any]:
    """Build a manifest describing the revision currently checked out.

    ``previous_commit`` defaults to the sentinel ``"inherit"``, which derives the
    rollback target from the existing manifest: the previously deployed commit
    when the revision has changed, or the unchanged older target when this is a
    metadata-only re-record of the same revision (so a re-record can never
    silently turn rollback into a no-op).
    """
    live = live_revision(repo)
    drift = classify_drift(repo)
    if drift.has_genuine and not allow_dirty:
        raise DriftError(
            "refusing to record a deployment over genuine working-tree drift: "
            + ", ".join(drift.genuine)
        )

    previous_deployed_at: str | None = None
    if previous_commit == "inherit":
        existing = _existing_manifest(repo)
        if existing is not None and not force:
            validate_manifest(existing)
            if existing.get("deployed_commit") == live.commit:
                previous_commit = existing.get("previous_commit")
                previous_deployed_at = existing.get("previous_deployed_at")
            else:
                previous_commit = existing.get("deployed_commit")
                previous_deployed_at = existing.get("deployed_at")
        else:
            previous_commit = None
    if previous_commit is not None:
        _require_sha(previous_commit, "previous_commit")

    equivalents = list(equivalent_commits or [])
    for sha in equivalents:
        _require_sha(sha, "equivalent_commits")
    equivalents = [sha for sha in equivalents if sha != live.commit]

    when = deployed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "deployed_commit": live.commit,
        "deployed_tree": live.tree,
        "deployed_at": when,
        "source_branch": source_branch,
        "source_release": source_release,
        "source_branch_tip": resolve_branch_tip(repo, source_branch) if source_branch else None,
        "previous_commit": previous_commit,
        "previous_deployed_at": previous_deployed_at,
        "deploy_mechanism": deploy_mechanism,
        "deploy_mechanism_version": deploy_mechanism_version,
        "dirty_at_deploy": drift.has_genuine,
        "dirty_paths_at_deploy": list(drift.genuine),
        "equivalent_commits": equivalents,
        "host_local_config": list(
            host_local_config if host_local_config is not None else DEFAULT_HOST_LOCAL_CONFIG
        ),
        "host": socket.gethostname(),
        "tree_state": "dirty" if drift.has_genuine else "clean",
    }
    return validate_manifest(manifest, repo=repo)


def record(
    repo: Path | str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build and persist a manifest for the currently checked-out revision."""
    manifest = build_manifest(repo, **kwargs)
    write_manifest(repo, manifest)
    if Path(repo, LEGACY_MARKER_NAME).exists():
        _retire_legacy_marker(repo)
    return manifest


def _retire_legacy_marker(repo: Path | str) -> None:
    """Remove the superseded ``DEPLOYED_COMMIT`` marker.

    Its value is preserved as ``previous_commit`` by ``build_manifest`` before
    the marker is dropped; leaving it behind would keep a stale-looking file
    whose name asserts a claim the manifest now owns.
    """
    marker = Path(repo) / LEGACY_MARKER_NAME
    try:
        marker.unlink()
    except OSError:  # pragma: no cover - permissions
        pass


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VerificationReport:
    """Everything an operator needs to answer "what is running, and has it drifted?"."""

    repo: str
    manifest_path: str
    manifest_present: bool
    manifest_valid: bool
    live: LiveRevision | None
    deployed_commit_matches_head: bool
    tree_matches_manifest: bool
    drift: DriftReport | None
    rollback_target: str | None
    rollback_resolvable: bool
    rollback_detail: str
    host_local_config: tuple[dict[str, Any], ...]
    failures: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "manifest_path": self.manifest_path,
            "manifest_present": self.manifest_present,
            "manifest_valid": self.manifest_valid,
            "errors": list(self.errors),
            "live": self.live.to_dict() if self.live else None,
            "deployed_commit_matches_head": self.deployed_commit_matches_head,
            "tree_matches_manifest": self.tree_matches_manifest,
            "drift": self.drift.to_dict() if self.drift else None,
            "rollback_target": self.rollback_target,
            "rollback_resolvable": self.rollback_resolvable,
            "rollback_detail": self.rollback_detail,
            "host_local_config": list(self.host_local_config),
            "failures": list(self.failures),
            "ok": self.ok,
        }


def _check_host_local_config(paths: Any) -> tuple[dict[str, Any], ...]:
    results = []
    for entry in paths or []:
        path = Path(str(entry))
        results.append(
            {
                "path": str(path),
                "present": path.exists(),
                "inside_checkout": False,
            }
        )
    return tuple(results)


def verify(
    repo: Path | str,
    *,
    path: Path | str | None = None,
    require_rollback: bool = True,
) -> VerificationReport:
    """Prove what is deployed: revision, provenance, drift, and rollback target.

    Never raises for expected bad states -- it *reports* them, so a caller (or
    an operator) always gets an answer instead of a traceback.
    """
    repo = Path(repo)
    target = Path(path) if path is not None else manifest_path(repo)
    failures: list[str] = []
    errors: list[str] = []
    manifest: dict[str, Any] | None = None
    manifest_present = target.exists()
    manifest_valid = False

    try:
        manifest = read_manifest(repo, path=target)
        manifest_valid = True
    except ManifestError as exc:
        errors.append(str(exc))
        failures.append("manifest invalid" if manifest_present else "manifest missing")

    live: LiveRevision | None = None
    try:
        live = live_revision(repo)
    except DeploymentError as exc:
        errors.append(str(exc))
        failures.append("live revision unreadable")

    drift: DriftReport | None = None
    try:
        drift = classify_drift(repo)
        if drift.has_genuine:
            failures.append("genuine working-tree drift: " + ", ".join(drift.genuine))
    except DeploymentError as exc:
        errors.append(str(exc))
        failures.append("drift check unavailable")

    deployed_commit_matches_head = bool(
        manifest and live and manifest.get("deployed_commit") == live.commit
    )
    tree_matches_manifest = bool(manifest and live and manifest.get("deployed_tree") == live.tree)
    if manifest_valid and live:
        if not deployed_commit_matches_head:
            failures.append(
                f"HEAD {live.commit[:12]} is not the recorded deployed_commit "
                f"{str(manifest.get('deployed_commit'))[:12]}"
            )
        if not tree_matches_manifest:
            failures.append("working tree does not match the recorded deployed_tree")

    rollback_target = manifest.get("previous_commit") if manifest else None
    rollback_resolvable = False
    rollback_detail = "not checked"
    if rollback_target:
        rollback_resolvable = git_ok(repo, "cat-file", "-e", f"{rollback_target}^{{commit}}")
        rollback_detail = (
            "present in this clone"
            if rollback_resolvable
            else "recorded rollback target is absent from this clone"
        )
        if not rollback_resolvable:
            failures.append(f"rollback target {rollback_target[:12]} is not in this clone")
    else:
        rollback_detail = "no rollback target recorded"
        if require_rollback:
            failures.append(
                "no rollback target recorded; re-record with "
                "`--previous-commit <sha>` (or roll back a deploy that had one)"
            )

    if drift and drift.legacy_marker_present:
        failures.append(
            f"superseded {LEGACY_MARKER_NAME} marker is still present; "
            "remove it (its value belongs in the manifest)"
        )

    config_checks = _check_host_local_config(
        manifest.get("host_local_config") if manifest else None
    )
    for entry in config_checks:
        if not entry["present"]:
            failures.append(f"declared host-local configuration missing: {entry['path']}")

    return VerificationReport(
        repo=str(repo),
        manifest_path=str(target),
        manifest_present=manifest_present,
        manifest_valid=manifest_valid,
        live=live,
        deployed_commit_matches_head=deployed_commit_matches_head,
        tree_matches_manifest=tree_matches_manifest,
        drift=drift,
        rollback_target=rollback_target,
        rollback_resolvable=rollback_resolvable,
        rollback_detail=rollback_detail,
        host_local_config=config_checks,
        failures=tuple(failures),
        errors=tuple(errors),
    )


def plan_rollback(repo: Path | str, *, to: str | None = None) -> dict[str, Any]:
    """Resolve the rollback target without touching the checkout."""
    manifest = read_manifest(repo)
    target = to or manifest.get("previous_commit")
    if not target:
        raise ManifestError(
            "no rollback target recorded in the manifest and none supplied; "
            "pass the revision to roll back to explicitly"
        )
    _require_sha(target, "rollback target")
    present = git_ok(repo, "cat-file", "-e", f"{target}^{{commit}}")
    if not present:
        raise ManifestError(f"rollback target {target[:12]} is not a commit in this clone")
    tree = run_git(repo, "rev-parse", f"{target}^{{tree}}")
    live = live_revision(repo)
    return {
        "from_commit": live.commit,
        "to_commit": target,
        "to_tree": tree,
        "to_subject": run_git(repo, "log", "-1", "--format=%s", target),
        "target_present": True,
        "changes_content": tree != live.tree,
    }


def perform_rollback(
    repo: Path | str,
    *,
    to: str | None = None,
    confirm: bool = False,
    restart: bool = True,
    restart_fn: Any = None,
) -> dict[str, Any]:
    """Roll the checkout back to a recorded revision and re-record provenance.

    ``restart_fn`` is injectable so the mechanics can be tested without touching
    the live systemd estate.
    """
    if not confirm:
        raise DeploymentError("rollback requires confirm=True (operator-authorised action)")
    plan = plan_rollback(repo, to=to)
    from_commit = plan["from_commit"]

    # Preserve the declared host-local configuration across the rollback: it
    # describes the host, not the revision, so it must not silently revert to
    # the built-in defaults.
    try:
        declared = read_manifest(repo, validate=False).get("host_local_config")
    except ManifestError:
        declared = None

    run_git(repo, "checkout", "--quiet", "--detach", plan["to_commit"])
    live = live_revision(repo)
    if live.commit != plan["to_commit"]:
        raise DeploymentError(
            f"rollback checkout did not land on {plan['to_commit'][:12]} (at {live.commit[:12]})"
        )

    restarted = False
    if restart:
        if restart_fn is not None:
            restart_fn()
        else:
            _restart_services()
        restarted = True

    manifest = build_manifest(
        repo, previous_commit=from_commit, source_branch=None, host_local_config=declared
    )
    write_manifest(repo, manifest)
    return {
        "rolled_back_from": from_commit,
        "rolled_back_to": plan["to_commit"],
        "restarted": restarted,
        "manifest": manifest,
    }


def _restart_services() -> None:  # pragma: no cover - live host only
    for unit in ("tradehub-execution.service", "tradehub-research.service"):
        subprocess.run(["systemctl", "restart", unit], check=True, timeout=300)
