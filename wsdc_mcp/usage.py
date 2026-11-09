"""Usage log for the MCP endpoint: one JSON line per POST to /mcp.

Each line records what was asked (the JSON-RPC method; the tool and its arguments for
tools/call; the prompt or resource for prompts/get, completion/complete and resources/read), who asked (the client app name and version sent with initialize, the User-Agent
and IP address) and how it went (HTTP status, tool status, duration, and for some tools how
many results they found or whose profile they returned). Lines go to the
`wsdc_mcp.usage` logger; `log_to_file` sends them to a file rotated daily at midnight UTC.
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("wsdc_mcp.usage")

# Bodies above this size are logged without parsing them (real MCP requests are far smaller).
MAX_BODY = 64 * 1024
MAX_ARGS = 300
MAX_TEXT = 100

_noted: ContextVar[dict[str, Any] | None] = ContextVar("wsdc_mcp_usage_noted", default=None)


def note(**fields: Any) -> None:
    """Add fields to the usage line of the request being handled, if any (used by tool calls)."""
    noted = _noted.get()
    if noted is not None:
        noted.update(fields)


def _text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) and value else None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# Up to protocol 2025-11-25 a client names itself once, in initialize; from 2026-07-28 on it
# sends its name and protocol version in the _meta of every request.
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"


def describe(body: bytes) -> dict[str, Any]:
    """What a JSON-RPC request body asks for: method(s), tool and arguments, client app."""
    try:
        payload = json.loads(body)
    except ValueError:
        return {"method": "invalid"}
    fields: dict[str, Any] = {"method": None}  # first key of the line; filled in below
    methods = []
    for message in payload if isinstance(payload, list) else [payload]:
        if not isinstance(message, dict):
            methods.append("invalid")
            continue
        if "method" not in message:
            methods.append("response" if "result" in message or "error" in message else "invalid")
            continue
        method = _text(message["method"]) or "invalid"
        methods.append(method)
        params = _object(message.get("params"))
        meta = _object(params.get("_meta"))
        if method == "initialize":
            info, protocol = _object(params.get("clientInfo")), params.get("protocolVersion")
        else:
            info, protocol = _object(meta.get(CLIENT_INFO_META_KEY)), meta.get(PROTOCOL_VERSION_META_KEY)
        if info:
            fields["client"] = _text(info.get("name"))
            fields["client_version"] = _text(info.get("version"))
        if _text(protocol):
            fields["protocol"] = _text(protocol)
        if method == "tools/call":
            fields["tool"] = _text(params.get("name"))
            fields["args"] = json.dumps(params.get("arguments") or {}, default=str, ensure_ascii=False)[:MAX_ARGS]
        elif method == "prompts/get":
            fields["prompt"] = _text(params.get("name"))
        elif method == "completion/complete":
            ref = _object(params.get("ref"))
            fields["prompt"] = _text(ref.get("name") or ref.get("uri"))
        elif method == "resources/read":
            fields["resource"] = _text(params.get("uri"))
    fields["method"] = ",".join(methods) or "invalid"
    return fields


class UsageLog:
    """ASGI middleware that writes one usage line for every POST to the MCP endpoint."""

    def __init__(self, app: ASGIApp, path: str = "/mcp"):
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        headers = Headers(scope=scope)
        client = scope.get("client")
        line: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ip": client[0] if client else None,
            "ua": headers.get("user-agent", "")[:300] or None,
        }
        body = bytearray()
        too_large = False
        status: int | None = None

        async def receive_and_keep() -> Message:
            nonlocal too_large
            message = await receive()
            if message["type"] == "http.request" and not too_large:
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > MAX_BODY:
                    too_large = True
                    body.clear()
                else:
                    body.extend(chunk)
            return message

        async def send_and_note(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        noted: dict[str, Any] = {}
        token = _noted.set(noted)
        try:
            await self.app(scope, receive_and_keep, send_and_note)
        except Exception:
            status = status or 500
            raise
        finally:
            _noted.reset(token)
            line.update({"method": "too_large"} if too_large else describe(bytes(body)))
            line.setdefault("protocol", _text(headers.get("mcp-protocol-version")))
            line.update(status=status, **noted, ms=round((time.perf_counter() - started) * 1000))
            logger.info(json.dumps({k: v for k, v in line.items() if v is not None}, ensure_ascii=False, separators=(",", ":")))


def log_to_file(path: Path, days: int = 30) -> None:
    """Send usage lines to `path` only, rotated at midnight UTC and kept for `days` days."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(path, when="midnight", utc=True, backupCount=days - 1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
