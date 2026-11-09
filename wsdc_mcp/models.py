"""Structured tool outputs. Field descriptions end up in each tool's output schema."""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

# Skill-level Jack & Jill divisions, lowest to highest.
SKILL_DIVISIONS = ["Newcomer", "Novice", "Intermediate", "Advanced", "All-Stars", "Champions"]
OTHER_DIVISIONS = ["Masters", "Sophisticated", "Juniors", "Professional", "Teacher", "Invitational"]
ALL_DIVISIONS = SKILL_DIVISIONS + OTHER_DIVISIONS


class DancerSummary(BaseModel):
    wsdc_id: int = Field(description="WSDC competitor ID")
    name: str = Field(description="Full name, 'First Last'")
    country: str | None = None
    primary_role: str | None = Field(None, description="'Leader' or 'Follower' (role with more points)")
    primary_division: str | None = Field(None, description="Highest division the dancer may enter in the primary role")
    primary_division_points: int | None = Field(None, description="Points in that division, primary role")
    secondary_role: str | None = Field(None, description="Set only when the dancer also has points in the other role")
    secondary_division: str | None = None
    secondary_division_points: int | None = None
    first_points: dt.date | None = Field(None, description="Date of the dancer's first WSDC points")
    last_points: dt.date | None = Field(None, description="Date of the dancer's most recent WSDC points")


class SearchDancersResult(BaseModel):
    query: str
    match_type: str = Field(description="'id', 'name' or 'fuzzy' (no exact name match, closest spellings)")
    count: int
    dancers: list[DancerSummary]


class Levelup(BaseModel):
    division: str
    date: dt.date = Field(description="When the dancer became eligible for / moved into this division")
    points: int = Field(description="Dancer's current total points in this division and role")


class RoleStatus(BaseModel):
    role: str = Field(description="'Leader' or 'Follower'")
    has_points: bool = Field(description="Whether the dancer has any Jack & Jill points in this role")
    division: str | None = Field(None, description="Highest division the dancer may enter in this role")
    division_points: int = Field(0, description="Points in the current division for this role")
    allowed_divisions: list[str] = Field(
        default_factory=list,
        description="Skill-level Jack & Jill divisions the dancer may enter in this role (WSDC rules applied)",
    )
    points_by_division: dict[str, int] = Field(
        default_factory=dict, description="Non-zero point totals per skill-level division in this role"
    )
    levelups: list[Levelup] = Field(default_factory=list, description="Level-up timeline, lowest division first")


class DancerProfile(BaseModel):
    wsdc_id: int
    name: str
    first_name: str | None = None
    last_name: str | None = None
    country: str | None = None
    primary_role: str | None = None
    is_dual_role: bool = Field(False, description="True when the dancer has points as both Leader and Follower")
    roles: list[RoleStatus] = Field(description="Primary role first")
    first_points: dt.date | None = None
    last_points: dt.date | None = None
    note: str | None = None


class CompetitionResult(BaseModel):
    date: dt.date | None
    event_id: int
    event_name: str
    location: str | None = None
    country: str | None = None
    division: str
    role: str = Field(description="'Leader' or 'Follower'")
    placement: str = Field(description="'1st'..'5th', or 'Finalist' (made the final, placed below 5th)")
    points: int


class DancerResults(BaseModel):
    wsdc_id: int
    name: str
    total_results: int = Field(description="Number of results matching the filters (before limit)")
    total_points: int = Field(description="Sum of points over all matching results (before limit)")
    results: list[CompetitionResult] = Field(description="Most recent first")


class EventSummary(BaseModel):
    event_id: int
    name: str
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    location: str | None = None
    country: str | None = None
    results_count: int | None = Field(None, description="Number of point-earning placements recorded")


class SearchEventsResult(BaseModel):
    count: int
    events: list[EventSummary] = Field(description="Most recent first")


class EventPlacement(BaseModel):
    placement: str
    role: str
    points: int
    wsdc_id: int
    name: str
    country: str | None = None


class DivisionResults(BaseModel):
    division: str
    results: list[EventPlacement]


class EventResults(BaseModel):
    event: EventSummary
    divisions: list[DivisionResults] = Field(description="Highest division first; placements in order")


class LevelupEntry(BaseModel):
    wsdc_id: int
    name: str
    country: str | None = None
    role: str
    division: str = Field(description="Division the dancer moved up into")
    date: dt.date


class RecentLevelupsResult(BaseModel):
    since: dt.date
    country: str | None = None
    division: str | None = None
    total: int = Field(description="Total matching level-ups (before limit)")
    levelups: list[LevelupEntry] = Field(description="Most recent first")


class DivisionCount(BaseModel):
    division: str
    leaders: int
    followers: int


class DivisionStatsResult(BaseModel):
    country: str | None = None
    active_since: dt.date | None = Field(None, description="Only dancers with points on/after this date were counted")
    dancers: int = Field(description="Distinct dancers counted")
    divisions: list[DivisionCount] = Field(description="Dancers by current division and role, lowest division first")


class RulesExcerpt(BaseModel):
    section: str
    text: str


class RulesSearchResult(BaseModel):
    version: str = Field(description="Rulebook version, e.g. '2026.1B'")
    query: str
    excerpts: list[RulesExcerpt]
    sections: list[str] = Field(description="All section headings in the rulebook, for follow-up searches")


class Partnership(BaseModel):
    date: dt.date | None = None
    event_id: int
    event_name: str
    division: str
    placement: str = Field(description="Place the couple took, '1st' to '5th'")
    role: str = Field(description="The dancer's role in this final")
    partner_wsdc_id: int
    partner_name: str
    partner_country: str | None = None


class FrequentPartner(BaseModel):
    wsdc_id: int
    name: str
    country: str | None = None
    times: int = Field(description="Placements together")
    best_placement: str
    last_date: dt.date | None = None


class DancerPartners(BaseModel):
    wsdc_id: int
    name: str
    placements: int = Field(description="Placed Jack & Jill results (1st to 5th) of the dancer")
    paired: int = Field(description="Placements whose partner could be identified")
    frequent_partners: list[FrequentPartner] = Field(description="Partners placed with more than once, most often first")
    partnerships: list[Partnership] = Field(description="Placements with their partner, most recent first")
    note: str


class EventEdition(BaseModel):
    event: EventSummary
    divisions: list[DivisionResults] = Field(description="Highest division first; requested placements only")


class EventHistoryResult(BaseModel):
    series: str = Field(description="Name of the event series (the most recent edition's name)")
    wsdc_event_id: int | None = Field(None, description="WSDC's own ID of the event series")
    editions_total: int = Field(description="Editions with results in the registry (before limit)")
    editions: list[EventEdition] = Field(description="Most recent first")
    other_series: list[str] = Field(description="Other event series that also matched the query")


class RegistryUpdate(BaseModel):
    event: EventSummary
    published: dt.date = Field(description="When the event's results first appeared in the registry")
    divisions: list[str] = Field(description="Divisions with results, highest first")
    top_division: DivisionResults | None = Field(None, description="Winners (1st place) of the highest division")


class RegistryUpdatesResult(BaseModel):
    since: dt.date
    country: str | None = None
    total: int = Field(description="Events with newly published results (before limit)")
    updates: list[RegistryUpdate] = Field(description="Most recently published first")
