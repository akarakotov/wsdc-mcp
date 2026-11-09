"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


@dataclass(frozen=True)
class Config:
    # Read-only Postgres DSN. MCP_DB_DSN is accepted as a fallback so the service can
    # share the bot host's env file during rollout.
    db_dsn: str | None
    # Canonical public origin, used in the discovery documents.
    public_url: str
    host: str
    port: int
    # Path to the plain-text WSDC rules document (optional; rules tools are disabled without it).
    rules_path: Path | None
    rules_version: str | None
    pool_max_size: int
    statement_timeout_ms: int
    # Body for /.well-known/mcp-registry-auth (MCP Registry HTTP domain proof), if any.
    registry_auth: str | None
    repo_url: str
    log_level: str
    # File for the usage log (one JSON line per MCP request, rotated daily); unset = the process log.
    usage_log: Path | None = None

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def public_host(self) -> str:
        return self.public_url.split("://", 1)[-1].split("/", 1)[0]


def load_config() -> Config:
    rules_path = _env("WSDC_MCP_RULES_PATH")
    usage_log = _env("WSDC_MCP_USAGE_LOG")
    return Config(
        db_dsn=_env("WSDC_MCP_DB_DSN") or _env("MCP_DB_DSN"),
        public_url=(_env("WSDC_MCP_PUBLIC_URL", "https://wcspoints.com") or "").rstrip("/"),
        host=_env("WSDC_MCP_HOST", "127.0.0.1") or "127.0.0.1",
        port=int(_env("WSDC_MCP_PORT", "8001") or "8001"),
        rules_path=Path(rules_path) if rules_path else None,
        rules_version=_env("WSDC_MCP_RULES_VERSION"),
        pool_max_size=int(_env("WSDC_MCP_POOL_MAX", "3") or "3"),
        statement_timeout_ms=int(_env("WSDC_MCP_STATEMENT_TIMEOUT_MS", "8000") or "8000"),
        registry_auth=_env("WSDC_MCP_REGISTRY_AUTH"),
        repo_url=(_env("WSDC_MCP_REPO_URL", "https://github.com/akarakotov/wsdc-mcp") or "").rstrip("/"),
        log_level=(_env("WSDC_MCP_LOG_LEVEL", "INFO") or "INFO").upper(),
        usage_log=Path(usage_log) if usage_log else None,
    )
