"""MCP tool and resource definitions."""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Icon, ToolAnnotations
from pydantic import Field

from . import __version__, data
from .config import Config
from .db import Database
from .models import (
    DancerProfile,
    DancerResults,
    DivisionStatsResult,
    EventResults,
    RecentLevelupsResult,
    RulesExcerpt,
    RulesSearchResult,
    SearchDancersResult,
    SearchEventsResult,
)
from .rules import RulesIndex

logger = logging.getLogger("wsdc_mcp.tools")

P = ParamSpec("P")
T = TypeVar("T")

SERVER_NAME = "com.wcspoints/wsdc-points"
SERVER_TITLE = "WCS Points"
SERVER_DESCRIPTION = (
    "West Coast Swing WSDC points registry: dancer points, divisions, level-ups, event results and rules."
)

INSTRUCTIONS = """\
Unofficial, read-only access to the World Swing Dance Council (WSDC) Points Registry for
West Coast Swing (WCS) Jack & Jill competitions: ~27,000 dancers and ~8,000 events since the 1990s.

How to use:
- Dancers are identified by their WSDC ID. If the user gives a name, call search_dancers first,
  then get_dancer_profile / get_dancer_results with the ID. Names may be typed in any order
  ("First Last" or "Last, First"); Cyrillic is transliterated; misspellings fall back to fuzzy matching.
- "What division can X dance?" -> get_dancer_profile: `allowed_divisions` per role already applies
  WSDC rules (primary/secondary role rules, move-up thresholds). Do not recompute them.
- Skill divisions, low to high: Newcomer, Novice, Intermediate, Advanced, All-Stars, Champions.
  Points also exist in age/other divisions (Masters, Sophisticated, Juniors, ...).
- Registry dates have month precision: the day of month is always 01.
- Rules questions (points per placement, move-up thresholds, tiers, dancing in a secondary role)
  -> search_wsdc_rules, and quote the rulebook version in the answer.
- Data mirrors the public registry and is refreshed regularly, but the official source is
  worldsdc.com; mention that for anything consequential (e.g. contest registration disputes).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def build_server(config: Config, db: Database | None, rules: RulesIndex | None) -> MCPServer:
    mcp = MCPServer(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
        instructions=INSTRUCTIONS,
        website_url=config.public_url,
        icons=[
            Icon(src=f"{config.public_url}/icon-512.png", mime_type="image/png", sizes=["512x512"]),
            Icon(src=f"{config.public_url}/icon.svg", mime_type="image/svg+xml", sizes=["any"]),
        ],
        version=__version__,
    )

    def need_db() -> Database:
        if db is None:
            raise ToolError("The registry database is not configured on this server.")
        return db

    def tool(title: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
        """Register a read-only tool with call logging and user-facing error mapping."""

        def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
            @functools.wraps(fn)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                started = time.perf_counter()
                status = "ok"
                try:
                    return await fn(*args, **kwargs)
                except data.NotFound as exc:
                    status = "not_found"
                    raise ToolError(str(exc)) from exc
                except ToolError:
                    status = "tool_error"
                    raise
                except Exception:
                    status = "error"
                    raise
                finally:
                    logger.info(
                        "tool=%s status=%s ms=%.0f args=%s",
                        fn.__name__,
                        status,
                        (time.perf_counter() - started) * 1000,
                        json.dumps(kwargs, default=str, ensure_ascii=False)[:300],
                    )

            mcp.add_tool(wrapper, title=title, annotations=READ_ONLY, structured_output=True)
            return fn

        return decorator

    @tool("Search WSDC dancers")
    async def search_dancers(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=100,
                description="Dancer name (any order, partial names OK, Latin or Cyrillic) or a WSDC ID",
            ),
        ],
        limit: Annotated[int, Field(ge=1, le=25, description="Maximum number of dancers to return")] = 10,
    ) -> SearchDancersResult:
        """Find West Coast Swing dancers in the WSDC Points Registry by name or WSDC ID.

        Returns WSDC ID, country, primary role and current division for each match, most
        recently active dancers first. Use the WSDC ID with the other dancer tools.
        """
        return await data.search_dancers(need_db(), query, limit)

    @tool("Get WSDC dancer profile")
    async def get_dancer_profile(
        wsdc_id: Annotated[int, Field(ge=1, le=9_999_999, description="WSDC competitor ID")],
    ) -> DancerProfile:
        """Get a dancer's WSDC profile: current division and points per role (Leader/Follower),
        the Jack & Jill divisions they are allowed to enter in each role, points per division,
        level-up timeline, and first/last competition dates.
        """
        return await data.get_dancer_profile(need_db(), wsdc_id)

    @tool("Get WSDC dancer competition results")
    async def get_dancer_results(
        wsdc_id: Annotated[int, Field(ge=1, le=9_999_999, description="WSDC competitor ID")],
        division: Annotated[
            str | None, Field(description="Only this division, e.g. 'Novice', 'All-Stars', 'Masters'")
        ] = None,
        role: Annotated[str | None, Field(description="Only results as 'Leader' or 'Follower'")] = None,
        year: Annotated[int | None, Field(ge=1985, le=2100, description="Only results from this year")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum number of results to return")] = 50,
    ) -> DancerResults:
        """List a dancer's WSDC Jack & Jill placements (event, date, division, role, placement,
        points), most recent first. Totals are computed over all results matching the filters.
        """
        return await data.get_dancer_results(need_db(), wsdc_id, division, role, year, limit)

    @tool("Search WSDC events")
    async def search_events(
        query: Annotated[
            str | None, Field(max_length=100, description="Words from the event name or city, e.g. 'Swing Diego'")
        ] = None,
        year: Annotated[int | None, Field(ge=1985, le=2100, description="Only events held in this year")] = None,
        country: Annotated[
            str | None, Field(max_length=60, description="Only events in this country, e.g. 'Germany', 'USA'")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum number of events to return")] = 20,
    ) -> SearchEventsResult:
        """Find WSDC registry events (West Coast Swing conventions with points-awarding Jack &
        Jill contests) by name, city, year and/or country. Most recent first. Use the event_id
        with get_event_results.
        """
        return await data.search_events(need_db(), query, year, country, limit)

    @tool("Get WSDC event results")
    async def get_event_results(
        event_id: Annotated[int, Field(ge=1, description="Event ID from search_events")],
        division: Annotated[str | None, Field(description="Only this division, e.g. 'Champions'")] = None,
    ) -> EventResults:
        """Get the Jack & Jill placements recorded in the WSDC registry for one event, grouped by
        division (highest first): placement, role, points awarded, dancer name and WSDC ID.
        """
        return await data.get_event_results(need_db(), event_id, division)

    @tool("Get recent WSDC level-ups")
    async def get_recent_levelups(
        country: Annotated[str | None, Field(max_length=60, description="Only dancers from this country")] = None,
        division: Annotated[
            str | None,
            Field(description="Only moves into this division: Novice, Intermediate, Advanced, All-Stars or Champions"),
        ] = None,
        days: Annotated[int, Field(ge=1, le=730, description="Look-back window in days")] = 90,
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum number of level-ups to return")] = 50,
    ) -> RecentLevelupsResult:
        """List dancers who recently moved up to a higher WSDC division (in either role),
        most recent first, optionally filtered by country and target division.
        """
        return await data.recent_levelups(need_db(), country, division, days, limit)

    @tool("Get WSDC division statistics")
    async def get_division_stats(
        country: Annotated[str | None, Field(max_length=60, description="Only dancers from this country")] = None,
        active_within_months: Annotated[
            int | None,
            Field(ge=1, le=600, description="Only count dancers who earned points within this many months"),
        ] = None,
    ) -> DivisionStatsResult:
        """Count dancers by current division and role (Leader/Follower), worldwide or for one
        country, optionally only recently active dancers.
        """
        return await data.division_stats(need_db(), country, active_within_months)

    @tool("Search the WSDC rules")
    async def search_wsdc_rules(
        query: Annotated[
            str,
            Field(
                min_length=2,
                max_length=200,
                description="What to look up, e.g. 'points needed to move up from Novice', 'points per tier'",
            ),
        ],
        max_results: Annotated[int, Field(ge=1, le=8, description="Maximum number of rulebook excerpts")] = 4,
    ) -> RulesSearchResult:
        """Search the official WSDC Registry Event Rules (current rulebook) and return the most
        relevant sections verbatim: division move-up thresholds, points per placement and tier,
        primary/secondary role rules, contest requirements, age divisions, judging.
        """
        if rules is None:
            raise ToolError("The WSDC rulebook is not loaded on this server.")
        hits = rules.search(query, max_results)
        return RulesSearchResult(
            version=rules.version,
            query=query,
            excerpts=[RulesExcerpt(section=h.heading, text=h.text) for h in hits],
            sections=rules.headings,
        )

    @tool("Get WSDC registry info")
    async def get_registry_info() -> dict[str, Any]:
        """Data coverage and freshness: number of dancers and events, date of the most recent
        event in the mirror, the rulebook version, and links to the official sources.
        """
        info: dict[str, Any] = dict(await data.data_freshness(need_db()))
        info["latest_event_date"] = str(info["latest_event_date"]) if info.get("latest_event_date") else None
        info["rules_version"] = rules.version if rules else None
        info["official_registry"] = "https://worldsdc.com/registry/"
        info["about"] = f"{config.public_url}/"
        info["note"] = "Unofficial mirror, not affiliated with the World Swing Dance Council."
        return info

    if rules is not None:

        @mcp.resource(
            "wsdc://rules/current",
            name="wsdc-rules",
            title=f"WSDC Registry Event Rules {rules.version}",
            description="Full text of the current WSDC Registry Event Rules (plain text extracted from the PDF).",
            mime_type="text/plain",
        )
        def rules_text() -> str:
            return rules.text

    return mcp
