"""ASGI application: the MCP endpoint (/mcp) plus the public website, in one process."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Config, load_config
from .db import Database
from .rules import RulesIndex
from .server import build_server
from .web import WebPages

logger = logging.getLogger("wsdc_mcp")


class McpGetGuard:
    """Answer GET /mcp without opening a server-to-client SSE stream.

    In stateless mode the server never pushes messages, so the standalone GET stream would
    just hold a connection open forever. The spec allows answering 405 instead; people who
    open the URL in a browser are sent to the landing page.
    """

    def __init__(self, app: ASGIApp, path: str = "/mcp"):
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"].rstrip("/") == self.path:
            accept = Headers(scope=scope).get("accept", "")
            if "text/html" in accept:
                response: Response = RedirectResponse("/#connect", status_code=302)
            else:
                response = PlainTextResponse(
                    "This MCP endpoint accepts POST (Streamable HTTP, stateless). See /llms.txt.",
                    status_code=405,
                    headers={"Allow": "POST, OPTIONS"},
                )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(config: Config | None = None, db: Database | None = None, rules: RulesIndex | None = None) -> Starlette:
    config = config or load_config()
    if db is None and config.db_dsn:
        db = Database(config.db_dsn, max_size=config.pool_max_size, statement_timeout_ms=config.statement_timeout_ms)
    if db is None:
        logger.warning("No database DSN configured (WSDC_MCP_DB_DSN); data tools will return errors.")
    if rules is None and config.rules_path:
        if config.rules_path.is_file():
            rules = RulesIndex.from_file(config.rules_path, config.rules_version)
        else:
            logger.warning("Rules file %s not found; search_wsdc_rules is disabled.", config.rules_path)

    mcp = build_server(config, db, rules)
    WebPages(mcp, config, rules).register(mcp)

    app = mcp.streamable_http_app(
        # Stateless JSON responses: every request is independent, so the server can be restarted
        # or scaled without breaking clients, and no session state is kept in memory.
        stateless_http=True,
        json_response=True,
        # This is a public, unauthenticated, read-only service behind a reverse proxy, so
        # Host/Origin pinning (a defence for servers on localhost) is not applicable.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=config.host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if db is not None:
            await db.open()
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            if db is not None:
                await db.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(McpGetGuard)
    # Browser-based MCP clients and inspectors call the endpoint cross-origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "Mcp-Protocol-Version"],
        max_age=86400,
    )
    return app
