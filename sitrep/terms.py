"""Emerging terms: words that are suddenly far busier than usual in Sydney news and posts.

sitrep's keyword groups only catch what you thought to list. This catches the rest. Every
headline and Bluesky post about Sydney or NSW is broken into terms: single words, and
two-word phrases so names like "Macquarie Park" survive. Each mention is counted with
when it was made and who made it (the outlet, or a one-way code for a Bluesky account,
never its handle). No text is kept: only the terms.

A term is flagged when all of these hold (the numbers are under terms in config.yaml):

- more than 5 mentions in the last 2 hours;
- more than 3 times its usual for 2 hours, from the last 7 days;
- from at least 2 different sources (different outlets, or different Bluesky accounts);
- it wasn't flagged in the last 30 minutes.

Everyday words, and a blocklist you can edit, are never terms. Suburb names come first
on the dashboard.

A flagged term raises one category to elevated: social if most of its mentions are posts,
news if most are headlines. Never critical. Its scope is the suburb it names, if it is
one, otherwise Sydney as a whole, so it can back up another signal about the same place
but can't make the overall status Elevated on its own. It needs 7 days of history first,
and shows "learning" until then.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import TermsSettings
from .db import Database
from .geo import cell_of
from .models import SYDNEY, Assessment, Category, Level, Signal, area
from .places import gazetteer
from .state import SourceView
from .stories import STOPWORDS as STORY_STOPWORDS
from .util import format_duration

# Everyday English, on top of the words stories ignore.
ENGLISH = set("""
the a an and or but if then than that this these those there here where when what which who whom whose why how
is are was were be been being am do does did done have has had having will would shall should can could may might
must not no nor so too very just only also even still yet ever never always often again more most less least much
many some any all both each every few other another such own same into onto from with without within about above
below over under between through during before after since until while because though although per via
i me my mine we us our ours you your yours he him his she her hers it its they them their theirs
one two three four five first last next new old big small good bad best worst well really get got gets getting
go goes going gone went make makes made say says said see seen look looks know knows think thinks want wants
need needs come comes came take takes took give gives gave let lets like likes way ways thing things lot lots
don't doesn't didn't isn't aren't wasn't weren't won't can't couldn't wouldn't shouldn't i'm it's that's
there's here's what's you're we're they're i've you've we've they've i'll you'll we'll they'll i'd you'd
across around behind beside inside outside anyone anything anybody everyone everything everybody someone something
somebody nothing nobody right left really actually maybe probably literally pretty quite lol omg yeah yes okay
http https www com org net html amp bsky app
""".split()) | STORY_STOPWORDS
WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def who_code(author: str) -> str:
    """A one-way code for a Bluesky account: tells accounts apart without keeping who they are."""
    return hashlib.sha256(author.encode()).hexdigest()[:16]


def extract(text: str, blocklist: set[str]) -> set[str]:
    """The terms in a text: words and two-word phrases, lower case, minus everyday words.
    Suburb names the text mentions are kept whole, however many words they have."""
    places = gazetteer()
    found = {p.name.lower() for p in places.find(text) if p.sydney}
    tokens = [t.split("'")[0] for t in WORD.findall(text.lower())]
    def keep(token: str) -> bool:
        if any(c.isdigit() for c in token):
            # Only road names like m2, m4 or a32: not bits of times, dates or codes.
            return bool(re.fullmatch(r"[a-z]\d{1,2}", token)) and token not in blocklist
        return len(token) >= 3 and token not in ENGLISH and token not in blocklist
    for i, token in enumerate(tokens):
        if keep(token):
            found.add(token)
            if i + 1 < len(tokens) and keep(tokens[i + 1]):
                found.add(f"{token} {tokens[i + 1]}")
    return {t for t in found if t not in blocklist}


def record(db: Database, text: str, *, at: float, kind: str, who: str, fresh: bool, blocklist: set[str]) -> int:
    """Count the terms in one headline or post. Returns how many were counted."""
    terms = extract(text, blocklist)
    if terms:
        db.add_term_hits([(t, at, kind, who, int(fresh)) for t in terms])
    return len(terms)


def blocklist_of(settings: TermsSettings) -> set[str]:
    return {w.strip().lower() for w in settings.stopwords if w.strip()}


@dataclass
class Term:
    term: str
    mentions: int
    sources: int
    usual: float  # mentions usually seen in a window this long
    news: int
    social: int
    fresh_mentions: int
    fresh_sources: int
    place: bool = False  # a Greater Sydney suburb
    flagged: bool = False
    since: float | None = None  # when it was flagged
    samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "social" if self.social > self.news else "news"

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "mentions": self.mentions, "sources": self.sources, "usual": round(self.usual, 2),
                "news": self.news, "social": self.social, "kind": self.kind, "place": self.place,
                "flagged": self.flagged, "since": self.since, "samples": self.samples}


class Terms:
    """Works out which terms are emerging, on each scoring pass (at most once a minute)."""

    def __init__(self, db: Database, settings: TermsSettings, clock: Callable[[], float] = time.time, *,
                 posts: Callable[[float], list[Any]] | None = None) -> None:
        self.db = db
        self.s = settings
        self.clock = clock
        self.posts = posts or (lambda since: [])  # recent Bluesky posts (in memory), for samples
        self.terms: list[Term] = []  # the busiest recent terms, flagged ones first
        self.learning_left = 0.0
        self._checked = float("-inf")
        self._flagged: dict[str, float] = {}  # term -> when it was last flagged
        self._active: set[str] = set()  # terms flagged on the last check

    def update(self, now: float | None = None) -> list[Term]:
        now = self.clock() if now is None else now
        if now - self._checked < 55:
            return self.terms
        self._checked = now
        s = self.s
        window = s.window_hours * 3600
        # Learning counts from when sitrep started counting, not from the oldest headline it read.
        first = self.db.kv_get("terms:first_seen")
        if first is None:
            first = str(now)
            self.db.kv_set("terms:first_seen", first)
        self.learning_left = s.baseline_days * 86400 - (now - float(first))
        rows = self.db.query(
            """SELECT term, COUNT(*) AS n, COUNT(DISTINCT who) AS who,
                      SUM(kind = 'news') AS news, SUM(kind = 'social') AS social,
                      SUM(fresh) AS fresh_n, COUNT(DISTINCT CASE WHEN fresh THEN who END) AS fresh_who
               FROM term_hits WHERE ts >= ? AND ts <= ? GROUP BY term
               ORDER BY n DESC LIMIT 200""",
            (now - window, now + 600),
        )
        windows = s.baseline_days * 86400 / window
        places = gazetteer()
        terms = []
        for row in rows:
            before = self.db.query(
                "SELECT COUNT(*) AS n FROM term_hits WHERE term = ? AND ts >= ? AND ts < ?",
                (row["term"], now - window - s.baseline_days * 86400, now - window),
            )[0]["n"]
            place = places.get_lower(row["term"])
            terms.append(Term(
                term=row["term"], mentions=row["n"], sources=row["who"], usual=before / windows,
                news=row["news"] or 0, social=row["social"] or 0, fresh_mentions=row["fresh_n"] or 0,
                fresh_sources=row["fresh_who"] or 0, place=bool(place and place.sydney),
            ))
        for t in terms:
            if self.learning_left > 0 or not self._passes(t.mentions, t.sources, t.usual):
                continue
            last = self._flagged.get(t.term)
            if t.term in self._active:
                t.flagged = True  # still busy since it was flagged
            elif last is None or now - last >= s.repeat_minutes * 60:
                t.flagged, self._flagged[t.term] = True, now
            # otherwise it was flagged less than repeat_minutes ago, went quiet, and is back: not again yet
            t.since = self._flagged.get(t.term) if t.flagged else None
        self._active = {t.term for t in terms if t.flagged}
        flagged = [t for t in terms if t.flagged]
        for t in flagged:
            t.samples = self._samples(t.term, now - window)
        self.terms = sorted(terms, key=lambda t: (not t.flagged, not t.place, -t.mentions))[:30]
        return self.terms

    def _passes(self, mentions: int, sources: int, usual: float) -> bool:
        s = self.s
        return mentions > s.min_mentions and mentions > s.ratio * max(usual, 1.0) and sources >= s.min_sources

    def alertable(self, t: Term) -> bool:
        """Would the term be flagged on mentions that were new enough when seen to raise an alert?"""
        return self._passes(t.fresh_mentions, t.fresh_sources, t.usual)

    def _samples(self, term: str, since: float) -> list[dict[str, Any]]:
        pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
        found = []
        rows = self.db.query(
            "SELECT title, url, ts, details FROM items WHERE source_id = 'news' AND ts >= ? ORDER BY ts DESC LIMIT 200",
            (since,))
        for row in rows:
            if pattern.search(row["title"]):
                found.append({"kind": "news", "text": row["title"], "url": row["url"], "at": row["ts"]})
            if len(found) >= 3:
                break
        for post in self.posts(since):
            if len(found) >= 6:
                break
            if pattern.search(post.title):
                found.append({"kind": "social", "text": post.title, "url": None, "at": post.at})  # shown, never saved
        return found

    def views(self, now: float, alertable_only: bool = False) -> list[SourceView]:
        """A stand-in view for each category a flagged term raises, for scoring."""
        by_category: dict[Category, list[Term]] = {}
        for t in self.terms:
            if not t.flagged or (alertable_only and not self.alertable(t)):
                continue
            category = Category.SOCIAL if t.kind == "social" else Category.NEWS
            by_category.setdefault(category, []).append(t)
        views = []
        for category, found in by_category.items():
            signals = tuple(
                Signal(f"term:{t.term}", Level.ELEVATED, (self._scope(t),), title=self._describe(t),
                       alertable=alertable_only or self.alertable(t))
                for t in found
            )
            reason = "; ".join(self._describe(t) for t in found)
            views.append(SourceView(
                id=f"terms:{category.value}", name="Emerging terms", category=category, link=None,
                level=Level.ELEVATED, reason=reason, flags=frozenset(), details={}, last_known_level=Level.ELEVATED,
                last_known_reason=reason, observed_at=now, last_success_at=now, last_attempt_at=now, last_error=None,
                failures=0, signals=signals, derived=True,
            ))
        return views

    def _scope(self, t: Term) -> str:
        place = gazetteer().get_lower(t.term) if t.place else None
        return area(*cell_of(place.lat, place.lon)) if place else SYDNEY

    def _describe(self, t: Term) -> str:
        kinds = []
        if t.news:
            kinds.append(f"{t.news} in headlines")
        if t.social:
            kinds.append(f"{t.social} in posts")
        return (f'"{t.term}" is busy: {t.mentions} mentions in {self.s.window_hours:g} hours from {t.sources} sources '
                f"({', '.join(kinds)}), usually about {t.usual:.1f}")

    def assessment(self, now: float) -> Assessment:
        """What the "Emerging terms" tile shows. Information only: scoring uses views()."""
        flagged = [t for t in self.terms if t.flagged]
        if self.learning_left > 0:
            level = Level.LEARNING
            reason = f"Learning what's usual: {format_duration(self.learning_left)} to go"
            busiest = [t.term for t in self.terms[:5]]
            if busiest:
                reason += ". Busiest now: " + ", ".join(busiest)
        elif flagged:
            level = Level.ELEVATED
            reason = "; ".join(self._describe(t) for t in flagged[:3])
        else:
            level = Level.NORMAL
            reason = "No term is much busier than usual"
            if self.terms:
                top = self.terms[0]
                reason += f". Busiest: \"{top.term}\", {top.mentions} mentions (usually about {top.usual:.1f})"
        details = {
            "window_hours": self.s.window_hours,
            "learning_left": max(0.0, self.learning_left),
            "terms": [t.to_dict() for t in self.terms[:15]],
            "map": [
                {"kind": "term", "id": t.term, "title": f'Busy term: "{t.term}"', "label": "Emerging term",
                 "mark": "elevated", "lat": p.lat, "lon": p.lon, "at": t.since, "km": None,
                 "detail": f"{t.mentions} mentions from {t.sources} sources"}
                for t in flagged if t.place and (p := gazetteer().get_lower(t.term))
            ],
        }
        counted = Counter(t.kind for t in flagged)
        details["counts"] = dict(counted)
        return Assessment(level, reason, details)
