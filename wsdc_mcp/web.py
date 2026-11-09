"""Public web pages served next to the MCP endpoint.

The landing page is the human- and crawler-facing description of the server (SEO and AI
answer engines index it); the discovery documents let MCP registries and clients find the
endpoint automatically.
"""

from __future__ import annotations

import html
import json
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from pathlib import Path

from . import __version__
from .config import Config
from .rules import RulesIndex
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
STATIC_FILES = {
    "/icon.png": ("icon-256.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/og.jpg": ("og.jpg", "image/jpeg"),
}

EXAMPLE_QUESTIONS = [
    "What divisions can I dance as a follower? My name is <your name>.",
    "How many more Intermediate points do I need to move up? My WSDC ID is <your ID>.",
    "Show the Champions results of the latest Swing Diego.",
    "Who moved up to All-Stars in Germany in the last 3 months?",
    "How many active Advanced leaders are there in France?",
    "How many points does 1st place earn in a Novice Jack & Jill with 45 followers?",
]

FAQ = [
    (
        "What is the WSDC Points Registry?",
        "The World Swing Dance Council (WSDC) records the results of West Coast Swing Jack &amp; Jill "
        "contests held at WSDC Registry Events. Points earned for placements determine which skill "
        "division (Newcomer, Novice, Intermediate, Advanced, All-Stars, Champions) a dancer may "
        "compete in, separately for the Leader and Follower roles.",
    ),
    (
        "How are WSDC points earned?",
        "By placing in a WSDC Jack &amp; Jill contest. Points depend on the placement and on the "
        "contest tier, which is set by the number of competitors in your role: under the 2026.1B "
        "rulebook, 1st place earns 3 points in a Tier 1 contest (5–10 competitors) and up to 25 "
        "points in a Tier 6 contest (130+ competitors). Larger contests also give points to other finalists.",
    ),
    (
        "How many points do I need to move up a division?",
        "Per the WSDC advancement chart (rulebook 2026.1B, Chart 3): Novice dancers may move up with "
        "16 Novice points and must move up with 30; Intermediate 30 / 45; Advanced 60 / 90; All-Stars "
        "150 / 225. A single point in the next division up also requires moving up. Champions are allowed "
        "with 1 Champions point and required with 10. A secondary role has its own chart. Ask the "
        "server's <code>search_wsdc_rules</code> tool for the exact wording.",
    ),
    (
        "How do I find my WSDC ID?",
        "Ask your AI assistant to search for your name: the <code>search_dancers</code> tool accepts "
        "names in any order, partial names, misspellings and Cyrillic. A WSDC ID is assigned once you "
        "earn your first point.",
    ),
    (
        "Is this the official WSDC registry?",
        'No. It is an independent, unofficial mirror of the public WSDC Points Registry, not affiliated '
        'with the World Swing Dance Council. The official source is '
        '<a href="https://worldsdc.com/registry/">worldsdc.com</a>.',
    ),
    (
        "How up to date is the data?",
        "The mirror is synchronised from the public registry continuously; new results usually appear "
        "within about an hour of being published officially.",
    ),
    (
        "Is it free? Do I need an API key?",
        "Free, no sign-up and no API key. The server is read-only and rate-limited for fair use.",
    ),
    (
        "Which AI assistants can use it?",
        "Any client that supports remote MCP servers over Streamable HTTP: Claude (web, desktop and "
        "Claude Code), ChatGPT (custom connectors / developer mode), Cursor, VS Code with GitHub "
        "Copilot, Windsurf, Zed and others.",
    ),
]


def _strip_tags(s: str) -> str:
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", s))


class WebPages:
    def __init__(self, mcp: MCPServer, config: Config, rules: RulesIndex | None):
        self.mcp = mcp
        self.config = config
        self.rules = rules
        self._tools_cache: tuple[float, list[dict[str, str]]] | None = None

    # -- helpers -----------------------------------------------------------

    async def tools(self) -> list[dict[str, str]]:
        if self._tools_cache and self._tools_cache[0] > time.monotonic():
            return self._tools_cache[1]
        out = []
        for t in await self.mcp.list_tools():
            desc = " ".join((t.description or "").split("\n\n")[0].split())
            out.append({"name": t.name, "title": t.title or t.name, "description": desc})
        self._tools_cache = (time.monotonic() + 300, out)
        return out

    def server_card(self) -> dict[str, Any]:
        c = self.config
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

    # -- routes ------------------------------------------------------------

    def register(self, mcp: MCPServer) -> None:
        routes = [
            ("/", self.landing),
            ("/llms.txt", self.llms_txt),
            ("/robots.txt", self.robots),
            ("/sitemap.xml", self.sitemap),
            ("/privacy", self.privacy),
            ("/icon.svg", self.icon),
            *((path, self.static) for path in STATIC_FILES),
            ("/favicon.ico", self.favicon),
            ("/health", self.health),
            ("/mcp/server-card", self.server_card_route),
            ("/.well-known/mcp/server-card.json", self.server_card_route),
            ("/.well-known/ai-catalog.json", self.ai_catalog),
            ("/.well-known/mcp-registry-auth", self.registry_auth),
        ]
        if self.config.indexnow_key:
            routes.append((f"/{self.config.indexnow_key}.txt", self.indexnow_key))
        for path, handler in routes:
            mcp.custom_route(path, methods=["GET", "HEAD"], include_in_schema=False)(handler)

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    async def icon(self, request: Request) -> Response:
        return Response(ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})

    async def static(self, request: Request) -> Response:
        name, media_type = STATIC_FILES[request.url.path]
        return Response(
            (STATIC_DIR / name).read_bytes(), media_type=media_type, headers={"Cache-Control": "public, max-age=604800"}
        )

    async def favicon(self, request: Request) -> Response:
        return RedirectResponse("/icon.svg", status_code=301)

    async def server_card_route(self, request: Request) -> Response:
        return JSONResponse(
            self.server_card(),
            media_type="application/mcp-server-card+json",
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
        )

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
        return JSONResponse(
            body,
            media_type="application/ai-catalog+json",
            headers={"Cache-Control": "public, max-age=3600", "Access-Control-Allow-Origin": "*"},
        )

    async def registry_auth(self, request: Request) -> Response:
        if not self.config.registry_auth:
            return PlainTextResponse("Not found", status_code=404)
        return PlainTextResponse(self.config.registry_auth)

    async def indexnow_key(self, request: Request) -> Response:
        return PlainTextResponse(self.config.indexnow_key or "")

    async def robots(self, request: Request) -> Response:
        body = f"User-agent: *\nAllow: /\n\nSitemap: {self.config.public_url}/sitemap.xml\n"
        return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=86400"})

    async def sitemap(self, request: Request) -> Response:
        urls = [f"{self.config.public_url}/", f"{self.config.public_url}/llms.txt", f"{self.config.public_url}/privacy"]
        items = "".join(f"<url><loc>{html.escape(u)}</loc></url>" for u in urls)
        body = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
        return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=86400"})

    async def llms_txt(self, request: Request) -> Response:
        c = self.config
        tools = "\n".join(f"- `{t['name']}`: {t['description']}" for t in await self.tools())
        faq = "\n\n".join(f"### {q}\n{_strip_tags(a)}" for q, a in FAQ)
        body = f"""# WSDC Points MCP Server

> Free, public, read-only Model Context Protocol (MCP) server for the West Coast Swing WSDC Points Registry: look up any dancer's WSDC points, current division per role (leader/follower), allowed Jack & Jill divisions, level-up history, event results and the official WSDC rules from Claude, ChatGPT, Cursor or any MCP client.

- MCP endpoint (Streamable HTTP, no authentication): {c.mcp_url}
- Server card: {c.mcp_url}/server-card
- Website: {c.public_url}/
- Source code: {c.repo_url}
- Unofficial; not affiliated with the World Swing Dance Council. Official registry: https://worldsdc.com/registry/

## Connect

- Claude Code: `claude mcp add --transport http wsdc-points {c.mcp_url}`
- Claude (web/desktop): add a custom connector with the URL {c.mcp_url}
- Cursor / Windsurf / other clients: `{{"mcpServers": {{"wsdc-points": {{"url": "{c.mcp_url}"}}}}}}`
- VS Code: `{{"servers": {{"wsdc-points": {{"type": "http", "url": "{c.mcp_url}"}}}}}}`

## Tools

{tools}

## FAQ

{faq}
"""
        return PlainTextResponse(body, media_type="text/markdown; charset=utf-8", headers={"Cache-Control": "public, max-age=3600"})

    async def privacy(self, request: Request) -> Response:
        c = self.config
        contact = (
            f'<a href="mailto:{html.escape(c.contact_email)}">{html.escape(c.contact_email)}</a> or '
            if c.contact_email
            else ""
        )
        body = f"""
<h1>Privacy policy</h1>
<p class="muted">Last updated: November 2026</p>
<h2>What this service is</h2>
<p>{html.escape(c.public_host)} runs a free, read-only MCP server and this website. It republishes
data from the public WSDC Points Registry: dancer names, WSDC IDs, countries, competition results,
points and divisions. It is not affiliated with the World Swing Dance Council.</p>
<h2>What we collect</h2>
<ul>
<li>No accounts, no cookies, no analytics or advertising trackers.</li>
<li>For operations and abuse prevention, the server logs each request (time, IP address, user agent)
and each MCP tool call (tool name, its arguments such as a searched name or WSDC ID, and how long it took).</li>
<li>Logs are kept for up to 30 days and are not sold or shared.</li>
</ul>
<h2>Personal data in results</h2>
<p>Tool results contain the personal data that the WSDC publishes in its registry (names, country,
competition history). The authoritative copy lives at <a href="https://worldsdc.com/registry/">worldsdc.com</a>;
corrections should be requested from the WSDC, and are reflected here after the next sync.
To ask about this mirror, contact {contact}<a href="{html.escape(c.repo_url)}/issues">open an issue on GitHub</a>.</p>
"""
        return HTMLResponse(self._page("Privacy policy · WCS Points", "Privacy policy of the WSDC Points MCP server.", "/privacy", body))

    async def landing(self, request: Request) -> Response:
        c = self.config
        tools = await self.tools()
        tool_rows = "".join(
            f"<tr><td><code>{html.escape(t['name'])}</code></td><td><strong>{html.escape(t['title'])}</strong><br>"
            f"{html.escape(t['description'])}</td></tr>"
            for t in tools
        )
        examples = "".join(f"<li>“{html.escape(q)}”</li>" for q in EXAMPLE_QUESTIONS)
        faq_html = "".join(f"<details><summary>{html.escape(q)}</summary><p>{a}</p></details>" for q, a in FAQ)
        mcp_url = html.escape(c.mcp_url)
        claude_code = f"claude mcp add --transport http wsdc-points {c.mcp_url}"
        cursor_json = json.dumps({"mcpServers": {"wsdc-points": {"url": c.mcp_url}}}, indent=2)
        vscode_json = json.dumps({"servers": {"wsdc-points": {"type": "http", "url": c.mcp_url}}}, indent=2)
        rules_note = f" (rulebook {html.escape(self.rules.version)})" if self.rules else ""
        body = f"""
<header class="hero">
  <img src="/icon.svg" width="56" height="56" alt="WCS Points logo">
  <p class="brand">WCS Points</p>
  <h1>WSDC Points MCP Server</h1>
  <p class="lead">Ask Claude, ChatGPT, Cursor or any AI assistant about West Coast Swing dancers:
  WSDC points, divisions per role, level-ups, event results and the official rules{rules_note}.</p>
  <div class="endpoint"><code id="url">{mcp_url}</code><button type="button" data-copy="{mcp_url}">Copy</button></div>
  <p class="badges"><span>Free</span><span>No sign-up</span><span>Read-only</span><span>~27,000 dancers</span><span>~8,000 events</span></p>
</header>

<section>
  <h2>Ask things like</h2>
  <ul class="examples">{examples}</ul>
</section>

<section id="connect">
  <h2>Connect your AI assistant</h2>
  <div class="grid">
    <div class="card"><h3>Claude (web &amp; desktop)</h3>
      <p>Settings → Connectors → <em>Add custom connector</em>, name it “WSDC Points” and paste the URL:</p>
      <pre><code>{mcp_url}</code></pre></div>
    <div class="card"><h3>Claude Code</h3>
      <pre><code>{html.escape(claude_code)}</code></pre></div>
    <div class="card"><h3>ChatGPT</h3>
      <p>Enable developer mode for apps/connectors in Settings, then create a connector with the URL above (no authentication).</p></div>
    <div class="card"><h3>Cursor, Windsurf &amp; others</h3>
      <pre><code>{html.escape(cursor_json)}</code></pre></div>
    <div class="card"><h3>VS Code (GitHub Copilot)</h3>
      <p><code>.vscode/mcp.json</code>:</p>
      <pre><code>{html.escape(vscode_json)}</code></pre></div>
    <div class="card"><h3>Any MCP client</h3>
      <p>Transport: Streamable HTTP. Authentication: none. Server card:
      <a href="/mcp/server-card">/mcp/server-card</a>.</p></div>
  </div>
</section>

<section>
  <h2>Tools</h2>
  <table class="tools"><tbody>{tool_rows}</tbody></table>
</section>

<section>
  <h2>FAQ</h2>
  {faq_html}
</section>

<section>
  <h2>Prefer Telegram?</h2>
  <p>The same data powers <a href="https://t.me/wsdc_bot">@wsdc_bot</a>, a Telegram bot for WSDC points,
  level-up notifications for dancers you follow, and chat questions about the registry.</p>
</section>
"""
        return HTMLResponse(
            self._page(
                "WSDC Points MCP Server — West Coast Swing points for AI assistants | WCS Points",
                "Free MCP server for the WSDC Points Registry: look up any West Coast Swing dancer's "
                "points, division per role, level-ups, event results and WSDC rules from Claude, ChatGPT or Cursor.",
                "/",
                body,
                jsonld=self._jsonld(tools),
            ),
            headers={"Cache-Control": "public, max-age=600"},
        )

    def _jsonld(self, tools: list[dict[str, str]]) -> list[dict[str, Any]]:
        c = self.config
        return [
            {
                "@context": "https://schema.org",
                "@type": "SoftwareApplication",
                "name": "WSDC Points MCP Server",
                "alternateName": ["WCS Points", "WSDC Points MCP", SERVER_NAME],
                "applicationCategory": "DeveloperApplication",
                "applicationSubCategory": "Model Context Protocol server",
                "operatingSystem": "Any",
                "url": f"{c.public_url}/",
                "installUrl": c.mcp_url,
                "codeRepository": c.repo_url,
                "softwareVersion": __version__,
                "image": f"{c.public_url}/og.jpg",
                "description": SERVER_DESCRIPTION,
                "featureList": [t["title"] for t in tools],
                "keywords": "WSDC, West Coast Swing, WCS, points registry, Jack and Jill, dance divisions, MCP, Model Context Protocol, Claude, ChatGPT",
                "isAccessibleForFree": True,
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
            },
            {
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": _strip_tags(a)}}
                    for q, a in FAQ
                ],
            },
        ]

    def _page(self, title: str, description: str, path: str, body: str, jsonld: list[dict[str, Any]] | None = None) -> str:
        c = self.config
        canonical = f"{c.public_url}{path}"
        ld = "".join(
            f'<script type="application/ld+json">{json.dumps(item, ensure_ascii=False).replace("</", "<\\/")}</script>'
            for item in (jsonld or [])
        )
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="canonical" href="{html.escape(canonical)}">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="alternate" type="text/markdown" href="/llms.txt" title="LLM-friendly summary">
<meta name="theme-color" content="#7c3aed">
<meta property="og:type" content="website">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<meta property="og:url" content="{html.escape(canonical)}">
<meta property="og:site_name" content="WCS Points">
<meta property="og:image" content="{html.escape(c.public_url)}/og.jpg">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
{ld}
<style>
:root {{ --bg:#fbfaff; --fg:#1d1b26; --muted:#5f5b6e; --card:#ffffff; --line:#e6e2f0; --accent:#7c3aed; --code:#f3f0fb; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#14121b; --fg:#ece9f5; --muted:#a6a1b8; --card:#1d1a27; --line:#2e2a3c; --accent:#a78bfa; --code:#231f30; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }}
a {{ color: var(--accent); }}
h1 {{ font-size: clamp(28px, 5vw, 40px); line-height:1.15; margin: 12px 0 8px; }}
h2 {{ font-size: 22px; margin: 44px 0 12px; }}
h3 {{ font-size: 16px; margin: 0 0 8px; }}
.hero {{ text-align:center; padding-top: 16px; }}
.brand {{ margin: 8px 0 0; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; font-size: 13px; color: var(--muted); }}
.lead {{ font-size: 18px; color: var(--muted); max-width: 680px; margin: 0 auto 20px; }}
.endpoint {{ display:inline-flex; gap:8px; align-items:center; max-width:100%; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:8px 8px 8px 14px; }}
.endpoint code {{ overflow-wrap:anywhere; font-size: 15px; }}
button {{ font:inherit; background:var(--accent); color:#fff; border:0; border-radius:8px; padding:6px 14px; cursor:pointer; }}
.badges span {{ display:inline-block; margin:4px; padding:2px 10px; border:1px solid var(--line); border-radius:999px; font-size:14px; color:var(--muted); }}
.examples li {{ margin: 4px 0; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:16px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; min-width:0; }}
.card p {{ margin: 0 0 8px; color: var(--muted); }}
pre {{ margin:0; background:var(--code); border-radius:8px; padding:10px 12px; overflow-x:auto; font-size:13px; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
table.tools {{ width:100%; border-collapse: collapse; }}
table.tools td {{ border-top:1px solid var(--line); padding:10px 8px; vertical-align: top; }}
table.tools td:first-child {{ white-space: nowrap; }}
details {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 16px; margin:8px 0; }}
summary {{ cursor:pointer; font-weight:600; }}
.muted, footer {{ color: var(--muted); }}
footer {{ border-top:1px solid var(--line); margin-top:56px; padding-top:16px; font-size:14px; }}
@media (max-width: 560px) {{ table.tools td:first-child {{ white-space: normal; }} }}
</style>
</head>
<body>
<main>
{body}
<footer>
  <p>Unofficial project, not affiliated with the World Swing Dance Council. Data from the public
  <a href="https://worldsdc.com/registry/">WSDC Points Registry</a>.</p>
  <p><a href="/">Home</a> · <a href="{html.escape(c.repo_url)}">Source on GitHub</a> · <a href="/llms.txt">llms.txt</a> ·
  <a href="/mcp/server-card">Server card</a> · <a href="/privacy">Privacy</a> · v{__version__}</p>
</footer>
</main>
<script>
document.querySelectorAll("[data-copy]").forEach(function (b) {{
  b.addEventListener("click", function () {{
    try {{ navigator.clipboard.writeText(b.getAttribute("data-copy")); b.textContent = "Copied"; }} catch (e) {{}}
    setTimeout(function () {{ b.textContent = "Copy"; }}, 1500);
  }});
}});
</script>
</body>
</html>
"""
