"""Read-only queries against the WSDC registry database.

All profile semantics (allowed divisions per role, cross-role rules, level-up dates) are
computed in the database (`contester_view_roles`); this module only reads and reshapes them,
mirroring how the @wsdc_bot Telegram bot renders a dancer profile.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from .db import Database, cached
from .models import (
    ALL_DIVISIONS,
    SKILL_DIVISIONS,
    CompetitionResult,
    DancerProfile,
    DancerResults,
    DancerSummary,
    DivisionCount,
    DivisionResults,
    DivisionStatsResult,
    EventPlacement,
    EventResults,
    EventSummary,
    Levelup,
    LevelupEntry,
    RecentLevelupsResult,
    RoleStatus,
    SearchDancersResult,
    SearchEventsResult,
)


class NotFound(LookupError):
    """A user-facing 'no such thing' error; the message is shown to the model."""


# Column suffixes used by contester_view_roles / the role-split cache, per skill division.
DIVISION_SUFFIX = {
    "Newcomer": "newcomer",
    "Novice": "novice",
    "Intermediate": "intermediate",
    "Advanced": "advanced",
    "All-Stars": "allstars",
    "Champions": "champions",
}
ROLE_PREFIX = {"Leader": "leader", "Follower": "follower"}
ENTRY_TYPE = {"L": "Leader", "F": "Follower"}

_DIVISION_ALIASES = {
    "newcomer": "Newcomer", "newcomers": "Newcomer", "nc": "Newcomer",
    "novice": "Novice", "nov": "Novice",
    "intermediate": "Intermediate", "int": "Intermediate", "inter": "Intermediate",
    "advanced": "Advanced", "adv": "Advanced",
    "allstars": "All-Stars", "allstar": "All-Stars", "as": "All-Stars",
    "champions": "Champions", "champion": "Champions", "champs": "Champions", "champ": "Champions",
    "masters": "Masters", "master": "Masters",
    "sophisticated": "Sophisticated", "sophisticates": "Sophisticated",
    "juniors": "Juniors", "junior": "Juniors",
    "professional": "Professional", "pro": "Professional", "pros": "Professional",
    "teacher": "Teacher", "teachers": "Teacher",
    "invitational": "Invitational", "invitationals": "Invitational",
}

_COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "great britain": "United Kingdom", "england": "United Kingdom",
    "britain": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
    "russian federation": "Russia", "czechia": "Czech Republic", "korea": "South Korea",
    "holland": "Netherlands", "the netherlands": "Netherlands",
}

_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    "є": "ye", "і": "i", "ї": "yi", "ґ": "g", "ў": "u",
}


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------


def normalize_name_query(query: str) -> str:
    """Lowercase, transliterate Cyrillic, strip accents and punctuation."""
    q = query.strip().lower()
    q = "".join(_CYRILLIC.get(ch, ch) for ch in q)
    q = unicodedata.normalize("NFKD", q).encode("ascii", "ignore").decode("ascii")
    q = re.sub(r"[^a-z0-9' -]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def normalize_division(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    key = re.sub(r"[^a-z]", "", value.lower())
    if key in _DIVISION_ALIASES:
        return _DIVISION_ALIASES[key]
    raise NotFound(f"Unknown division '{value}'. Valid divisions: {', '.join(ALL_DIVISIONS)}.")


def normalize_role(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    v = value.strip().lower()
    if v in ("leader", "lead", "leaders", "l"):
        return "Leader"
    if v in ("follower", "follow", "followers", "f"):
        return "Follower"
    raise NotFound(f"Unknown role '{value}'. Use 'Leader' or 'Follower'.")


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _d(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _display_name(first: str | None, last: str | None, full: str | None) -> str:
    if first or last:
        return " ".join(p.strip() for p in (first, last) if p and p.strip())
    if full and "," in full:
        last_name, first_name = full.split(",", 1)
        return f"{first_name.strip()} {last_name.strip()}"
    return (full or "Unknown").strip()


def _placement(result: str | None) -> str:
    if result is None:
        return "Unknown"
    r = result.strip().upper()
    if r == "F":
        return "Finalist"
    if r.isdigit():
        n = int(r)
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")
        return f"{n}{suffix}"
    return r


# ---------------------------------------------------------------------------
# Role helpers (mirror bot/features/role_split/bot/profile_display_v2.py)
# ---------------------------------------------------------------------------


def _allowed_divisions(row: dict[str, Any], role: str) -> list[str]:
    prefix = ROLE_PREFIX[role]
    return [div for div, suffix in DIVISION_SUFFIX.items() if row.get(f"{prefix}_available_{suffix}")]


def _role_points_total(row: dict[str, Any], role: str) -> int:
    prefix = ROLE_PREFIX[role]
    return sum(int(row.get(f"{prefix}_{suffix}_points") or 0) for suffix in DIVISION_SUFFIX.values())


def _role_status(row: dict[str, Any], role: str) -> RoleStatus:
    prefix = ROLE_PREFIX[role]
    allowed = _allowed_divisions(row, role)
    points_by_division = {
        div: int(row.get(f"{prefix}_{suffix}_points") or 0)
        for div, suffix in DIVISION_SUFFIX.items()
        if int(row.get(f"{prefix}_{suffix}_points") or 0) > 0
    }
    levelups = []
    for div, suffix in DIVISION_SUFFIX.items():
        entered = _d(row.get(f"{prefix}_entered_{suffix}"))
        if entered:
            levelups.append(
                Levelup(division=div, date=entered, points=int(row.get(f"{prefix}_{suffix}_points") or 0))
            )
    division_points = row.get(f"{prefix}_current_division_points")
    return RoleStatus(
        role=role,
        has_points=_role_points_total(row, role) > 0,
        division=allowed[-1] if allowed else None,
        division_points=int(division_points or 0),
        allowed_divisions=allowed,
        points_by_division=points_by_division,
        levelups=levelups,
    )


def _summary_from_row(row: dict[str, Any]) -> DancerSummary:
    summary = DancerSummary(
        wsdc_id=row["contester_id"],
        name=_display_name(row.get("contester_first_name"), row.get("contester_last_name"), row.get("contester_full_name")),
        country=row.get("contester_country_name"),
        first_points=_d(row.get("contester_start_date")),
        last_points=_d(row.get("contester_end_date")),
    )
    primary = row.get("primary_role")
    if primary in ROLE_PREFIX:
        status = _role_status(row, primary)
        summary.primary_role = primary
        summary.primary_division = status.division
        summary.primary_division_points = status.division_points
        other = "Follower" if primary == "Leader" else "Leader"
        if _role_points_total(row, other) > 0:
            other_status = _role_status(row, other)
            summary.secondary_role = other
            summary.secondary_division = other_status.division
            summary.secondary_division_points = other_status.division_points
    return summary


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_ROLE_POINT_COLUMNS = ", ".join(
    f"coalesce(sum(p.{role}_{suffix}_points), 0)::int AS {role}_{suffix}_points"
    for role in ROLE_PREFIX.values()
    for suffix in DIVISION_SUFFIX.values()
)

# contester_view_roles + per-role point totals from the role-split cache (one row per dancer).
_ROLES_WITH_POINTS_SQL = f"""
    SELECT r.*, pts.*
    FROM contester_view_roles r
    LEFT JOIN LATERAL (
        SELECT {_ROLE_POINT_COLUMNS}
        FROM bot_funnel_cache_2025_role_split p
        WHERE p.contester_id = r.contester_id
    ) pts ON true
    WHERE r.contester_id = ANY(%(ids)s)
"""

_HAYSTACK = (
    "lower(coalesce(c.contester_first_name, '') || ' ' || coalesce(c.contester_last_name, '') || ' ' || "
    "coalesce(c.contester_full_name_decoded, '') || ' ' || coalesce(c.contester_full_name, ''))"
)
_NAME = "lower(coalesce(c.contester_first_name, '') || ' ' || coalesce(c.contester_last_name, ''))"
_NAME_REV = "lower(coalesce(c.contester_last_name, '') || ' ' || coalesce(c.contester_first_name, ''))"


async def _rows_by_ids(db: Database, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    rows = await db.fetch_all(_ROLES_WITH_POINTS_SQL, {"ids": ids})
    return {r["contester_id"]: r for r in rows}


@cached(ttl=600)
async def search_dancers(db: Database, query: str, limit: int) -> SearchDancersResult:
    raw = query.strip()
    id_match = re.fullmatch(r"(?:wsdc\s*)?(?:id\s*)?[#:№]?\s*(\d{1,7})", raw, re.IGNORECASE)
    ids: list[int] = []
    basic: dict[int, dict[str, Any]] = {}
    match_type = "name"

    if id_match:
        match_type = "id"
        rows = await db.fetch_all(
            "SELECT c.* FROM contester_view c WHERE c.contester_id = %(id)s", {"id": int(id_match.group(1))}
        )
    else:
        q = normalize_name_query(raw)
        tokens = [t for t in q.replace("'", " ").split() if t][:6]
        if not tokens:
            raise NotFound("Please provide a dancer name or a WSDC ID.")
        params: dict[str, Any] = {"q": q, "limit": limit}
        conds = []
        for i, tok in enumerate(tokens):
            params[f"t{i}"] = _like(tok)
            conds.append(f"{_HAYSTACK} LIKE %(t{i})s")
        rows = await db.fetch_all(
            f"""
            SELECT c.* FROM contester_view c
            WHERE {' AND '.join(conds)}
            ORDER BY ({_NAME} = %(q)s OR {_NAME_REV} = %(q)s) DESC,
                     c.contester_end_date DESC NULLS LAST, c.contester_full_name
            LIMIT %(limit)s
            """,
            params,
        )
        if not rows and len(q) >= 3:
            match_type = "fuzzy"
            rows = await db.fetch_all(
                f"""
                SELECT c.*, greatest(similarity({_NAME}, %(q)s), similarity({_NAME_REV}, %(q)s)) AS sim
                FROM contester_view c
                WHERE {_NAME} %% %(q)s OR {_NAME_REV} %% %(q)s
                ORDER BY sim DESC, c.contester_end_date DESC NULLS LAST
                LIMIT %(limit)s
                """,
                params,
            )

    for r in rows:
        ids.append(r["contester_id"])
        basic[r["contester_id"]] = r
    full = await _rows_by_ids(db, ids)
    dancers = [_summary_from_row(full.get(i) or basic[i]) for i in ids]
    return SearchDancersResult(query=raw, match_type=match_type, count=len(dancers), dancers=dancers)


@cached(ttl=600)
async def get_dancer_profile(db: Database, wsdc_id: int) -> DancerProfile:
    row = (await _rows_by_ids(db, [wsdc_id])).get(wsdc_id)
    if row is None:
        basic = await db.fetch_one("SELECT * FROM contester_view WHERE contester_id = %(id)s", {"id": wsdc_id})
        if basic is None:
            raise NotFound(f"No dancer with WSDC ID {wsdc_id}. Use search_dancers to look up the ID by name.")
        return DancerProfile(
            wsdc_id=wsdc_id,
            name=_display_name(basic.get("contester_first_name"), basic.get("contester_last_name"), basic.get("contester_full_name")),
            first_name=basic.get("contester_first_name"),
            last_name=basic.get("contester_last_name"),
            country=basic.get("contester_country_name"),
            roles=[],
            first_points=_d(basic.get("contester_start_date")),
            last_points=_d(basic.get("contester_end_date")),
            note="Role-specific division data is not available for this dancer yet.",
        )

    leader_total = _role_points_total(row, "Leader")
    follower_total = _role_points_total(row, "Follower")
    primary = row.get("primary_role") if row.get("primary_role") in ROLE_PREFIX else (
        "Leader" if leader_total >= follower_total else "Follower"
    )
    other = "Follower" if primary == "Leader" else "Leader"
    roles = [_role_status(row, primary), _role_status(row, other)]
    note = None

    if not any(r.allowed_divisions for r in roles) and not (
        row.get("leader_current_division_name") or row.get("follower_current_division_name")
    ):
        # No skill-level Jack & Jill points at all (e.g. Masters/Sophisticated only): the bot
        # treats the dancer as eligible for Newcomer and Novice in both roles.
        primary = "Leader" if leader_total >= follower_total else "Follower"
        other = "Follower" if primary == "Leader" else "Leader"
        roles = [
            RoleStatus(role=r, has_points=False, division="Novice", allowed_divisions=["Newcomer", "Novice"])
            for r in (primary, other)
        ]
        note = "No skill-level Jack & Jill points; points come only from other divisions (e.g. Masters)."

    return DancerProfile(
        wsdc_id=wsdc_id,
        name=_display_name(row.get("contester_first_name"), row.get("contester_last_name"), row.get("contester_full_name")),
        first_name=row.get("contester_first_name"),
        last_name=row.get("contester_last_name"),
        country=row.get("contester_country_name"),
        primary_role=primary,
        is_dual_role=leader_total > 0 and follower_total > 0,
        roles=roles,
        first_points=_d(row.get("contester_start_date")),
        last_points=_d(row.get("contester_end_date")),
        note=note,
    )


@cached(ttl=600)
async def _all_results(db: Database, wsdc_id: int) -> tuple[str | None, list[CompetitionResult]]:
    dancer = await db.fetch_one(
        "SELECT contester_first_name, contester_last_name, contester_full_name FROM contester_view "
        "WHERE contester_id = %(id)s",
        {"id": wsdc_id},
    )
    if dancer is None:
        return None, []
    rows = await db.fetch_all(
        """
        SELECT ev.event_id, ev.event_name, ev.event_start_date, l.location_name, co.country_name,
               d.division_name, d.division_sortorder, e.entry_type, e.entry_result, e.entry_points
        FROM entry_view e
        JOIN event ev ON ev.event_id = e.entry_event
        JOIN division d ON d.division_id = e.entry_division
        LEFT JOIN location l ON l.location_id = ev.event_location
        LEFT JOIN country co ON co.country_id = l.location_country
        WHERE e.entry_contester = %(id)s
        ORDER BY ev.event_start_date DESC, d.division_sortorder DESC
        """,
        {"id": wsdc_id},
    )
    name = _display_name(dancer.get("contester_first_name"), dancer.get("contester_last_name"), dancer.get("contester_full_name"))
    results = [
        CompetitionResult(
            date=_d(r["event_start_date"]),
            event_id=r["event_id"],
            event_name=r["event_name"],
            location=r["location_name"],
            country=r["country_name"],
            division=r["division_name"],
            role=ENTRY_TYPE.get(r["entry_type"], r["entry_type"] or "Unknown"),
            placement=_placement(r["entry_result"]),
            points=int(r["entry_points"] or 0),
        )
        for r in rows
    ]
    return name, results


async def get_dancer_results(
    db: Database, wsdc_id: int, division: str | None, role: str | None, year: int | None, limit: int
) -> DancerResults:
    division = normalize_division(division)
    role = normalize_role(role)
    name, results = await _all_results(db, wsdc_id)
    if name is None:
        raise NotFound(f"No dancer with WSDC ID {wsdc_id}. Use search_dancers to look up the ID by name.")
    filtered = [
        r
        for r in results
        if (division is None or r.division == division)
        and (role is None or r.role == role)
        and (year is None or (r.date is not None and r.date.year == year))
    ]
    return DancerResults(
        wsdc_id=wsdc_id,
        name=name,
        total_results=len(filtered),
        total_points=sum(r.points for r in filtered),
        results=filtered[:limit],
    )


async def resolve_country(db: Database, country: str | None) -> str | None:
    if country is None or not country.strip():
        return None
    value = _COUNTRY_ALIASES.get(country.strip().lower(), country.strip())
    names = await _country_names(db)
    for name, iso in names:
        if name.lower() == value.lower() or (iso and iso.lower() == value.lower()):
            return name
    by_lower = {n.lower(): n for n, _ in names}
    close = [by_lower[m] for m in difflib.get_close_matches(value.lower(), list(by_lower), n=5, cutoff=0.6)]
    close += [n for n, _ in names if n.lower().startswith(value.lower()) and n not in close][: 5 - len(close)]
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise NotFound(f"Unknown country '{country}'.{hint}")


@cached(ttl=3600)
async def _country_names(db: Database) -> list[tuple[str, str | None]]:
    rows = await db.fetch_all("SELECT country_name, country_iso_a2 FROM country WHERE country_name IS NOT NULL")
    return [(r["country_name"], r["country_iso_a2"]) for r in rows]


@cached(ttl=900)
async def search_events(
    db: Database, query: str | None, year: int | None, country: str | None, limit: int
) -> SearchEventsResult:
    country_name = await resolve_country(db, country)
    params: dict[str, Any] = {"year": year, "country": country_name, "limit": limit}
    conds = ["EXISTS (SELECT 1 FROM entry_view x WHERE x.entry_event = ev.event_id)"]
    tokens = normalize_name_query(query).split() if query and query.strip() else []
    for i, tok in enumerate(tokens[:6]):
        params[f"t{i}"] = _like(tok)
        conds.append(
            f"lower(coalesce(ev.event_name, '') || ' ' || coalesce(l.location_name, '')) LIKE %(t{i})s"
        )
    if year is not None:
        conds.append("ev.event_start_date >= make_date(%(year)s, 1, 1) AND ev.event_start_date < make_date(%(year)s + 1, 1, 1)")
    if country_name is not None:
        conds.append("co.country_name = %(country)s")
    rows = await db.fetch_all(
        f"""
        SELECT ev.event_id, ev.event_name, ev.event_start_date, ev.event_end_date,
               l.location_name, co.country_name,
               (SELECT count(*) FROM entry_view x WHERE x.entry_event = ev.event_id)::int AS results_count,
               count(*) OVER ()::int AS total
        FROM event ev
        LEFT JOIN location l ON l.location_id = ev.event_location
        LEFT JOIN country co ON co.country_id = l.location_country
        WHERE {' AND '.join(conds)}
        ORDER BY ev.event_start_date DESC NULLS LAST, ev.event_name
        LIMIT %(limit)s
        """,
        params,
    )
    events = [
        EventSummary(
            event_id=r["event_id"],
            name=r["event_name"],
            start_date=_d(r["event_start_date"]),
            end_date=_d(r["event_end_date"]),
            location=r["location_name"],
            country=r["country_name"],
            results_count=r["results_count"],
        )
        for r in rows
    ]
    return SearchEventsResult(count=rows[0]["total"] if rows else 0, events=events)


@cached(ttl=900)
async def get_event_results(db: Database, event_id: int, division: str | None) -> EventResults:
    division = normalize_division(division)
    ev = await db.fetch_one(
        """
        SELECT ev.event_id, ev.event_name, ev.event_start_date, ev.event_end_date, l.location_name, co.country_name
        FROM event ev
        LEFT JOIN location l ON l.location_id = ev.event_location
        LEFT JOIN country co ON co.country_id = l.location_country
        WHERE ev.event_id = %(id)s
        """,
        {"id": event_id},
    )
    if ev is None:
        raise NotFound(f"No event with ID {event_id}. Use search_events to find the event ID.")
    rows = await db.fetch_all(
        """
        SELECT d.division_name, d.division_sortorder, e.entry_type, e.entry_result, e.entry_points,
               c.contester_id, c.contester_first_name, c.contester_last_name, c.contester_full_name,
               c.contester_country_name
        FROM entry_view e
        JOIN division d ON d.division_id = e.entry_division
        JOIN contester_view c ON c.contester_id = e.entry_contester
        WHERE e.entry_event = %(id)s AND (%(division)s::text IS NULL OR d.division_name = %(division)s)
        ORDER BY d.division_sortorder DESC,
                 CASE WHEN e.entry_result ~ '^[0-9]+$' THEN e.entry_result::int ELSE 99 END,
                 e.entry_points DESC, e.entry_type DESC, c.contester_last_name
        """,
        {"id": event_id, "division": division},
    )
    divisions: list[DivisionResults] = []
    for r in rows:
        if not divisions or divisions[-1].division != r["division_name"]:
            divisions.append(DivisionResults(division=r["division_name"], results=[]))
        divisions[-1].results.append(
            EventPlacement(
                placement=_placement(r["entry_result"]),
                role=ENTRY_TYPE.get(r["entry_type"], r["entry_type"] or "Unknown"),
                points=int(r["entry_points"] or 0),
                wsdc_id=r["contester_id"],
                name=_display_name(r["contester_first_name"], r["contester_last_name"], r["contester_full_name"]),
                country=r["contester_country_name"],
            )
        )
    summary = EventSummary(
        event_id=ev["event_id"],
        name=ev["event_name"],
        start_date=_d(ev["event_start_date"]),
        end_date=_d(ev["event_end_date"]),
        location=ev["location_name"],
        country=ev["country_name"],
        results_count=len(rows),
    )
    return EventResults(event=summary, divisions=divisions)


def _entered_case(prefix: str) -> str:
    whens = " ".join(
        f"WHEN '{div}' THEN c.{prefix}_entered_{suffix}" for div, suffix in DIVISION_SUFFIX.items()
    )
    return f"CASE c.{prefix}_current_division_name {whens} END"


@cached(ttl=1800)
async def recent_levelups(
    db: Database, country: str | None, division: str | None, days: int, limit: int
) -> RecentLevelupsResult:
    division = normalize_division(division)
    if division is not None and division not in SKILL_DIVISIONS[1:]:
        raise NotFound("Level-ups are tracked for Novice, Intermediate, Advanced, All-Stars and Champions.")
    country_name = await resolve_country(db, country)
    since = date.today() - timedelta(days=days)
    rows = await db.fetch_all(
        f"""
        SELECT c.contester_id, c.contester_first_name, c.contester_last_name, c.contester_full_name,
               c.contester_country_name, x.role, x.division, x.levelup_date,
               count(*) OVER ()::int AS total
        FROM contester_view_roles c
        CROSS JOIN LATERAL (VALUES
            ('Leader', c.leader_current_division_name, {_entered_case('leader')}),
            ('Follower', c.follower_current_division_name, {_entered_case('follower')})
        ) AS x(role, division, levelup_date)
        WHERE x.division IS NOT NULL AND x.division <> 'Newcomer'
          AND x.levelup_date >= %(since)s
          AND (%(country)s::text IS NULL OR c.contester_country_name = %(country)s)
          AND (%(division)s::text IS NULL OR x.division = %(division)s)
        ORDER BY x.levelup_date DESC, c.contester_last_name
        LIMIT %(limit)s
        """,
        {"since": since, "country": country_name, "division": division, "limit": limit},
    )
    return RecentLevelupsResult(
        since=since,
        country=country_name,
        division=division,
        total=rows[0]["total"] if rows else 0,
        levelups=[
            LevelupEntry(
                wsdc_id=r["contester_id"],
                name=_display_name(r["contester_first_name"], r["contester_last_name"], r["contester_full_name"]),
                country=r["contester_country_name"],
                role=r["role"],
                division=r["division"],
                date=_d(r["levelup_date"]),
            )
            for r in rows
        ],
    )


@cached(ttl=3600)
async def division_stats(db: Database, country: str | None, active_within_months: int | None) -> DivisionStatsResult:
    country_name = await resolve_country(db, country)
    active_since = None
    if active_within_months:
        active_since = date.today() - timedelta(days=round(active_within_months * 30.44))
    rows = await db.fetch_all(
        """
        SELECT x.division, x.role, count(*)::int AS n
        FROM contester_view_roles c
        CROSS JOIN LATERAL (VALUES
            ('Leader', c.leader_current_division_name),
            ('Follower', c.follower_current_division_name)
        ) AS x(role, division)
        WHERE x.division IS NOT NULL
          AND (%(country)s::text IS NULL OR c.contester_country_name = %(country)s)
          AND (%(since)s::date IS NULL OR c.contester_end_date >= %(since)s)
        GROUP BY x.division, x.role
        """,
        {"country": country_name, "since": active_since},
    )
    dancers = await db.fetch_one(
        """
        SELECT count(*)::int AS n FROM contester_view c
        WHERE (%(country)s::text IS NULL OR c.contester_country_name = %(country)s)
          AND (%(since)s::date IS NULL OR c.contester_end_date >= %(since)s)
        """,
        {"country": country_name, "since": active_since},
    )
    counts: dict[str, DivisionCount] = {d: DivisionCount(division=d, leaders=0, followers=0) for d in SKILL_DIVISIONS}
    for r in rows:
        dc = counts.setdefault(r["division"], DivisionCount(division=r["division"], leaders=0, followers=0))
        if r["role"] == "Leader":
            dc.leaders = r["n"]
        else:
            dc.followers = r["n"]
    return DivisionStatsResult(
        country=country_name,
        active_since=active_since,
        dancers=dancers["n"] if dancers else 0,
        divisions=[c for c in counts.values() if c.leaders or c.followers],
    )


@cached(ttl=900)
async def data_freshness(db: Database) -> dict[str, Any]:
    row = await db.fetch_one(
        "SELECT max(event_start_date) AS latest_event, (SELECT count(*) FROM contester_view)::int AS dancers, "
        "count(*)::int AS events FROM event"
    )
    return {
        "latest_event_date": _d(row["latest_event"]) if row else None,
        "dancers": row["dancers"] if row else None,
        "events": row["events"] if row else None,
    }
