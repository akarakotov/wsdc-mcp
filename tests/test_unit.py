"""Tests that need neither a database nor network."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx2 as httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from wsdc_mcp import data, usage
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
            "get_recent_levelups", "get_division_stats", "search_wsdc_rules", "get_registry_info",
            "get_jj_partners", "get_event_history", "get_registry_updates"} == names
    for t in tools:
        assert t.title, t.name
        assert t.description and len(t.description) > 40, t.name
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert t.output_schema, t.name
    assert missing_db.is_error and "not configured" in missing_db.content[0].text
    assert not rules.is_error and rules.structured_content["version"] == "2026.1B"


# -- HTTP surface ------------------------------------------------------------


def _http(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://wcspoints.test")


def _get_all(app, paths, **headers):
    async def run():
        async with _http(app) as http:
            return {p: await http.get(p, headers=headers) for p in paths}

    return asyncio.run(run())


def test_discovery_documents(config):
    app = create_app(make_config(registry_auth="v=MCPv1; k=ed25519; p=abc"), rules=RulesIndex(SAMPLE_RULES, "2026.1B"))
    r = _get_all(app, [
        "/health", "/mcp/server-card", "/.well-known/mcp/server-card.json", "/.well-known/ai-catalog.json",
        "/.well-known/mcp-registry-auth", "/icon.svg",
    ])
    assert all(resp.status_code == 200 for resp in r.values()), {p: x.status_code for p, x in r.items()}
    card = r["/mcp/server-card"].json()
    assert r["/mcp/server-card"].headers["content-type"].startswith("application/mcp-server-card+json")
    assert card["remotes"][0] == {**card["remotes"][0], "type": "streamable-http", "url": "https://wcspoints.test/mcp"}
    assert card["$schema"].endswith("/schemas/v1/server-card.schema.json")
    assert r["/.well-known/mcp/server-card.json"].json() == card
    catalog = r["/.well-known/ai-catalog.json"].json()
    assert catalog["entries"][0]["url"] == "https://wcspoints.test/mcp/server-card"
    assert r["/.well-known/mcp-registry-auth"].text == "v=MCPv1; k=ed25519; p=abc"


def test_registry_auth_absent_is_404(config):
    assert _get_all(create_app(config), ["/.well-known/mcp-registry-auth"])["/.well-known/mcp-registry-auth"].status_code == 404


def test_mcp_only_app_serves_no_website(config):
    r = _get_all(create_app(config), ["/", "/sitemap.xml", "/dancer/421"])
    assert all(resp.status_code == 404 for resp in r.values()), {p: x.status_code for p, x in r.items()}


def test_get_on_mcp_endpoint_does_not_hang(config):
    async def run(app):
        async with _http(app) as http:
            plain = await asyncio.wait_for(http.get("/mcp", headers={"Accept": "text/event-stream"}), 5)
            browser = await asyncio.wait_for(http.get("/mcp", headers={"Accept": "text/html"}), 5)
            preflight = await http.options(
                "/mcp",
                headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST",
                         "Access-Control-Request-Headers": "content-type"},
            )
            return plain, browser, preflight

    plain, browser, preflight = asyncio.run(run(create_app(config)))
    assert plain.status_code == 405 and "POST" in plain.headers["allow"]
    assert browser.status_code == 405
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == "*"
    _, browser, _ = asyncio.run(run(create_app(config, browser_url="/#connect")))
    assert browser.status_code == 302 and browser.headers["location"] == "/#connect"


def _usage_lines(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "wsdc_mcp.usage"]


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
def test_usage_log_names_client_and_tool(config, caplog, mode):
    caplog.set_level(logging.INFO, logger="wsdc_mcp.usage")
    app = create_app(config, rules=RulesIndex(SAMPLE_RULES, "2026.1B"))

    async def run():
        async with app.router.lifespan_context(app), _http(app) as http:
            transport = streamable_http_client("https://wcspoints.test/mcp?via=Test", http_client=http)
            info = Implementation(name="test-client", version="9.9")
            async with Client(transport, client_info=info, mode=mode) as client:
                rules = await client.call_tool("search_wsdc_rules", {"query": "novice points"})
                missing = await client.call_tool("get_dancer_profile", {"wsdc_id": 1})
                return rules, missing

    rules, missing = asyncio.run(run())
    assert not rules.is_error and missing.is_error
    lines = _usage_lines(caplog)
    calls = [line for line in lines if line["method"] == "tools/call"]
    assert [(c["tool"], c["tool_status"], c["status"]) for c in calls] == [
        ("search_wsdc_rules", "ok", 200), ("get_dancer_profile", "tool_error", 200),
    ]
    assert json.loads(calls[0]["args"]) == {"query": "novice points"}
    assert calls[0]["results"] >= 1 and "results" not in calls[1]  # what a successful call found
    named = [line for line in lines if line.get("client") == "test-client"]
    assert named and all(line["client_version"] == "9.9" for line in named)
    if mode == "legacy":
        assert any(line["method"] == "initialize" for line in named)
    else:
        assert all(line.get("client") == "test-client" for line in calls)
    assert all(line["ip"] and line["ts"].endswith("+00:00") and "ms" in line for line in lines)
    assert all(line["via"] == "test" for line in lines)  # the channel from the connector URL


def test_usage_outcome_never_fails_a_tool_call(caplog):
    from wsdc_mcp import server

    server._note_outcome(lambda result: {"subject": result.name}, object())  # no such attribute
    assert "usage outcome failed" in caplog.text


def test_usage_log_skips_other_requests_and_survives_junk(config, caplog):
    caplog.set_level(logging.INFO, logger="wsdc_mcp.usage")
    app = create_app(config)

    async def run():
        async with app.router.lifespan_context(app), _http(app) as http:
            await http.get("/mcp")
            await http.get("/health")
            return await http.post("/mcp", content=b"{not json", headers={
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "User-Agent": "junk/1.0"})

    junk = asyncio.run(run())
    lines = _usage_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["method"] == "invalid" and lines[0]["ua"] == "junk/1.0"
    assert lines[0]["status"] == junk.status_code >= 400


def test_usage_via_names_the_channel():
    assert usage.via(b"via=Reddit") == "reddit"
    assert usage.via(b"x=1&via=claude-directory") == "claude-directory"
    assert usage.via(b"via=%3Cscript%3E") is None and usage.via(b"") is None
    assert usage.via(("via=" + "a" * 41).encode()) is None


def test_usage_describe_handles_batches_and_garbage():
    assert usage.describe(b'[{"jsonrpc":"2.0","method":"tools/list","id":1},{"jsonrpc":"2.0","result":{},"id":2}, 7]') == {
        "method": "tools/list,response,invalid"}
    assert usage.describe(b'{"method":"tools/call","params":{"name":"x","arguments":null,"_meta":[]}}') == {
        "method": "tools/call", "tool": "x", "args": "{}"}
    assert usage.describe(b'{"method":"initialize","params":{"clientInfo":"nope"}}') == {"method": "initialize"}
    assert usage.describe(b"null") == {"method": "invalid"}
    assert usage.describe(b'{"method":"prompts/get","params":{"name":"dancer_story","arguments":{}}}') == {
        "method": "prompts/get", "prompt": "dancer_story"}
    assert usage.describe(b'{"method":"completion/complete","params":{"ref":{"type":"ref/prompt","name":"event_story"}}}') == {
        "method": "completion/complete", "prompt": "event_story"}
    assert usage.describe(b'{"method":"resources/read","params":{"uri":"ui://wsdc/dancer-card.html"}}') == {
        "method": "resources/read", "resource": "ui://wsdc/dancer-card.html"}


def test_usage_log_to_file(tmp_path):
    path = tmp_path / "logs" / "usage.jsonl"
    try:
        usage.log_to_file(path)
        usage.logger.info('{"method":"tools/list"}')
    finally:
        for handler in list(usage.logger.handlers):
            handler.close()
            usage.logger.removeHandler(handler)
        usage.logger.propagate = True
    assert path.read_text() == '{"method":"tools/list"}\n'


def test_extensions_mount_extra_routes(config):
    from starlette.responses import PlainTextResponse

    seen = []

    def extension(mcp, cfg, db, rules):
        seen.append((cfg, db, rules))

        async def hello(request):
            return PlainTextResponse("hi")

        mcp.custom_route("/hello", methods=["GET"])(hello)

    app = create_app(config, extensions=[extension])
    assert seen == [(config, None, None)]
    assert _get_all(app, ["/hello"])["/hello"].text == "hi"


def test_icons_served(config):
    for path, resp in _get_all(create_app(config), ["/icon.png", "/icon-512.png"]).items():
        assert resp.status_code == 200 and resp.content[:4] == b"\x89PNG", path


def test_server_json_matches_code():
    import json
    from pathlib import Path

    from wsdc_mcp import __version__

    manifest = json.loads((Path(__file__).resolve().parents[1] / "server.json").read_text())
    assert manifest["name"] == SERVER_NAME
    assert manifest["description"] == SERVER_DESCRIPTION
    assert manifest["version"] == __version__
    assert manifest["remotes"] == [{"type": "streamable-http", "url": "https://wcspoints.com/mcp"}]


# -- prompts, completions, MCP Apps -------------------------------------------


def test_prompts_completions_and_the_dancer_card(config):
    from dataclasses import replace

    from mcp.types import PromptReference

    from wsdc_mcp.server import DANCER_CARD_URI

    async def run():
        mcp = build_server(replace(config, dancer_page_url="https://wcspoints.test/dancer/{id}"), db=None, rules=None)
        async with Client(mcp) as client:
            prompts = (await client.list_prompts()).prompts
            story = await client.get_prompt("event_story", {"event": "US Open", "division": "Champions"})
            divisions = await client.complete(PromptReference(name="event_story"), {"name": "division", "value": "A"})
            dancers = await client.complete(PromptReference(name="dancer_story"), {"name": "dancer", "value": "Jane"})
            tools = {t.name: t for t in (await client.list_tools()).tools}
            card = await client.read_resource(DANCER_CARD_URI)
            return client.server_capabilities, prompts, story, divisions, dancers, tools, card

    capabilities, prompts, story, divisions, dancers, tools, card = asyncio.run(run())
    assert {p.name for p in prompts} == {"divisions_check", "dancer_story", "event_story", "registry_news", "rules_question"}
    for p in prompts:
        assert p.title and p.description and p.arguments, p.name
        assert all(a.description for a in p.arguments), p.name
    text = story.messages[0].content.text
    assert "US Open" in text and "Champions division" in text and "get_event_history" in text
    assert capabilities.completions is not None
    assert divisions.completion.values == ["Advanced", "All-Stars"]
    assert dancers.completion.values == []  # no database here
    # MCP Apps: the profile tool points at the card, which is served with the Apps MIME type.
    assert "io.modelcontextprotocol/ui" in (capabilities.extensions or {})
    assert tools["get_dancer_profile"].meta == {"ui": {"resourceUri": DANCER_CARD_URI}}
    assert all(t.meta is None for name, t in tools.items() if name != "get_dancer_profile")
    html = card.contents[0]
    assert html.mime_type == "text/html;profile=mcp-app"
    assert '"https://wcspoints.test/dancer/{id}"' in html.text and "__DANCER_URL__" not in html.text
    assert "ui/initialize" in html.text and "ui/notifications/tool-result" in html.text


def test_dancer_card_without_a_website_has_no_link():
    from wsdc_mcp.server import dancer_card_html

    assert "const DANCER_URL = \"\";" in dancer_card_html(None)
