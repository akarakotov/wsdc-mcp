"""Keyword search over the WSDC Registry Event Rules text.

The rules document is loaded from a plain-text file (see WSDC_MCP_RULES_PATH), split into
sections by its numbered headings and charts, and ranked with a small TF-IDF-style score.
No external search engine is needed for a ~50 KB document.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)+\.?\s+[A-Z].{2,90}|Chart \d+:.{3,100}|Overview|Definitions)$")
TOKEN_RE = re.compile(r"[a-z0-9]+")
MAX_CHUNK_CHARS = 2400

STOPWORDS = frozenset(
    """a an and are as at be by can do does for from has have how i if in is it its may
    me must my not of on or so than that the their them then there these they this to
    up was what when where which who why will with you your wsdc rule rules""".split()
)

# Common synonyms people use that differ from the rulebook's wording.
SYNONYMS = {
    "allstar": ["all", "star"],
    "allstars": ["all", "star"],
    "champs": ["champion"],
    "levelup": ["move", "up"],
    "level": ["level", "move"],
    "jnj": ["jack", "jill"],
    "jj": ["jack", "jill"],
    "lead": ["leader"],
    "follow": ["follower"],
}


@dataclass(frozen=True)
class RulesSection:
    heading: str
    text: str


def _stem(token: str) -> str:
    # Crude prefix stemming: "points"/"pointed" -> "point", "competitors" -> "compet".
    return token[:6] if len(token) > 6 else token.rstrip("s") or token


def _terms(text: str) -> list[str]:
    out: list[str] = []
    for tok in TOKEN_RE.findall(text.lower()):
        for t in SYNONYMS.get(tok, [tok]):
            if t not in STOPWORDS and (len(t) > 1 or t.isdigit()):
                out.append(_stem(t))
    return out


def split_sections(text: str) -> list[RulesSection]:
    sections: list[RulesSection] = []
    heading = "Overview"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip("\n")
        if not body.strip():
            return
        # Split oversized sections at blank lines so each result stays readable.
        part: list[str] = []
        size = 0
        for para in re.split(r"\n\s*\n", body):
            if size + len(para) > MAX_CHUNK_CHARS and part:
                sections.append(RulesSection(heading, "\n\n".join(part)))
                part, size = [], 0
            part.append(para)
            size += len(para) + 2
        if part:
            sections.append(RulesSection(heading, "\n\n".join(part)))

    for line in text.splitlines():
        if HEADING_RE.match(line.strip()) and not line.startswith(" "):
            flush()
            heading = line.strip()
            buf = []
        else:
            buf.append(line)
    flush()
    return sections


class RulesIndex:
    def __init__(self, text: str, version: str):
        self.version = version
        self.text = text
        self.sections = split_sections(text)
        self._terms = [_terms(s.heading + "\n" + s.text) for s in self.sections]
        self._heading_terms = [set(_terms(s.heading)) for s in self.sections]
        n = len(self.sections)
        df: dict[str, int] = {}
        for terms in self._terms:
            for t in set(terms):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log(1 + n / c) for t, c in df.items()}

    @classmethod
    def from_file(cls, path: Path, version: str | None = None) -> "RulesIndex":
        text = path.read_text(encoding="utf-8")
        if not version:
            m = re.search(r"(\d{4}\.\d[A-Z]?)", path.name)
            version = m.group(1) if m else "unknown"
        return cls(text, version)

    @property
    def headings(self) -> list[str]:
        seen: list[str] = []
        for s in self.sections:
            if s.heading not in seen:
                seen.append(s.heading)
        return seen

    def search(self, query: str, limit: int = 4) -> list[RulesSection]:
        q_terms = _terms(query)
        if not q_terms:
            return []
        scored: list[tuple[float, int]] = []
        for i, terms in enumerate(self._terms):
            if not terms:
                continue
            counts: dict[str, int] = {}
            for t in terms:
                counts[t] = counts.get(t, 0) + 1
            score = 0.0
            matched = 0
            for qt in set(q_terms):
                c = counts.get(qt, 0)
                if c:
                    matched += 1
                    score += (1 + math.log(c)) * self._idf.get(qt, 0.0)
                    if qt in self._heading_terms[i]:
                        score += 2 * self._idf.get(qt, 0.0)
            if matched:
                # Prefer sections that cover more of the query, and damp very long sections.
                coverage = matched / len(set(q_terms))
                score *= 0.5 + coverage
                score /= 1 + math.log(1 + len(terms) / 400)
                scored.append((score, i))
        scored.sort(reverse=True)
        return [self.sections[i] for _, i in scored[:limit]]
