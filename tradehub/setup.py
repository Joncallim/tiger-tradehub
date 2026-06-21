# ruff: noqa: E501
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from tradehub.config import MIN_API_TOKEN_LENGTH, validate_api_token_value

ENV_KEYS = [
    "TRADEHUB_API_TOKEN",
    "TRADEHUB_DRY_RUN",
    "TRADEHUB_BIND_HOST",
    "TRADEHUB_PORT",
    "TRADEHUB_DATABASE_PATH",
    "TRADEHUB_SYMBOL_ALLOWLIST",
    "TRADEHUB_MAX_NOTIONAL_USD",
    "TRADEHUB_MAX_QUANTITY",
    "TRADEHUB_CONFIRMATION_TTL_SECONDS",
    "TIGEROPEN_TIGER_ID",
    "TIGEROPEN_ACCOUNT",
    "TIGEROPEN_PRIVATE_KEY_PATH",
    "TIGEROPEN_PRIVATE_KEY",
    "TIGEROPEN_SANDBOX",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
]
SECRET_KEYS = {"TRADEHUB_API_TOKEN", "TIGEROPEN_PRIVATE_KEY", "TELEGRAM_BOT_TOKEN"}
LOCAL_TEST_HOST = "testclient"
ENV_LINE_PATTERN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


class SetupEnvRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    api_token: str | None = Field(default=None, alias="TRADEHUB_API_TOKEN")
    generate_api_token: bool = True
    dry_run: bool = Field(default=True, alias="TRADEHUB_DRY_RUN")
    bind_host: str = Field(default="127.0.0.1", alias="TRADEHUB_BIND_HOST")
    port: int = Field(default=8787, ge=1, le=65535, alias="TRADEHUB_PORT")
    database_path: str = Field(default="data/tradehub.db", alias="TRADEHUB_DATABASE_PATH")
    symbol_allowlist: str = Field(default="AAPL,MSFT,VOO", alias="TRADEHUB_SYMBOL_ALLOWLIST")
    max_notional_usd: float = Field(default=1000.0, gt=0, alias="TRADEHUB_MAX_NOTIONAL_USD")
    max_quantity: float = Field(default=100.0, gt=0, alias="TRADEHUB_MAX_QUANTITY")
    confirmation_ttl_seconds: int = Field(
        default=300, ge=30, alias="TRADEHUB_CONFIRMATION_TTL_SECONDS"
    )
    tiger_id: str | None = Field(default=None, alias="TIGEROPEN_TIGER_ID")
    tiger_account: str | None = Field(default=None, alias="TIGEROPEN_ACCOUNT")
    tiger_private_key_path: str | None = Field(default=None, alias="TIGEROPEN_PRIVATE_KEY_PATH")
    tiger_private_key: str | None = Field(default=None, alias="TIGEROPEN_PRIVATE_KEY")
    clear_tiger_private_key: bool = False
    tiger_sandbox: bool = Field(default=False, alias="TIGEROPEN_SANDBOX")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    clear_telegram_bot_token: bool = False
    telegram_allowed_chat_ids: str | None = Field(default=None, alias="TELEGRAM_ALLOWED_CHAT_IDS")


class McpConfigRequest(BaseModel):
    config_path: str | None = None
    command: str | None = None


def is_local_host(host: str | None) -> bool:
    if not host:
        return False
    if host == LOCAL_TEST_HOST:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def is_allowed_setup_host_header(host: str | None) -> bool:
    host_name = normalize_host_header(host)
    return is_local_host(host_name)


def is_allowed_setup_origin(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return False
    return is_local_host(parsed.hostname)


def is_allowed_setup_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


def normalize_host_header(host: str | None) -> str | None:
    if not host:
        return None
    value = host.strip().lower().rstrip(".")
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        name, port = value.rsplit(":", 1)
        if port.isdigit():
            return name
    return value


def setup_status(env_path: Path | None = None) -> dict[str, Any]:
    env_path = env_path or Path(".env")
    raw = read_env(env_path)
    token = raw.get("TRADEHUB_API_TOKEN") or ""
    token_error = None
    try:
        validate_api_token_value(token)
        token_valid = True
    except ValueError as exc:
        token_valid = False
        token_error = str(exc)

    return {
        "env_exists": env_path.exists(),
        "env_path": str(env_path.resolve()),
        "setup_complete": token_valid,
        "api_token_configured": token_valid,
        "api_token_preview": mask_secret(token) if token else None,
        "api_token_error": token_error,
        "tiger_configured": bool(
            raw.get("TIGEROPEN_TIGER_ID")
            and raw.get("TIGEROPEN_ACCOUNT")
            and (raw.get("TIGEROPEN_PRIVATE_KEY") or raw.get("TIGEROPEN_PRIVATE_KEY_PATH"))
        ),
        "mcp_command": default_mcp_command(),
        "mcp_config_path": str(default_mcp_config_path()),
        "values": public_env_values(raw),
        "secrets": {
            key: {"configured": bool(raw.get(key)), "preview": mask_secret(raw.get(key) or "")}
            for key in SECRET_KEYS
        },
    }


def write_env(request: SetupEnvRequest, env_path: Path | None = None) -> dict[str, Any]:
    env_path = env_path or Path(".env")
    existing = read_env(env_path)
    token, generated_token = resolve_api_token(request, existing)
    tiger_private_key = resolve_secret(
        request.tiger_private_key,
        existing.get("TIGEROPEN_PRIVATE_KEY"),
        clear=request.clear_tiger_private_key,
    )
    telegram_token = resolve_secret(
        request.telegram_bot_token,
        existing.get("TELEGRAM_BOT_TOKEN"),
        clear=request.clear_telegram_bot_token,
    )

    values = {
        "TRADEHUB_API_TOKEN": token,
        "TRADEHUB_DRY_RUN": bool_env(request.dry_run),
        "TRADEHUB_BIND_HOST": clean_text(request.bind_host) or "127.0.0.1",
        "TRADEHUB_PORT": str(request.port),
        "TRADEHUB_DATABASE_PATH": clean_text(request.database_path) or "data/tradehub.db",
        "TRADEHUB_SYMBOL_ALLOWLIST": clean_text(request.symbol_allowlist),
        "TRADEHUB_MAX_NOTIONAL_USD": format_number(request.max_notional_usd),
        "TRADEHUB_MAX_QUANTITY": format_number(request.max_quantity),
        "TRADEHUB_CONFIRMATION_TTL_SECONDS": str(request.confirmation_ttl_seconds),
        "TIGEROPEN_TIGER_ID": clean_text(request.tiger_id),
        "TIGEROPEN_ACCOUNT": clean_text(request.tiger_account),
        "TIGEROPEN_PRIVATE_KEY_PATH": clean_text(request.tiger_private_key_path),
        "TIGEROPEN_PRIVATE_KEY": tiger_private_key,
        "TIGEROPEN_SANDBOX": bool_env(request.tiger_sandbox),
        "TELEGRAM_BOT_TOKEN": telegram_token,
        "TELEGRAM_ALLOWED_CHAT_IDS": clean_text(request.telegram_allowed_chat_ids),
    }
    write_env_file(env_path, values)
    return {
        "ok": True,
        "env_path": str(env_path.resolve()),
        "generated_api_token": generated_token,
        "api_token_preview": mask_secret(token),
        "mcp_command": default_mcp_command(),
        "mcp_config_path": str(default_mcp_config_path()),
    }


def mcp_snippet(command: str | None = None, env_path: Path | None = None) -> dict[str, Any]:
    env_path = env_path or Path(".env")
    raw = read_env(env_path)
    token = raw.get("TRADEHUB_API_TOKEN") or ""
    validate_api_token_value(token)
    return {
        "mcpServers": {
            "tiger-tradehub": {
                "command": command or default_mcp_command(),
                "env": {
                    "TRADEHUB_BASE_URL": local_base_url(raw),
                    "TRADEHUB_API_TOKEN": token,
                },
            }
        }
    }


def write_mcp_config(request: McpConfigRequest, env_path: Path | None = None) -> dict[str, Any]:
    config_path = Path(request.config_path or default_mcp_config_path()).expanduser()
    snippet = mcp_snippet(command=request.command, env_path=env_path)
    if config_path.exists() and config_path.read_text(encoding="utf-8").strip():
        current = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise ValueError("MCP config file must contain a JSON object")
    else:
        current = {}

    servers = current.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be a JSON object")
    servers["tiger-tradehub"] = snippet["mcpServers"]["tiger-tradehub"]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = backup_file(config_path) if config_path.exists() else None
    atomic_write_text(config_path, json.dumps(current, indent=2) + "\n")
    result = {
        "ok": True,
        "config_path": str(config_path.resolve()),
        "server_name": "tiger-tradehub",
    }
    if backup_path:
        result["backup_path"] = str(backup_path.resolve())
    return result


def read_env(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    return {key: value or "" for key, value in dotenv_values(env_path).items() if key is not None}


def resolve_api_token(request: SetupEnvRequest, existing: dict[str, str]) -> tuple[str, bool]:
    provided = clean_text(request.api_token)
    existing_token = existing.get("TRADEHUB_API_TOKEN") or ""
    if provided:
        validate_api_token_value(provided)
        return provided, False
    if existing_token:
        try:
            validate_api_token_value(existing_token)
            return existing_token, False
        except ValueError:
            pass
    if request.generate_api_token:
        token = secrets.token_urlsafe(32)
        validate_api_token_value(token)
        return token, True
    raise ValueError(
        f"TRADEHUB_API_TOKEN must be provided or generated; minimum length is "
        f"{MIN_API_TOKEN_LENGTH}"
    )


def resolve_secret(provided: str | None, existing: str | None, *, clear: bool) -> str:
    if clear:
        return ""
    if provided:
        return provided.strip() if "\n" not in provided else provided.strip("\r\n")
    return existing or ""


def write_env_file(env_path: Path, values: dict[str, str]) -> None:
    unknown_lines = read_unknown_env_lines(env_path)
    lines = [
        "# Generated by Tiger TradeHub setup.",
        "# Keep this file private.",
        "",
        "# Local API used by REST, MCP, and Telegram clients.",
        env_line("TRADEHUB_API_TOKEN", values["TRADEHUB_API_TOKEN"]),
        env_line("TRADEHUB_DRY_RUN", values["TRADEHUB_DRY_RUN"]),
        env_line("TRADEHUB_BIND_HOST", values["TRADEHUB_BIND_HOST"]),
        env_line("TRADEHUB_PORT", values["TRADEHUB_PORT"]),
        env_line("TRADEHUB_DATABASE_PATH", values["TRADEHUB_DATABASE_PATH"]),
        "",
        "# Guardrails. Empty TRADEHUB_SYMBOL_ALLOWLIST means no allowlist restriction.",
        env_line("TRADEHUB_SYMBOL_ALLOWLIST", values["TRADEHUB_SYMBOL_ALLOWLIST"]),
        env_line("TRADEHUB_MAX_NOTIONAL_USD", values["TRADEHUB_MAX_NOTIONAL_USD"]),
        env_line("TRADEHUB_MAX_QUANTITY", values["TRADEHUB_MAX_QUANTITY"]),
        env_line(
            "TRADEHUB_CONFIRMATION_TTL_SECONDS",
            values["TRADEHUB_CONFIRMATION_TTL_SECONDS"],
        ),
        "",
        "# Tiger OpenAPI credentials. Prefer a paper account while testing.",
        env_line("TIGEROPEN_TIGER_ID", values["TIGEROPEN_TIGER_ID"]),
        env_line("TIGEROPEN_ACCOUNT", values["TIGEROPEN_ACCOUNT"]),
        env_line("TIGEROPEN_PRIVATE_KEY_PATH", values["TIGEROPEN_PRIVATE_KEY_PATH"]),
        env_line("TIGEROPEN_PRIVATE_KEY", values["TIGEROPEN_PRIVATE_KEY"]),
        env_line("TIGEROPEN_SANDBOX", values["TIGEROPEN_SANDBOX"]),
        "",
        "# Telegram integration. Leave blank to disable.",
        env_line("TELEGRAM_BOT_TOKEN", values["TELEGRAM_BOT_TOKEN"]),
        env_line("TELEGRAM_ALLOWED_CHAT_IDS", values["TELEGRAM_ALLOWED_CHAT_IDS"]),
    ]
    if unknown_lines:
        lines.extend(["", "# Existing custom values preserved by setup.", *unknown_lines])
    env_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(env_path, "\n".join(lines) + "\n")


def backup_file(path: Path) -> Path:
    backup_path = path.with_name(f"{path.name}.bak")
    backup_path.write_bytes(path.read_bytes())
    os.chmod(backup_path, 0o600)
    return backup_path


def atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def read_unknown_env_lines(env_path: Path) -> list[str]:
    if not env_path.exists():
        return []
    unknown: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        match = ENV_LINE_PATTERN.match(line)
        if match and match.group(1) not in ENV_KEYS:
            unknown.append(line)
    return unknown


def public_env_values(raw: dict[str, str]) -> dict[str, Any]:
    return {
        "TRADEHUB_DRY_RUN": bool_value(raw.get("TRADEHUB_DRY_RUN"), True),
        "TRADEHUB_BIND_HOST": raw.get("TRADEHUB_BIND_HOST") or "127.0.0.1",
        "TRADEHUB_PORT": int_value(raw.get("TRADEHUB_PORT"), 8787),
        "TRADEHUB_DATABASE_PATH": raw.get("TRADEHUB_DATABASE_PATH") or "data/tradehub.db",
        "TRADEHUB_SYMBOL_ALLOWLIST": raw.get("TRADEHUB_SYMBOL_ALLOWLIST") or "AAPL,MSFT,VOO",
        "TRADEHUB_MAX_NOTIONAL_USD": number_value(raw.get("TRADEHUB_MAX_NOTIONAL_USD"), 1000),
        "TRADEHUB_MAX_QUANTITY": number_value(raw.get("TRADEHUB_MAX_QUANTITY"), 100),
        "TRADEHUB_CONFIRMATION_TTL_SECONDS": int_value(
            raw.get("TRADEHUB_CONFIRMATION_TTL_SECONDS"), 300
        ),
        "TIGEROPEN_TIGER_ID": raw.get("TIGEROPEN_TIGER_ID") or "",
        "TIGEROPEN_ACCOUNT": raw.get("TIGEROPEN_ACCOUNT") or "",
        "TIGEROPEN_PRIVATE_KEY_PATH": raw.get("TIGEROPEN_PRIVATE_KEY_PATH") or "",
        "TIGEROPEN_SANDBOX": bool_value(raw.get("TIGEROPEN_SANDBOX"), False),
        "TELEGRAM_ALLOWED_CHAT_IDS": raw.get("TELEGRAM_ALLOWED_CHAT_IDS") or "",
    }


def env_line(key: str, value: str) -> str:
    return f"{key}={encode_env_value(value)}"


def encode_env_value(value: str) -> str:
    if value == "":
        return ""
    if re.fullmatch(r"[A-Za-z0-9_./:@,+-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return f'"{escaped}"'


def local_base_url(raw: dict[str, str]) -> str:
    host = raw.get("TRADEHUB_BIND_HOST") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = int_value(raw.get("TRADEHUB_PORT"), 8787)
    return f"http://{host}:{port}"


def default_mcp_command() -> str:
    executable = "tradehub-mcp.exe" if os.name == "nt" else "tradehub-mcp"
    local_command = Path.cwd() / ".venv" / ("Scripts" if os.name == "nt" else "bin") / executable
    if local_command.exists():
        return str(local_command.resolve())
    discovered = shutil.which(executable)
    if discovered:
        return discovered
    return str(local_command.resolve())


def default_mcp_config_path() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / ("claude_desktop_config.json")
        )
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def clean_text(value: str | None) -> str:
    return (value or "").strip()


def bool_env(value: bool) -> str:
    return "true" if value else "false"


def bool_value(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_value(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def number_value(value: str | None, default: float | int) -> float | int:
    try:
        number = float(value) if value not in (None, "") else float(default)
    except ValueError:
        return default
    return int(number) if number.is_integer() else number


def format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


SETUP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Tiger TradeHub Setup</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --panel-2: #eef3f8;
      --text: #15181d;
      --muted: #5f6875;
      --line: #d7dce3;
      --accent: #0b6bcb;
      --accent-2: #0f766e;
      --danger: #b42318;
      --ok: #166534;
      --shadow: 0 1px 2px rgba(10, 20, 30, .08);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #121417;
        --panel: #1b1f24;
        --panel-2: #202833;
        --text: #edf1f5;
        --muted: #a9b2bf;
        --line: #303842;
        --accent: #5aa2ff;
        --accent-2: #39b7a8;
        --danger: #ff8a80;
        --ok: #86efac;
        --shadow: none;
      }
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); }
    main { width: min(1180px, calc(100vw - 32px)); margin: 24px auto 48px; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 { margin: 0; font-size: 26px; line-height: 1.2; letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 17px; line-height: 1.3; letter-spacing: 0; }
    p { margin: 0; color: var(--muted); }
    form { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .full { grid-column: 1 / -1; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .field { display: grid; gap: 6px; min-width: 0; }
    label { font-size: 13px; font-weight: 650; color: var(--text); }
    input, textarea, select {
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      font: inherit;
      font-size: 14px;
      padding: 9px 10px;
      outline: none;
    }
    textarea { min-height: 118px; resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    input:focus, textarea:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 20%, transparent); }
    .check {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      color: var(--text);
      font-size: 14px;
    }
    .check input { width: 16px; height: 16px; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; }
    button {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      font: inherit;
      font-weight: 700;
      font-size: 14px;
      padding: 9px 12px;
      cursor: pointer;
    }
    button.primary { background: var(--accent); border-color: var(--accent); color: white; }
    button.secondary { background: var(--accent-2); border-color: var(--accent-2); color: white; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .status {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 5px 9px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }
    .status.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 35%, var(--line)); }
    .status.bad { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 35%, var(--line)); }
    .hint { color: var(--muted); font-size: 12px; line-height: 1.35; overflow-wrap: anywhere; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    pre {
      margin: 12px 0 0;
      padding: 12px;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: var(--panel-2);
      overflow: auto;
      min-height: 92px;
      font-size: 12px;
      line-height: 1.45;
    }
    .message { margin-top: 12px; font-size: 13px; color: var(--muted); min-height: 20px; }
    .message.error { color: var(--danger); }
    .message.success { color: var(--ok); }
    @media (max-width: 840px) {
      header, form, .grid { display: block; }
      section { margin-bottom: 14px; }
      .field { margin-bottom: 12px; }
      .status { white-space: normal; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>Tiger TradeHub Setup</h1>
        <p id="env-path" class="hint mono"></p>
      </div>
      <div id="setup-status" class="status">Checking setup</div>
    </header>

    <form id="env-form">
      <section>
        <h2>Core</h2>
        <div class="grid">
          <div class="field">
            <label for="api-token">API token</label>
            <input id="api-token" name="TRADEHUB_API_TOKEN" type="password" autocomplete="new-password">
            <div id="token-preview" class="hint"></div>
          </div>
          <label class="check">
            <input id="generate-token" type="checkbox" checked>
            Generate token when missing
          </label>
          <label class="check">
            <input id="dry-run" name="TRADEHUB_DRY_RUN" type="checkbox" checked>
            Dry-run mode
          </label>
          <label class="check">
            <input id="tiger-sandbox" name="TIGEROPEN_SANDBOX" type="checkbox">
            Tiger sandbox
          </label>
          <div class="field">
            <label for="bind-host">Bind host</label>
            <input id="bind-host" name="TRADEHUB_BIND_HOST" value="127.0.0.1">
          </div>
          <div class="field">
            <label for="port">Port</label>
            <input id="port" name="TRADEHUB_PORT" type="number" min="1" max="65535" value="8787">
          </div>
          <div class="field">
            <label for="database-path">Audit database</label>
            <input id="database-path" name="TRADEHUB_DATABASE_PATH" value="data/tradehub.db">
          </div>
        </div>
      </section>

      <section>
        <h2>Guardrails</h2>
        <div class="grid">
          <div class="field">
            <label for="symbol-allowlist">Symbol allowlist</label>
            <input id="symbol-allowlist" name="TRADEHUB_SYMBOL_ALLOWLIST" value="AAPL,MSFT,VOO">
          </div>
          <div class="field">
            <label for="max-notional">Max notional USD</label>
            <input id="max-notional" name="TRADEHUB_MAX_NOTIONAL_USD" type="number" min="1" step="1" value="1000">
          </div>
          <div class="field">
            <label for="max-quantity">Max quantity</label>
            <input id="max-quantity" name="TRADEHUB_MAX_QUANTITY" type="number" min="1" step="1" value="100">
          </div>
          <div class="field">
            <label for="confirmation-ttl">Confirmation TTL seconds</label>
            <input id="confirmation-ttl" name="TRADEHUB_CONFIRMATION_TTL_SECONDS" type="number" min="30" step="1" value="300">
          </div>
        </div>
      </section>

      <section>
        <h2>Tiger OpenAPI</h2>
        <div class="grid">
          <div class="field">
            <label for="tiger-id">Tiger ID</label>
            <input id="tiger-id" name="TIGEROPEN_TIGER_ID">
          </div>
          <div class="field">
            <label for="tiger-account">Account</label>
            <input id="tiger-account" name="TIGEROPEN_ACCOUNT">
          </div>
          <div class="field full">
            <label for="private-key-path">Private key path</label>
            <input id="private-key-path" name="TIGEROPEN_PRIVATE_KEY_PATH">
          </div>
          <div class="field full">
            <label for="private-key">Private key</label>
            <textarea id="private-key" name="TIGEROPEN_PRIVATE_KEY" autocomplete="off"></textarea>
            <div id="private-key-preview" class="hint"></div>
            <label class="check">
              <input id="clear-private-key" type="checkbox">
              Clear saved private key
            </label>
          </div>
        </div>
      </section>

      <section>
        <h2>Telegram</h2>
        <div class="grid">
          <div class="field">
            <label for="telegram-token">Bot token</label>
            <input id="telegram-token" name="TELEGRAM_BOT_TOKEN" type="password" autocomplete="new-password">
            <div id="telegram-preview" class="hint"></div>
          </div>
          <div class="field">
            <label for="telegram-chats">Allowed chat IDs</label>
            <input id="telegram-chats" name="TELEGRAM_ALLOWED_CHAT_IDS">
          </div>
          <label class="check">
            <input id="clear-telegram-token" type="checkbox">
            Clear saved bot token
          </label>
        </div>
      </section>

      <section class="full">
        <h2>MCP</h2>
        <div class="grid">
          <div class="field">
            <label for="mcp-command">MCP command</label>
            <input id="mcp-command">
          </div>
          <div class="field">
            <label for="mcp-config-path">Claude config path</label>
            <input id="mcp-config-path">
          </div>
        </div>
        <div class="actions">
          <button id="save-env" class="primary" type="submit">Save .env</button>
          <button id="write-mcp" class="secondary" type="button">Write MCP config</button>
          <button id="show-mcp" type="button">Show MCP JSON</button>
        </div>
        <div id="message" class="message"></div>
        <pre id="mcp-json" hidden></pre>
      </section>
    </form>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const msg = $("message");

    function setMessage(text, type = "") {
      msg.textContent = text || "";
      msg.className = "message" + (type ? " " + type : "");
    }

    function value(id) { return $(id).value; }
    function checked(id) { return $(id).checked; }
    function setValue(id, val) { $(id).value = val ?? ""; }
    function setChecked(id, val) { $(id).checked = Boolean(val); }

    async function request(path, options = {}) {
      const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
        throw new Error(detail);
      }
      return data;
    }

    function payload() {
      return {
        TRADEHUB_API_TOKEN: value("api-token"),
        generate_api_token: checked("generate-token"),
        TRADEHUB_DRY_RUN: checked("dry-run"),
        TRADEHUB_BIND_HOST: value("bind-host"),
        TRADEHUB_PORT: Number(value("port")),
        TRADEHUB_DATABASE_PATH: value("database-path"),
        TRADEHUB_SYMBOL_ALLOWLIST: value("symbol-allowlist"),
        TRADEHUB_MAX_NOTIONAL_USD: Number(value("max-notional")),
        TRADEHUB_MAX_QUANTITY: Number(value("max-quantity")),
        TRADEHUB_CONFIRMATION_TTL_SECONDS: Number(value("confirmation-ttl")),
        TIGEROPEN_TIGER_ID: value("tiger-id"),
        TIGEROPEN_ACCOUNT: value("tiger-account"),
        TIGEROPEN_PRIVATE_KEY_PATH: value("private-key-path"),
        TIGEROPEN_PRIVATE_KEY: value("private-key"),
        clear_tiger_private_key: checked("clear-private-key"),
        TIGEROPEN_SANDBOX: checked("tiger-sandbox"),
        TELEGRAM_BOT_TOKEN: value("telegram-token"),
        clear_telegram_bot_token: checked("clear-telegram-token"),
        TELEGRAM_ALLOWED_CHAT_IDS: value("telegram-chats")
      };
    }

    function applyStatus(data) {
      $("env-path").textContent = data.env_path;
      const badge = $("setup-status");
      badge.textContent = data.setup_complete ? ".env ready" : ".env needs token";
      badge.className = "status " + (data.setup_complete ? "ok" : "bad");
      $("token-preview").textContent = data.api_token_preview ? "Saved: " + data.api_token_preview : "";
      $("telegram-preview").textContent = data.secrets.TELEGRAM_BOT_TOKEN?.configured ? "Saved: " + data.secrets.TELEGRAM_BOT_TOKEN.preview : "";
      $("private-key-preview").textContent = data.secrets.TIGEROPEN_PRIVATE_KEY?.configured ? "Saved: " + data.secrets.TIGEROPEN_PRIVATE_KEY.preview : "";
      const values = data.values || {};
      setChecked("dry-run", values.TRADEHUB_DRY_RUN);
      setChecked("tiger-sandbox", values.TIGEROPEN_SANDBOX);
      setValue("bind-host", values.TRADEHUB_BIND_HOST);
      setValue("port", values.TRADEHUB_PORT);
      setValue("database-path", values.TRADEHUB_DATABASE_PATH);
      setValue("symbol-allowlist", values.TRADEHUB_SYMBOL_ALLOWLIST);
      setValue("max-notional", values.TRADEHUB_MAX_NOTIONAL_USD);
      setValue("max-quantity", values.TRADEHUB_MAX_QUANTITY);
      setValue("confirmation-ttl", values.TRADEHUB_CONFIRMATION_TTL_SECONDS);
      setValue("tiger-id", values.TIGEROPEN_TIGER_ID);
      setValue("tiger-account", values.TIGEROPEN_ACCOUNT);
      setValue("private-key-path", values.TIGEROPEN_PRIVATE_KEY_PATH);
      setValue("telegram-chats", values.TELEGRAM_ALLOWED_CHAT_IDS);
      setValue("mcp-command", data.mcp_command);
      setValue("mcp-config-path", data.mcp_config_path);
    }

    async function loadStatus() {
      try {
        const data = await request("/setup/status");
        applyStatus(data);
      } catch (error) {
        setMessage(error.message, "error");
      }
    }

    $("env-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      setMessage("Saving .env");
      try {
        const data = await request("/setup/env", {
          method: "POST",
          body: JSON.stringify(payload())
        });
        setValue("api-token", "");
        setValue("private-key", "");
        setValue("telegram-token", "");
        setChecked("clear-private-key", false);
        setChecked("clear-telegram-token", false);
        setMessage(data.generated_api_token ? "Saved .env with a generated token" : "Saved .env", "success");
        await loadStatus();
      } catch (error) {
        setMessage(error.message, "error");
      }
    });

    $("write-mcp").addEventListener("click", async () => {
      setMessage("Writing MCP config");
      try {
        const data = await request("/setup/mcp-config", {
          method: "POST",
          body: JSON.stringify({
            config_path: value("mcp-config-path"),
            command: value("mcp-command")
          })
        });
        const backup = data.backup_path ? " Backup: " + data.backup_path : "";
        setMessage("Updated " + data.config_path + backup, "success");
      } catch (error) {
        setMessage(error.message, "error");
      }
    });

    $("show-mcp").addEventListener("click", async () => {
      setMessage("");
      try {
        const data = await request("/setup/mcp-snippet?command=" + encodeURIComponent(value("mcp-command")));
        const block = $("mcp-json");
        block.hidden = false;
        block.textContent = JSON.stringify(data, null, 2);
      } catch (error) {
        setMessage(error.message, "error");
      }
    });

    loadStatus();
  </script>
</body>
</html>
"""
