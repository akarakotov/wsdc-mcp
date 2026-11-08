"""Tests that need neither a database nor network."""

from __future__ import annotations

import asyncio

import httpx2 as httpx
import pytest
from mcp import Client

from wsdc_mcp import data
from wsdc_mcp.app import create_app
from wsdc_mcp.db import cached
from wsdc_mcp.rules import RulesIndex
from wsdc_mcp.server import SERVER_DESCRIPTION, SERVER_NAME, build_server

from .conftest import RULES_FILE, make_config

SAMPLE_RULES = """\
Overview
The WSDC tracks Jack and Jill results.

3.2. Competitors: Skill Levels and Points Registry
Competitors move up based on points earned in their primary role.

Chart 3: WSDC Skill Level Jack and Jill Advancement Chart
Novice: allowed to move up with 16 Novice points, required with 30 Novice points.

Chart 5: Points Awarded per Tier
Tier 1: 1st place 3 points. Tier 6: 1st place 25 points.

3.4. Contest Oversight: Staff Requirements
Judges must not compete in contests they judge.
"""


# -- normalisation ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Каракотов", "karakotov"),
        ("  Jöhn   O'Neil ", "john o'neil"),
        ("Smith, John", "smith john"),
        ("Щукин Юрий", "shchukin yuriy"),
    ],
)
def test_normalize_name_query(raw, expected):
    assert data.normalize_name_query(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("allstars", "All-Stars"), ("All Stars", "All-Stars"), ("champs", "Champions"), ("NOV", "Novice"), (None, None), ("", None)],
)
def test_normalize_division(raw, expected):
    assert data.normalize_division(raw) == expected


def test_normalize_division_unknown_lists_valid_values():
    with pytest.raises(data.NotFound, match="Valid divisions: Newcomer"):
        data.normalize_division("pro-am")


@pytest.mark.parametrize("raw, expected", [("lead", "Leader"), ("Followers", "Follower"), (None, None)])
def test_normalize_role(raw, expected):
    assert data.normalize_role(raw) == expected


@pytest.mark.parametrize("raw, expected", [("1", "1st"), ("2", "2nd"), ("3", "3rd"), ("5", "5th"), ("F", "Finalist"), (None, "Unknown")])
def test_placement_labels(raw, expected):
    assert data._placement(raw) == expected


def test_like_escapes_wildcards():
    assert data._like("50%_x") == "%50\\%\\_x%"


def test_display_name_from_last_first():
    assert data._display_name(None, None, "Doe, Jane") == "Jane Doe"
    assert data._display_name("Jane", "Doe", "ignored") == "Jane Doe"


def test_role_status_reads_db_fields_without_recomputing():
    row = {
        "leader_available_novice": True,
        "leader_available_intermediate": True,
        "leader_current_division_points": 7,
        "leader_novice_points": 18,
        "leader_intermediate_points": 7,
        "leader_entered_intermediate": __import__("datetime").datetime(2024, 5, 1),
    }
    status = data._role_status(row, "Leader")
    assert status.division == "Intermediate"
    assert status.allowed_divisions == ["Novice", "Intermediate"]
    assert status.points_by_division == {"Novice": 18, "Intermediate": 7}
    assert [(lu.division, str(lu.date), lu.points) for lu in status.levelups] == [("Intermediate", "2024-05-01", 7)]
    assert status.has_points


# -- cache -----------------------------------------------------------------


def test_cached_dedupes_concurrent_calls():
    calls = 0

    @cached(ttl=60)
    async def slow(db, x):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return x * 2

    async def run():
        results = await asyncio.gather(*(slow(object(), 21) for _ in range(5)))
        again = await slow(object(), 21)
        return results, again

    results, again = asyncio.run(run())
    assert results == [42] * 5 and again == 42
    assert calls == 1


# -- rules -----------------------------------------------------------------


def test_rules_sections_and_search():
    idx = RulesIndex(SAMPLE_RULES, "2026.1B")
    assert idx.headings[:2] == ["Overview", "3.2. Competitors: Skill Levels and Points Registry"]
    assert idx.search("how many points to move up from novice")[0].heading.startswith("Chart 3")
    assert idx.search("points for first place by tier")[0].heading.startswith("Chart 5")
    assert idx.search("the a of") == []


@pytest.mark.skipif(not RULES_FILE.is_file(), reason="rulebook text not available")
def test_real_rulebook_finds_advancement_chart():
    idx = RulesIndex.from_file(RULES_FILE)
    assert idx.version == "2026.1B"
    top = [s.heading for s in idx.search("points required to move up from novice", 3)]
    assert any("Chart 3" in h for h in top)


# -- MCP surface -------------------------------------------------------------


def test_registry_metadata_limits():
    # MCP Registry / Server Card: description <= 100 chars, name '<namespace>/<name>'.
    assert len(SERVER_DESCRIPTION) <= 100
    assert SERVER_NAME.count("/") == 1


def test_tools_are_titled_read_only_and_structured(config):
    async def run():
        mcp = build_server(config, db=None, rules=RulesIndex(SAMPLE_RULES, "2026.1B"))
        async with Client(mcp) as client:
            tools = (await client.list_tools()).tools
            missing_db = await client.call_tool("get_dancer_profile", {"wsdc_id": 1})
            rules = await client.call_tool("search_wsdc_rules", {"query": "novice points"})
            return tools, missing_db, rules

    tools, missing_db, rules = asyncio.run(run())
    names = {t.name for t in tools}
    assert {"search_dancers", "get_dancer_profile", "get_dancer_results", "search_events", "get_event_results",
            "get_recent_levelups", "get_division_stats", "search_wsdc_rules", "get_registry_info"} == names
    for t in tools:
        assert t.title, t.name
        assert t.description and len(t.description) > 40, t.name
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.output_schema, t.name
    assert missing_db.is_error and "not configured" in missing_db.content[0].text
    assert not rules.is_error and rules.structured_content["version"] == "2026.1B"


# -- HTTP surface ------------------------------------------------------------


def _http(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://wsdcpoints.test")


def test_web_routes_and_discovery(config):
    app = create_app(make_config(registry_auth="v=MCPv1; k=ed25519; p=abc"), rules=RulesIndex(SAMPLE_RULES, "2026.1B"))

    async def run():
        async with _http(app) as http:
            return {p: await http.get(p) for p in [
                "/", "/llms.txt", "/robots.txt", "/sitemap.xml", "/privacy", "/icon.svg", "/health",
                "/mcp/server-card", "/.well-known/mcp/server-card.json", "/.well-known/ai-catalog.json",
                "/.well-known/mcp-registry-auth",
            ]}

    r = asyncio.run(run())
    assert all(resp.status_code == 200 for resp in r.values()), {p: x.status_code for p, x in r.items()}
    landing = r["/"].text
    assert "<title>WSDC Points MCP Server" in landing
    assert 'rel="canonical" href="https://wsdcpoints.test/"' in landing
    assert '"@type": "FAQPage"' in landing and "search_wsdc_rules" in landing
    assert "https://wsdcpoints.test/mcp" in r["/llms.txt"].text
    card = r["/mcp/server-card"].json()
    assert r["/mcp/server-card"].headers["content-type"].startswith("application/mcp-server-card+json")
    assert card["remotes"][0] == {**card["remotes"][0], "type": "streamable-http", "url": "https://wsdcpoints.test/mcp"}
    assert card["$schema"].endswith("/schemas/v1/server-card.schema.json")
    catalog = r["/.well-known/ai-catalog.json"].json()
    assert catalog["entries"][0]["url"] == "https://wsdcpoints.test/mcp/server-card"
    assert r["/.well-known/mcp-registry-auth"].text == "v=MCPv1; k=ed25519; p=abc"


def test_registry_auth_absent_is_404(config):
    app = create_app(config)

    async def run():
        async with _http(app) as http:
            return await http.get("/.well-known/mcp-registry-auth")

    assert asyncio.run(run()).status_code == 404


def test_get_on_mcp_endpoint_does_not_hang(config):
    app = create_app(config)

    async def run():
        async with _http(app) as http:
            plain = await asyncio.wait_for(http.get("/mcp", headers={"Accept": "text/event-stream"}), 5)
            browser = await asyncio.wait_for(http.get("/mcp", headers={"Accept": "text/html"}), 5)
            preflight = await http.options(
                "/mcp",
                headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST",
                         "Access-Control-Request-Headers": "content-type"},
            )
            return plain, browser, preflight

    plain, browser, preflight = asyncio.run(run())
    assert plain.status_code == 405 and "POST" in plain.headers["allow"]
    assert browser.status_code == 302 and browser.headers["location"] == "/#connect"
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == "*"


def test_static_assets_served(config):
    app = create_app(config)

    async def run():
        async with _http(app) as http:
            return {p: await http.get(p) for p in ["/icon.png", "/icon-512.png", "/og.jpg"]}

    for path, resp in asyncio.run(run()).items():
        assert resp.status_code == 200, path
        assert resp.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb"), path


def test_server_json_matches_code():
    import json
    from pathlib import Path

    from wsdc_mcp import __version__

    manifest = json.loads((Path(__file__).resolve().parents[1] / "server.json").read_text())
    assert manifest["name"] == SERVER_NAME
    assert manifest["description"] == SERVER_DESCRIPTION
    assert manifest["version"] == __version__
    assert manifest["remotes"] == [{"type": "streamable-http", "url": "https://wsdcpoints.com/mcp"}]
