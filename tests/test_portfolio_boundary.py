"""Execution-boundary scan: the research plane must never touch execution.

AST-parse every module under ``tradehub_research/`` and ``tests/`` and reject
execution imports, submit vocabulary, confirmation-token vocabulary, and Tiger
credential names.  Sanctioned negative-test modules are excluded explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_PACKAGE = REPO_ROOT / "tradehub_research"
TESTS_DIR = REPO_ROOT / "tests"

FORBIDDEN_IMPORT_PREFIXES = ("tradehub.",)
FORBIDDEN_IMPORT_EXACT = {"tradehub"}

# Hex-encoded so this scanner does not match itself.
FORBIDDEN_IDENTIFIERS = {
    "submit_order": "7375626d69745f6f72646572",
    "confirmation_token": "636f6e6669726d6174696f6e5f746f6b656e",
    "order_intent": "6f726465725f696e74656e74",
}
FORBIDDEN_STRINGS = {
    "/orders/preview": "2f6f72646572732f70726576696577",
    "/orders/submit": "2f6f72646572732f7375626d6974",
    "confirmation token": "636f6e6669726d6174696f6e20746f6b656e",
}
FORBIDDEN_ENV_PREFIXES = {
    "TIGEROPEN_": "54494745524f50454e5f",
    "TRADEHUB_API_TOKEN": "54524144454855425f4150495f544f4b454e",
    "TRADEHUB_DRY_RUN": "54524144454855425f4452595f52554e",
    "TRADEHUB_SYMBOL_ALLOWLIST": "54524144454855425f53594d424f4c5f414c4c4f574c495354",
}

# Modules that legitimately reference execution vocabulary as NEGATIVE tests or
# absence checks (capability profile verification, RA packs, sanitizer keys).
# The execution-core tests below test ``tradehub/*`` itself; they are the
# EXECUTION plane, not the research plane — the invariant is that the research
# plane never imports them, and no NEW research code may import execution.
SANCTIONED_FILES = {
    "tradehub_research/committee/capability.py",
    "tradehub_research/acceptance/packs/ra00.py",
    "tradehub_research/acceptance/packs/ra02.py",
    "tradehub_research/acceptance/sanitize.py",
    "tests/test_research_capability.py",
    "tests/test_acceptance.py",
    "tests/test_research_spine.py",  # negative test: execution env names are ignored
    # pre-existing execution-core tests (out of Phase-3 scope)
    "tests/test_audit.py",
    "tests/test_config.py",
    "tests/test_mcp_server.py",
    "tests/test_mcp_server_import.py",
    "tests/test_order_flow.py",
    "tests/test_policy.py",
    "tests/test_read_only_api.py",
    "tests/test_telegram_bot.py",
    "tests/test_tiger_gateway.py",
    "tests/test_phase4_execution.py",
    "tests/test_phase4_runtime.py",
    # the oracle module itself (hex-encoded terms; RA-03 repeats this pattern)
    "tests/test_portfolio_boundary.py",
}

DECODED_IDENTIFIERS = {
    key: bytes.fromhex(value).decode() for key, value in FORBIDDEN_IDENTIFIERS.items()
}
DECODED_STRINGS = {key: bytes.fromhex(value).decode() for key, value in FORBIDDEN_STRINGS.items()}
DECODED_ENV = {key: bytes.fromhex(value).decode() for key, value in FORBIDDEN_ENV_PREFIXES.items()}


def _modules() -> list[tuple[str, Path]]:
    modules: list[tuple[str, Path]] = []
    for root in (RESEARCH_PACKAGE, TESTS_DIR):
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            modules.append((path.relative_to(REPO_ROOT).as_posix(), path))
    return modules


def _scan_module(relative: str, path: Path) -> list[str]:
    violations: list[str] = []
    if relative in SANCTIONED_FILES:
        return violations
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name.split(".")[0]
                if name in FORBIDDEN_IMPORT_EXACT or any(
                    alias.name.startswith(prefix) for prefix in FORBIDDEN_IMPORT_PREFIXES
                ):
                    violations.append(f"{relative}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module in FORBIDDEN_IMPORT_EXACT
                or any(node.module.startswith(prefix) for prefix in FORBIDDEN_IMPORT_PREFIXES)
            ):
                violations.append(f"{relative}: from {node.module} import ...")
        elif isinstance(node, ast.Name):
            if node.id in DECODED_IDENTIFIERS:
                violations.append(f"{relative}:{node.lineno}: identifier {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr in DECODED_IDENTIFIERS:
                violations.append(f"{relative}:{node.lineno}: attribute {node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if any(fragment in lowered for fragment in DECODED_STRINGS.values()):
                violations.append(f"{relative}:{node.lineno}: forbidden string")
            if any(lowered.startswith(prefix.lower()) for prefix in DECODED_ENV.values()):
                violations.append(f"{relative}:{node.lineno}: forbidden env name")
    return violations


def test_research_plane_has_no_execution_imports_or_capability():
    violations: list[str] = []
    for relative, path in _modules():
        violations.extend(_scan_module(relative, path))
    assert not violations, "execution-boundary violations:\n" + "\n".join(violations)


def test_research_settings_cannot_emit_execution_env_names():
    import tradehub_research.config as config_module

    fields = set(config_module.ResearchSettings.model_fields.keys())
    forbidden = {name for name in DECODED_ENV.values() if name.lower() in fields}
    assert not forbidden, f"execution env names present in ResearchSettings: {forbidden}"


def test_sanctioned_files_still_exist_and_scan():
    """The sanction allowlist must not rot: every entry must be a real file."""
    for relative in SANCTIONED_FILES:
        assert (REPO_ROOT / relative).exists(), f"sanctioned file missing: {relative}"


def test_scanner_decoded_terms_are_intended():
    """Self-test: the hex-encoded forbidden terms decode to the intended strings."""
    assert DECODED_IDENTIFIERS["submit_order"] == "submit_order"
    assert DECODED_STRINGS["/orders/preview"] == "/orders/preview"
    assert DECODED_ENV["TIGEROPEN_"] == "TIGEROPEN_"


def test_portfolio_package_public_api_is_research_only():
    from tradehub_research.portfolio import __all__ as portfolio_all

    assert "Engine" in "".join(portfolio_all)
    forbidden = {
        name for name in portfolio_all if "order" in name.lower() or "submit" in name.lower()
    }
    assert not forbidden, f"execution-looking names in portfolio public API: {forbidden}"
