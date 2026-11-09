"""Discovery documents served next to the MCP endpoint.

They let MCP registries and clients find and describe the server: the server card, the AI
catalog, the MCP Registry domain proof, the icons the card points to, and a health check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import __version__
from .config import Config
from .server import SERVER_DESCRIPTION, SERVER_NAME, SERVER_TITLE

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#7c3aed"/><stop offset="1" stop-color="#db2777"/></linearGradient></defs>
<rect width="64" height="64" rx="14" fill="url(#g)"/>
<path d="M14 44c6-16 12-24 18-24s12 8 18 24" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round"/>
<circle cx="32" cy="20" r="5" fill="#fff"/>
</svg>
"""

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICON_FILES = {
    "/icon.png": ("icon-256.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
LONG_CACHE = {"Cache-Control": "public, max-age=604800"}
CARD_HEADERS = {"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"}


def server_card(config: Config) -> dict[str, Any]:
    c = config
    return {
        "$schema": "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json",
        "name": SERVER_NAME,
        "title": SERVER_TITLE,
        "description": SERVER_DESCRIPTION,
        "version": __version__,
        "websiteUrl": f"{c.public_url}/",
        "repository": {"url": c.repo_url, "source": "github"},
        "icons": [
            {"src": f"{c.public_url}/icon-512.png", "mimeType": "image/png", "sizes": ["512x512"]},
            {"src": f"{c.public_url}/icon.svg", "mimeType": "image/svg+xml", "sizes": ["any"]},
        ],
        "remotes": [
            {
                "type": "streamable-http",
                "url": c.mcp_url,
                "supportedProtocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            }
        ],
    }


class Discovery:
    def __init__(self, config: Config):
        self.config = config

    def register(self, mcp: MCPServer) -> None:
        routes = [
            ("/health", self.health),
            ("/mcp/server-card", self.server_card),
            ("/.well-known/mcp/server-card.json", self.server_card),
            ("/.well-known/ai-catalog.json", self.ai_catalog),
            ("/.well-known/mcp-registry-auth", self.registry_auth),
            ("/.well-known/glama.json", self.glama_claim),
            ("/icon.svg", self.icon),
            *((path, self.icon_file) for path in ICON_FILES),
        ]
        for path, handler in routes:
            mcp.custom_route(path, methods=["GET", "HEAD"], include_in_schema=False)(handler)

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    async def server_card(self, request: Request) -> Response:
        return JSONResponse(server_card(self.config), media_type="application/mcp-server-card+json", headers=CARD_HEADERS)

    async def ai_catalog(self, request: Request) -> Response:
        c = self.config
        body = {
            "specVersion": "1.0",
            "entries": [
                {
                    "identifier": f"urn:air:{c.public_host}:mcp:wsdc-points",
                    "type": "application/mcp-server-card+json",
                    "url": f"{c.mcp_url}/server-card",
                }
            ],
        }
        return JSONResponse(body, media_type="application/ai-catalog+json", headers=CARD_HEADERS)

    async def registry_auth(self, request: Request) -> Response:
        if not self.config.registry_auth:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(self.config.registry_auth)

    async def glama_claim(self, request: Request) -> Response:
        """Glama's proof that whoever owns this domain owns its connectors (glama.ai/mcp/connectors)."""
        if not self.config.glama_claim:
            return PlainTextResponse("Not found", status_code=404)
        return JSONResponse({"$schema": "https://glama.ai/mcp/schemas/connector.json", "claim": self.config.glama_claim})

    async def icon(self, request: Request) -> Response:
        return Response(ICON_SVG, media_type="image/svg+xml", headers=LONG_CACHE)

    async def icon_file(self, request: Request) -> Response:
        name, media_type = ICON_FILES[request.url.path]
        return Response((STATIC_DIR / name).read_bytes(), media_type=media_type, headers=LONG_CACHE)
