"""End-to-end tests over real HTTP against a WSDC database.

Set WSDC_MCP_TEST_DSN (read-only access is enough) to run them, e.g.
    WSDC_MCP_TEST_DSN="dbname=wsdc user=postgres host=127.0.0.1" pytest
Assertions are structural, so they hold for any snapshot of the registry.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from mcp import Client

from wsdc_mcp.app import create_app

from .conftest import RULES_FILE, TEST_DSN, make_config

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="WSDC_MCP_TEST_DSN not set")


@pytest.fixture(scope="module")
def server_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = make_config(
        db_dsn=TEST_DSN, port=port, public_url=f"http://127.0.0.1:{port}",
        rules_path=RULES_FILE if RULES_FILE.is_file() else None,
    )
    server = uvicorn.Server(uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def call(url: str, tool: str, args: dict):
    async def run():
        async with Client(url) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(run())


def any_dancer(url: str) -> dict:
    """Pick an active dancer with role data from the database snapshot."""
    result = call(url, "get_recent_levelups", {"days": 730, "limit": 1})
    if result.structured_content and result.structured_content["levelups"]:
        return result.structured_content["levelups"][0]
    found = call(url, "search_dancers", {"query": "a", "limit": 1}).structured_content
    return found["dancers"][0]


def test_search_by_id_and_name_round_trip(server_url):
    dancer = any_dancer(server_url)
    by_id = call(server_url, "search_dancers", {"query": f"#{dancer['wsdc_id']}"}).structured_content
    assert by_id["match_type"] == "id" and by_id["dancers"][0]["wsdc_id"] == dancer["wsdc_id"]
    last_name = dancer["name"].split()[-1]
    by_name = call(server_url, "search_dancers", {"query": last_name, "limit": 25}).structured_content
    assert by_name["count"] >= 1
    assert all(last_name.lower() in d["name"].lower() for d in by_name["dancers"])


def test_fuzzy_search_and_no_match(server_url):
    none = call(server_url, "search_dancers", {"query": "qzxqzxqzx"}).structured_content
    assert none["count"] == 0


def test_profile_shape(server_url):
    dancer = any_dancer(server_url)
    profile = call(server_url, "get_dancer_profile", {"wsdc_id": dancer["wsdc_id"]})
    assert not profile.is_error, profile.content
    p = profile.structured_content
    assert p["wsdc_id"] == dancer["wsdc_id"] and p["name"]
    assert [r["role"] for r in p["roles"]] in (["Leader", "Follower"], ["Follower", "Leader"])
    primary = p["roles"][0]
    assert primary["role"] == p["primary_role"]
    assert primary["division"] == primary["allowed_divisions"][-1]


def test_missing_dancer_is_a_readable_error(server_url):
    r = call(server_url, "get_dancer_profile", {"wsdc_id": 9_999_998})
    assert r.is_error and "search_dancers" in r.content[0].text


def test_results_filters_and_totals(server_url):
    dancer = any_dancer(server_url)
    res = call(server_url, "get_dancer_results", {"wsdc_id": dancer["wsdc_id"], "limit": 3}).structured_content
    assert res["total_results"] >= len(res["results"]) and len(res["results"]) <= 3
    dates = [r["date"] for r in res["results"] if r["date"]]
    assert dates == sorted(dates, reverse=True)
    if res["results"]:
        div = res["results"][0]["division"]
        only = call(server_url, "get_dancer_results", {"wsdc_id": dancer["wsdc_id"], "division": div}).structured_content
        assert {r["division"] for r in only["results"]} == {div}


def test_events_and_event_results(server_url):
    events = call(server_url, "search_events", {"limit": 3}).structured_content
    assert events["count"] >= 1
    event = events["events"][0]
    res = call(server_url, "get_event_results", {"event_id": event["event_id"]}).structured_content
    assert res["event"]["event_id"] == event["event_id"]
    assert sum(len(d["results"]) for d in res["divisions"]) == event["results_count"]


def test_unknown_country_suggests(server_url):
    r = call(server_url, "search_events", {"country": "Rusia"})
    assert r.is_error and "Russia" in r.content[0].text


def test_division_stats_and_registry_info(server_url):
    stats = call(server_url, "get_division_stats", {}).structured_content
    assert stats["dancers"] > 1000 and stats["divisions"]
    info = call(server_url, "get_registry_info", {}).structured_content
    assert info["dancers"] == stats["dancers"]


def test_finals_partners(server_url):
    dancer = any_dancer(server_url)
    r = call(server_url, "get_jj_partners", {"wsdc_id": dancer["wsdc_id"], "limit": 5}).structured_content
    assert r["wsdc_id"] == dancer["wsdc_id"] and r["paired"] <= r["placements"]
    assert len(r["partnerships"]) <= 5
    for p in r["partnerships"]:
        assert p["role"] in ("Leader", "Follower") and p["partner_wsdc_id"] != dancer["wsdc_id"]
        assert p["placement"] in ("1st", "2nd", "3rd", "4th", "5th")
    assert all(f["times"] > 1 for f in r["frequent_partners"])
    # The partner sees the dancer as their partner in the same final.
    if r["partnerships"]:
        p = r["partnerships"][0]
        back = call(server_url, "get_jj_partners", {"wsdc_id": p["partner_wsdc_id"], "limit": 200}).structured_content
        assert any(q["partner_wsdc_id"] == dancer["wsdc_id"] and q["event_id"] == p["event_id"] for q in back["partnerships"])
    assert call(server_url, "get_jj_partners", {"wsdc_id": 9_999_999}).is_error


def test_event_history(server_url):
    event = call(server_url, "search_events", {"limit": 1}).structured_content["events"][0]
    words = " ".join(event["name"].split()[:2])
    r = call(server_url, "get_event_history", {"query": words, "places": 2, "limit": 3}).structured_content
    assert r["editions_total"] >= len(r["editions"]) >= 1 and len(r["editions"]) <= 3
    dates = [e["event"]["start_date"] for e in r["editions"]]
    assert dates == sorted(dates, reverse=True)
    for edition in r["editions"]:
        for division in edition["divisions"]:
            assert {x["placement"] for x in division["results"]} <= {"1st", "2nd"}
    assert call(server_url, "get_event_history", {"query": "zzqx nothing like this"}).is_error


def test_registry_updates(server_url):
    r = call(server_url, "get_registry_updates", {"days": 120, "limit": 5}).structured_content
    assert r["total"] >= len(r["updates"])
    published = [u["published"] for u in r["updates"]]
    assert published == sorted(published, reverse=True)
    for u in r["updates"]:
        assert u["published"] >= r["since"] and u["divisions"]
        if u["top_division"]:
            assert u["top_division"]["division"] == u["divisions"][0]
            assert {x["placement"] for x in u["top_division"]["results"]} == {"1st"}


def test_completions_suggest_dancers_and_events(server_url):
    from mcp.types import PromptReference

    async def run():
        async with Client(server_url) as client:
            dancers = await client.complete(PromptReference(name="dancer_story"), {"name": "dancer", "value": "smith"})
            events = await client.complete(PromptReference(name="event_story"), {"name": "event", "value": "open"})
            countries = await client.complete(PromptReference(name="registry_news"), {"name": "country", "value": "ger"})
            return dancers, events, countries

    dancers, events, countries = asyncio.run(run())
    assert dancers.completion.values and all("(WSDC #" in v for v in dancers.completion.values)
    assert events.completion.values and all("open" in v.lower() for v in events.completion.values)
    assert countries.completion.values == ["Germany"]
