"""MCP tool and resource definitions."""

from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Annotated, Any, ParamSpec, TypeVar

from mcp.server.apps import Apps
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Completion, Icon, PromptReference, ToolAnnotations
from pydantic import Field

from . import __version__, data, usage
from .config import Config
from .db import Database
from .models import (
    ALL_DIVISIONS,
    DancerPartners,
    DancerProfile,
    DancerResults,
    DivisionStatsResult,
    EventHistoryResult,
    EventResults,
    RecentLevelupsResult,
    RegistryUpdatesResult,
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
- "Who did X dance with in finals?" -> get_jj_partners. "Who won event Y over the years?" ->
  get_event_history. "What's new / which results came out recently?" -> get_registry_updates.
- Data mirrors the public registry and is refreshed regularly, but the official source is
  worldsdc.com; mention that for anything consequential (e.g. contest registration disputes).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

# Prompt argument (module level: annotations are resolved against the module's globals).
DancerArg = Annotated[str, Field(description="Dancer's name or WSDC ID")]

# MCP Apps: an interactive dancer card that hosts supporting the extension render for get_dancer_profile.
DANCER_CARD_URI = "ui://wsdc/dancer-card.html"


def dancer_card_html(dancer_page_url: str | None) -> str:
    """The card's HTML; `dancer_page_url` (with an {id} placeholder) adds an "Open page" button."""
    html = resources.files("wsdc_mcp").joinpath("ui/dancer_card.html").read_text(encoding="utf-8")
    return html.replace("__DANCER_URL__", json.dumps(dancer_page_url or ""))


def _note_outcome(outcome: Callable[[Any], dict[str, Any]], result: Any) -> None:
    """Add what `outcome` says about a tool result to the usage line; never fails the call."""
    try:
        fields = outcome(result)
    except Exception:
        logger.exception("usage outcome failed")
        return
    usage.note(**{k: v[:usage.MAX_TEXT] if isinstance(v, str) else v for k, v in fields.items()})


def build_server(config: Config, db: Database | None, rules: RulesIndex | None) -> MCPServer:
    apps = Apps()
    apps.add_html_resource(
        DANCER_CARD_URI,
        dancer_card_html(config.dancer_page_url),
        name="dancer-card",
        title="WSDC dancer card",
        description="Interactive card for get_dancer_profile: divisions, points per division and level-ups per role.",
        prefers_border=True,
    )
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
        extensions=[apps],
    )

    def need_db() -> Database:
        if db is None:
            raise ToolError("The registry database is not configured on this server.")
        return db

    def tool(
        title: str, outcome: Callable[[Any], dict[str, Any]] | None = None, meta: dict[str, Any] | None = None
    ) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
        """Register a read-only tool with call logging and user-facing error mapping.

        `outcome` picks what the usage log keeps about a result: how many matches it had, or
        whose data it was, so that usage statistics can tell what people look for and miss.
        """

        def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
            @functools.wraps(fn)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                started = time.perf_counter()
                status = "ok"
                try:
                    result = await fn(*args, **kwargs)
                    if outcome is not None:
                        _note_outcome(outcome, result)
                    return result
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
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    usage.note(tool_status=status, tool_ms=round(elapsed_ms))
                    logger.info(
                        "tool=%s status=%s ms=%.0f args=%s",
                        fn.__name__,
                        status,
                        elapsed_ms,
                        json.dumps(kwargs, default=str, ensure_ascii=False)[:300],
                    )

            mcp.add_tool(wrapper, title=title, annotations=READ_ONLY, structured_output=True, meta=meta)
            return fn

        return decorator

    @tool("Search WSDC dancers", outcome=lambda r: {"results": len(r.dancers), "match": r.match_type})
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

    @tool("Get WSDC dancer profile", outcome=lambda r: {"subject": r.name}, meta={"ui": {"resourceUri": DANCER_CARD_URI}})
    async def get_dancer_profile(
        wsdc_id: Annotated[int, Field(ge=1, le=9_999_999, description="WSDC competitor ID")],
    ) -> DancerProfile:
        """Get a dancer's WSDC profile: current division and points per role (Leader/Follower),
        the Jack & Jill divisions they are allowed to enter in each role, points per division,
        level-up timeline, and first/last competition dates.

        Use it for "what division can X dance?" and "how close is X to moving up?"; when only a
        name is known, call search_dancers first to get the WSDC ID. An unknown ID returns an
        error that says so (nothing is guessed).
        """
        return await data.get_dancer_profile(need_db(), wsdc_id)

    @tool("Get WSDC dancer competition results", outcome=lambda r: {"subject": r.name, "results": r.total_results})
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

    @tool("Search WSDC events", outcome=lambda r: {"results": len(r.events)})
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

    @tool("Get WSDC event results", outcome=lambda r: {"subject": r.event.name})
    async def get_event_results(
        event_id: Annotated[int, Field(ge=1, description="Event ID from search_events")],
        division: Annotated[str | None, Field(description="Only this division, e.g. 'Champions'")] = None,
    ) -> EventResults:
        """Get the Jack & Jill placements recorded in the WSDC registry for one event, grouped by
        division (highest first): placement, role, points awarded, dancer name and WSDC ID.
        """
        return await data.get_event_results(need_db(), event_id, division)

    @tool("Get a dancer's Jack & Jill finals partners", outcome=lambda r: {"subject": r.name, "results": r.paired})
    async def get_jj_partners(
        wsdc_id: Annotated[int, Field(ge=1, le=9_999_999, description="WSDC competitor ID")],
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum number of placements to list")] = 30,
    ) -> DancerPartners:
        """Who a dancer was placed with in Jack & Jill finals: each placement (1st to 5th) with the
        partner, most recent first, and the partners they placed with most often. The registry does
        not record couples; a final places couples, so the partner is the dancer of the other role
        who took the same place in the same division.
        """
        return await data.get_jj_partners(need_db(), wsdc_id, limit)

    @tool("Get the history of a WSDC event", outcome=lambda r: {"subject": r.series, "results": r.editions_total})
    async def get_event_history(
        query: Annotated[
            str, Field(min_length=2, max_length=100, description="Words from the event's name, e.g. 'US Open'")
        ],
        division: Annotated[
            str | None, Field(description="Only this division, e.g. 'Champions' (default: every division)")
        ] = None,
        places: Annotated[int, Field(ge=1, le=5, description="Placements to show per division: 1 = winners only")] = 1,
        limit: Annotated[int, Field(ge=1, le=60, description="Maximum number of editions (years) to return")] = 15,
    ) -> EventHistoryResult:
        """Every edition of a WSDC event series in the registry (for example each year's US Open),
        most recent first, with the winners (or the top placements) of each division or of one
        division. Also lists other event series that matched the query.
        """
        return await data.get_event_history(need_db(), query, division, places, limit)

    @tool("Get what's new in the WSDC registry", outcome=lambda r: {"results": r.total})
    async def get_registry_updates(
        days: Annotated[int, Field(ge=1, le=120, description="Look-back window in days")] = 14,
        country: Annotated[str | None, Field(max_length=60, description="Only events in this country")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum number of events to return")] = 20,
    ) -> RegistryUpdatesResult:
        """Events whose results were published in the WSDC registry in the last days, most recent
        first: event, when the results appeared, divisions held, and the winners of the highest
        division. Pair with get_recent_levelups for dancers who moved up.
        """
        return await data.registry_updates(need_db(), days, country, limit)

    @tool("Get recent WSDC level-ups", outcome=lambda r: {"results": len(r.levelups)})
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

    @tool("Search the WSDC rules", outcome=lambda r: {"results": len(r.excerpts)})
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

    # -------------------------------------------------------------- prompts and completions

    @mcp.prompt(title="Which divisions can I dance?")
    def divisions_check(dancer: DancerArg) -> str:
        """Which Jack & Jill divisions a dancer may enter in each role, and what is missing for the next level."""
        return (
            f"Which Jack & Jill divisions can {dancer} enter in each role under the current WSDC rules, and how "
            "many points are missing for the next level? Find the dancer with search_dancers unless a WSDC ID is "
            "given, then use get_dancer_profile. For move-up thresholds use search_wsdc_rules and name the "
            "rulebook version."
        )

    @mcp.prompt(title="A dancer's competition story")
    def dancer_story(dancer: DancerArg) -> str:
        """First points, level-ups, best results, favourite events and most frequent finals partners."""
        return (
            f"Tell the West Coast Swing competition story of {dancer}: first points, level-ups in each role, best "
            "placements, the events they went to most, and the partners they made finals with most often. Use "
            "search_dancers (unless a WSDC ID is given), get_dancer_profile, get_dancer_results and get_jj_partners."
        )

    @mcp.prompt(title="History of an event")
    def event_story(
        event: Annotated[str, Field(description="Event name, e.g. 'US Open'")],
        division: Annotated[str | None, Field(description="Only this division, e.g. 'Champions'")] = None,
    ) -> str:
        """Winners of an event year by year, and who won it more than once."""
        scope = f"the {division} division" if division else "the top divisions"
        return (
            f"Show the history of {event} in the WSDC registry: the winners of {scope} year by year, and who won "
            "more than once. Use get_event_history; if it lists other matching series, say which one you used."
        )

    @mcp.prompt(title="What's new in the registry")
    def registry_news(
        country: Annotated[str | None, Field(description="Only this country, e.g. 'Germany'")] = None,
    ) -> str:
        """Results published in the last two weeks and the dancers who moved up."""
        where = f" in {country}" if country else ""
        return (
            f"What's new in the WSDC points registry{where} over the last two weeks? Use get_registry_updates for "
            "newly published event results and get_recent_levelups (days=14) for dancers who moved up, and "
            "summarize by event."
        )

    @mcp.prompt(title="Ask about the WSDC rules")
    def rules_question(question: Annotated[str, Field(description="Your question about the WSDC rules")]) -> str:
        """Answer a question from the official WSDC Registry rulebook."""
        return (
            f"Answer this question about the WSDC Registry rules: {question}\nUse search_wsdc_rules, quote the "
            "relevant sections with the rulebook version, and say when to check the official rules at worldsdc.com."
        )

    async def complete(argument: str, value: str) -> list[str]:
        prefix = value.strip()
        if argument == "division":
            return [d for d in ALL_DIVISIONS if d.lower().startswith(prefix.lower())]
        if len(prefix) < 2 or db is None:
            return []
        if argument == "dancer":
            found = await data.search_dancers(db, prefix, 10)
            return [f"{d.name} (WSDC #{d.wsdc_id})" for d in found.dancers]
        if argument == "event":
            return await data.event_series_names(db, prefix, 10)
        if argument == "country":
            return await data.country_names_starting(db, prefix, 10)
        return []

    @mcp.completion()
    async def completion(ref: Any, argument: Any, context: Any) -> Completion | None:
        """Suggest dancers, events, countries and divisions while a prompt's arguments are typed."""
        if not isinstance(ref, PromptReference):
            return None
        try:
            values = await complete(argument.name, argument.value)
        except data.NotFound:
            values = []
        return Completion(values=values[:100], total=len(values), has_more=False)

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
