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

One story makes many terms busy at once: "beau", "lamarre", "condon", "beau lamarre",
"guilty", "jesse baird"... So flagged terms are grouped into topics, for saying what's
busy (never for scoring: each term is still its own signal). Two terms are one topic when
they're mostly in the same headlines and posts (at least half of the larger one's, and at
least 2, or 1 when one's words are part of the other's: "fire" and "fire ant"), or, with
nothing to compare, when one's words are part of the other's ("beach" and "bondi beach").
It errs towards keeping topics apart: an everyday word in two stories mustn't glue a fire
to a court case and hide it. A topic is named after its busiest phrase, joined
with phrases that nearly always come with it ("beau lamarre" + "lamarre condon" -> "beau
lamarre condon"), and shows the headline that matches it best: one with its name in, or
several of its words (never a post). A lone word that's in no topic ("making") isn't
worth naming in an alert; it's only counted.
"""

from __future__ import annotations

import hashlib
import re
import time
import json
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
    topic: str = ""  # the name of the topic it's part of, once flagged

    @property
    def kind(self) -> str:
        return "social" if self.social > self.news else "news"

    def to_dict(self) -> dict[str, Any]:
        return {"term": self.term, "mentions": self.mentions, "sources": self.sources, "usual": round(self.usual, 2),
                "news": self.news, "social": self.social, "kind": self.kind, "place": self.place,
                "flagged": self.flagged, "since": self.since, "samples": self.samples, "topic": self.topic}


# Two terms are one topic when they share at least this share of the larger one's texts.
SAME_TOPIC = 0.5
# A phrase joins a topic's name when it's this busy compared with the name's busiest phrase.
NAME_JOINS = 0.75
MAX_LONE = 5  # lone busy words listed by name; the rest are counted


def term_pattern(term: str) -> re.Pattern[str]:
    """A term in a text, whatever joins its words: "lamarre condon" finds "Lamarre-Condon"."""
    return re.compile(r"\b" + r"[^a-z0-9]+".join(re.escape(w) for w in term.split()) + r"\b", re.IGNORECASE)


@dataclass
class Topic:
    """Busy terms that belong together: words and phrases from the same headlines and posts."""

    name: str
    terms: list[Term]
    sample: dict[str, Any] | None = None  # the headline that matches it best (never a post)
    named: bool = True  # worth naming; a lone everyday word ("making") isn't

    @property
    def strongest(self) -> Term:
        return max(self.terms, key=lambda t: (t.mentions, t.sources))

    @property
    def headline(self) -> str:
        return f'busy talk about "{self.name}"' if self.named else ""

    @property
    def line(self) -> str:
        """The headline about it, as a line for alerts: 'ABC News: "Beau Lamarre-Condon found guilty..."'."""
        if not self.sample:
            return ""
        outlet = self.sample.get("outlet") or "A headline"
        return f'{outlet}: "{self.sample["text"]}"'


def group(terms: list[Term], texts: dict[str, set[Any]]) -> list[list[Term]]:
    """Flagged terms in groups that belong together (see the module docstring)."""
    parent = {t.term: t.term for t in terms}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(terms):
        for b in terms[i + 1:]:
            ta, tb = texts.get(a.term, set()), texts.get(b.term, set())
            wa, wb = set(a.term.split()), set(b.term.split())
            part = wa <= wb or wb <= wa
            if ta and tb:
                shared = len(ta & tb)
                together = shared >= SAME_TOPIC * max(len(ta), len(tb)) and (shared >= 2 or (part and shared >= 1))
            else:
                together = part
            if together:
                parent[root(a.term)] = root(b.term)
    found: dict[str, list[Term]] = {}
    for t in terms:
        found.setdefault(root(t.term), []).append(t)
    return list(found.values())


def name_of(terms: list[Term]) -> str:
    """The busiest phrase, joined with phrases that nearly always come with it."""
    pairs = [t for t in terms if len(t.term.split()) == 2]
    best = max(pairs or terms, key=lambda t: (t.mentions, t.sources, len(t.term)))
    name = best.term.split()
    grown = True
    while grown and len(name) < 4:
        grown = False
        for t in pairs:
            first, second = t.term.split()
            if t.mentions < NAME_JOINS * best.mentions:
                continue
            if first == name[-1] and second not in name:
                name.append(second)
            elif second == name[0] and first not in name:
                name.insert(0, first)
            else:
                continue
            grown = True
            break
    return " ".join(name)


def best_headline(terms: list[Term], name: str, headlines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The headline matching the most of a topic's terms (its name counts most); the newest wins a tie.
    One that only shares a word or two with a big topic isn't about it ("police" in a story about
    a cannabis raid), so it needs the name, or at least half the terms (2 at least)."""
    by_name = term_pattern(name)
    patterns = [term_pattern(t.term) for t in terms]
    enough = max(2, len(terms) // 2) if len(terms) > 1 else 1
    best, score = None, 0
    for h in headlines:  # newest first
        matched = sum(1 for p in patterns if p.search(h["title"]))
        if not by_name.search(h["title"]) and matched < enough:
            continue
        found = 100 * bool(by_name.search(h["title"])) + matched
        if found > score:
            best, score = h, found
    return {"text": best["title"], "outlet": best.get("outlet", ""), "url": best.get("url")} if best else None


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
        self.topics: list[Topic] = []  # flagged terms grouped, the busiest topic first

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
        headlines = self._headlines(now - window) if flagged else []
        posts = self.posts(now - window) if flagged else []
        texts: dict[str, set[Any]] = {}
        for t in flagged:
            t.samples, texts[t.term] = self._samples(t.term, headlines, posts)
        self.topics = self._topics(flagged, texts, headlines)
        self.terms = sorted(terms, key=lambda t: (not t.flagged, not t.place, -t.mentions))[:30]
        return self.terms

    def _headlines(self, since: float) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT title, url, ts, details FROM items WHERE source_id = 'news' AND ts >= ? ORDER BY ts DESC LIMIT 200",
            (since,))
        return [{"title": r["title"], "url": r["url"], "ts": r["ts"],
                 "outlet": (json.loads(r["details"] or "{}") or {}).get("outlet", "")} for r in rows]

    def _topics(self, flagged: list[Term], texts: dict[str, set[Any]], headlines: list[dict[str, Any]]) -> list[Topic]:
        # Groups whose best headline is the same are one story ("jesse baird" and "beau lamarre
        # condon" both match "Beau Lamarre-Condon found guilty of murdering Jesse Baird..."). Not
        # a lone word: any headline with it in is its best, which says little.
        stories: dict[Any, list[Term]] = {}
        for i, terms in enumerate(group(flagged, texts)):
            sample = best_headline(terms, name_of(terms), headlines) if len(terms) > 1 else None
            stories.setdefault(sample["text"] if sample else i, []).extend(terms)
        topics = []
        for terms in stories.values():
            name = name_of(terms)
            named = len(terms) >= 2 or any(t.place for t in terms)
            topics.append(Topic(name, terms, best_headline(terms, name, headlines), named))
        topics.sort(key=lambda tp: (not tp.named, -tp.strongest.sources, -tp.strongest.mentions))
        if topics and not topics[0].named:
            topics[0].named = True  # nothing else to name: say what's busiest
        for tp in topics:
            for t in tp.terms:
                t.topic = tp.name
        return topics

    def topic_of(self, t: Term) -> Topic | None:
        return next((tp for tp in self.topics if any(x.term == t.term for x in tp.terms)), None)

    def _passes(self, mentions: int, sources: int, usual: float) -> bool:
        s = self.s
        return mentions > s.min_mentions and mentions > s.ratio * max(usual, 1.0) and sources >= s.min_sources

    def alertable(self, t: Term) -> bool:
        """Would the term be flagged on mentions that were new enough when seen to raise an alert?"""
        return self._passes(t.fresh_mentions, t.fresh_sources, t.usual)

    @staticmethod
    def _samples(term: str, headlines: list[dict[str, Any]], posts: list[Any]) -> tuple[list[dict[str, Any]], set[Any]]:
        """Up to 3 headlines and 3 posts with the term in, to show; and every headline and post
        with it in, to tell which terms go together. Posts are only looked at, never saved."""
        pattern = term_pattern(term)
        found: list[dict[str, Any]] = []
        texts: set[Any] = set()
        for h in headlines:
            if pattern.search(h["title"]):
                texts.add(("news", h["url"] or h["title"]))
                if len(found) < 3:
                    found.append({"kind": "news", "text": h["title"], "url": h["url"], "at": h["ts"]})
        shown = len(found)
        for i, post in enumerate(posts):
            if pattern.search(post.title):
                texts.add(("post", i))
                if len(found) < shown + 3:
                    found.append({"kind": "social", "text": post.title, "url": None, "at": post.at})  # shown, never saved
        return found, texts

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
            signals = []
            for t in found:
                topic = self.topic_of(t)
                named = topic is not None and topic.named
                signals.append(Signal(
                    f"term:{t.term}", Level.ELEVATED, (self._scope(t),), title=self._describe(t),
                    alertable=alertable_only or self.alertable(t), headline=topic.headline if named else "",
                    lines=(topic.line,) if named and topic.line else ()))
            reason = self._describe_all(found)
            views.append(SourceView(
                id=f"terms:{category.value}", name="Emerging terms", category=category, link=None,
                level=Level.ELEVATED, reason=reason, flags=frozenset(), details={}, last_known_level=Level.ELEVATED,
                last_known_reason=reason, observed_at=now, last_success_at=now, last_attempt_at=now, last_error=None,
                failures=0, signals=tuple(signals), derived=True,
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

    def _describe_all(self, terms: list[Term]) -> str:
        """Busy terms by topic, named topics first: '"beau lamarre condon" and 12 related words are
        busy: ...; also busy: "north", "making"'."""
        parts, lone = [], []
        wanted = {t.term for t in terms}
        for tp in self.topics:
            here = [t for t in tp.terms if t.term in wanted]
            if not here:
                continue
            if not tp.named:
                lone += [f'"{t.term}"' for t in here]
                continue
            top = tp.strongest  # the whole topic, so a topic across headlines and posts reads the same in both
            others = sum(1 for t in tp.terms if t.term != tp.name)
            related = f" and {others} related word{'' if others == 1 else 's'}" if others else ""
            parts.append(f'"{tp.name}"{related} {"are" if related else "is"} busy: {top.mentions} mentions in '
                         f"{self.s.window_hours:g} hours from {top.sources} sources, usually about {top.usual:.1f}")
        grouped = {t.term for tp in self.topics for t in tp.terms}
        parts += [self._describe(t) for t in terms if t.term not in grouped]
        if lone:
            more = f" and {len(lone) - MAX_LONE} more" if len(lone) > MAX_LONE else ""
            parts.append("also busy: " + ", ".join(lone[:MAX_LONE]) + more)
        return "; ".join(parts)

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
            reason = self._describe_all(flagged)
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
