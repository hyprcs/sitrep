"""Story clusters: news, official and social items about the same event, grouped together.

Every scoring pass, sitrep looks at the last 6 hours of items: headlines (mainstream),
warnings, hazards and disruptions (official), and Bluesky posts that name a Sydney place
(social). Two items are about the same story when both of these hold:

- the same place: the same suburb, the same or a neighbouring 2 km square, or both about
  Sydney as a whole; and
- overlapping words: two distinctive words in common, or the same "event word" (crash,
  fire, storm, flood, shooting, stabbing, evacuation, lockdown, explosion, outage, siege,
  collapse, train, police), where "bushfire" and "blaze" both count as "fire", and so on.
  "Police" alone only counts for items pinned to a place: it's in too many headlines to
  tie two items about Sydney as a whole together.

An item joins the first story it matches, oldest first; a story's first item names it, so
it keeps the same id as it grows. A new item is matched against the story's first item,
headlines and official items, never against posts that joined later. Otherwise a story
drifts: a post shares two words with a post that shared one with the headline, and so on,
until it's about something else.

Corroboration counts kinds of source, not outlets: three outlets running the same wire
story are still one kind. And an official item only counts when it matters on its own (a
major Live Traffic hazard, an RFS Watch and Act or Emergency Warning, a BoM severe warning
for Sydney, an unplanned train disruption); routine ones still join a story, but don't
count as "official". Likewise a post that shares a link joins a story but doesn't count as
"social": it's passing on an article (often a bot reposting headlines), not someone saying
what they can see.

"About Sydney as a whole" means an item names Sydney itself. Naming only NSW isn't enough:
a storm "in NSW" is as likely to be on the Southern Tablelands.

- 2 kinds: elevated, in every category the story spans, with the story as its scope, so
  a story of news plus official is enough to make the overall status Elevated.
- All 3 kinds ("triangulated"): shown at the top of the dashboard; still elevated.
- Critical only when a triangulated story is inside Greater Sydney and either includes an
  emergency-level item (an RFS Emergency Warning, a BoM severe warning for Sydney) or
  matches one of the keyword groups under sources.bluesky. A big motorway crash with news
  coverage and posts about it is elevated, not critical.

A story can only raise an alert with items that were new enough when sitrep saw them
(backlog after an outage still forms stories on the dashboard).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .db import Database
from .geo import cell_of
from .models import SYDNEY, Assessment, Category, Level, Signal, area, cluster
from .places import gazetteer
from .state import SourceView

WINDOW_HOURS = 6
TYPES = ("official", "mainstream", "social")
TYPE_WORDS = {"official": "official", "mainstream": "news", "social": "social"}

# Words that mean the same event, folded to one.
EVENTS = {
    "fire": {"fire", "fires", "bushfire", "bushfires", "blaze", "blazes", "grassfire", "inferno", "burning"},
    "crash": {"crash", "crashes", "crashed", "collision", "smash", "rollover", "pileup"},
    "storm": {"storm", "storms", "thunderstorm", "thunderstorms", "hail", "hailstorm", "tornado", "supercell"},
    "flood": {"flood", "floods", "flooding", "flooded", "floodwaters"},
    "shooting": {"shooting", "shootings", "shot", "shots", "gunman", "gunfire"},
    "stabbing": {"stabbing", "stabbings", "stabbed", "knife"},
    "evacuate": {"evacuate", "evacuated", "evacuating", "evacuation", "evacuations"},
    "lockdown": {"lockdown", "locked"},
    "explosion": {"explosion", "explosions", "blast", "exploded"},
    "outage": {"outage", "outages", "blackout", "blackouts"},
    "siege": {"siege", "hostage", "hostages"},
    "collapse": {"collapse", "collapsed"},
    "train": {"train", "trains", "rail", "railway"},
    "police": {"police", "officers", "tactical", "riot"},
}
# Event words too common to tie two items together on their own unless both are pinned to
# a place: most Sydney crime news mentions police, so it can't tell two Sydney-wide items apart.
NEEDS_A_PLACE = {"police"}
# A story counts towards the status while its newest item is this recent. After that it's
# still shown (for WINDOW_HOURS) but no longer raises anything.
ACTIVE_HOURS = 2
FOLD = {word: event for event, words in EVENTS.items() for word in words}
# Common words, and the boilerplate in official titles, that say nothing about the story.
STOPWORDS = set("""
a about after again against all also am an and any are as at be been before being below between both but by can
could did do does down during each few for from further had has have having he her here hers him his how i if in
into is it its just me more most my near new no nor not now of off on once only or other our out over own same says
said say she should so some such than that the their them then there these they this those through to too under
until up very was we were what when where which while who whom why will with would you your amid as live latest
update updates breaking news today tonight yesterday week day days year years man woman men women people person
two three four five one first last told says set get gets after report reports reported via
rfs warning emergency advice watch act major hazard hazards traffic bom live nsw sydney
""".split())


def words(text: str, places: Iterable[str] = ()) -> set[str]:
    """The distinctive words in a title: lower case, place names and common words removed,
    plurals trimmed, and event words folded together ("bushfire" -> "fire")."""
    text = text.lower()
    for place in places:
        text = text.replace(place.lower(), " ")
    found = set()
    for token in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text):
        token = token.split("'")[0]
        if token in FOLD:
            found.add(FOLD[token])
            continue
        if token in STOPWORDS or token.isdigit() or (len(token) < 3 and not any(c.isdigit() for c in token)):
            continue
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        found.add(FOLD.get(token, token))
    return found


@dataclass
class Member:
    """One item in a story."""

    key: str  # "<source>:<item id>", unique
    source: str
    source_name: str
    source_type: str  # official, mainstream or social
    category: Category
    title: str
    at: float
    url: str | None
    words: set[str]
    place: str | None  # a suburb name, if it names one
    cell: tuple[int, int] | None
    sydney_wide: bool
    notable: bool  # counts as its kind (routine official items and posts sharing a link don't)
    emergency: bool
    alertable: bool
    lat: float | None = None
    lon: float | None = None
    outlet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "source": self.source, "source_name": self.source_name, "type": self.source_type,
                "title": self.title, "at": self.at, "url": self.url, "place": self.place, "notable": self.notable,
                "emergency": self.emergency, "outlet": self.outlet}

    def kept(self) -> dict[str, Any]:
        """What's saved to the database: a post's text, link and author are left out."""
        if self.source_type != "social":
            return self.to_dict()
        return {**self.to_dict(), "key": "social", "title": "A Bluesky post", "url": None}


@dataclass
class Story:
    id: str
    members: list[Member] = field(default_factory=list)
    keywords: tuple[str, ...] = ()

    @property
    def first(self) -> Member:
        return self.members[0]

    @property
    def anchors(self) -> list[Member]:
        """What a new item has to match: the story's first item, headlines and official items.
        Never the posts that joined later, so a story can't drift from post to post onto
        something else."""
        return [m for i, m in enumerate(self.members) if i == 0 or m.source_type != "social"]

    @property
    def placed(self) -> Member | None:
        return next((m for m in self.members if m.cell is not None), None)

    @property
    def sydney_wide(self) -> bool:
        return self.placed is None

    def kinds(self, alertable_only: bool = False) -> set[str]:
        return {m.source_type for m in self.members if m.notable and (m.alertable or not alertable_only)}

    def active(self, now: float) -> bool:
        return max(m.at for m in self.members) >= now - ACTIVE_HOURS * 3600

    @property
    def triangulated(self) -> bool:
        return self.kinds() >= set(TYPES)

    @property
    def title(self) -> str:
        """A headline if there is one, else an official title. Never a post's text, which isn't
        kept once its author deletes it."""
        for kind in ("mainstream", "official"):
            found = next((m for m in self.members if m.source_type == kind), None)
            if found:
                return found.title
        return f"Bluesky posts about {self.where}"

    @property
    def where(self) -> str:
        named = next((m.place for m in self.members if m.place), None)
        if named:
            return "Sydney CBD" if named == "Sydney" else named  # the suburb called Sydney is the CBD
        return "Sydney" if self.sydney_wide else "Greater Sydney"

    def level(self, alertable_only: bool = False) -> Level:
        kinds = self.kinds(alertable_only)
        if len(kinds) < 2:
            return Level.NORMAL
        members = [m for m in self.members if m.alertable or not alertable_only]
        if kinds >= set(TYPES) and (any(m.emergency for m in members) or self.keywords):
            return Level.CRITICAL
        return Level.ELEVATED

    def scopes(self) -> tuple[str, ...]:
        placed = self.placed
        return (cluster(self.id), area(*placed.cell)) if placed else (cluster(self.id), SYDNEY)

    def describe(self) -> str:
        kinds = " + ".join(TYPE_WORDS[t] for t in TYPES if t in self.kinds())
        return f"Story in {self.where} ({kinds}): {self.title}"


def same_place(a: Member, b: Member) -> bool:
    """The same suburb, the same or a neighbouring 2 km square, or both about Sydney as a whole."""
    if a.cell is None or b.cell is None:
        return a.cell is None and b.cell is None
    if a.place and a.place == b.place:
        return True
    return abs(a.cell[0] - b.cell[0]) <= 1 and abs(a.cell[1] - b.cell[1]) <= 1


def same_words(a: Member, b: Member) -> bool:
    """Two distinctive words in common, or the same event word."""
    shared = a.words & b.words
    events = shared & EVENTS.keys()
    if a.cell is None:
        events -= NEEDS_A_PLACE
    return len(shared) >= 2 or bool(events)


def same_story(item: Member, story: Story) -> bool:
    return any(same_place(item, a) and same_words(item, a) for a in story.anchors)


def group(members: list[Member]) -> list[Story]:
    """Group items into stories, oldest first. A story's id comes from its first item."""
    stories: list[Story] = []
    for m in sorted(members, key=lambda m: (m.at, m.key)):
        home = next((s for s in stories if same_story(m, s)), None)
        if home is None:
            home = Story(hashlib.sha1(m.key.encode()).hexdigest()[:10])
            stories.append(home)
        home.members.append(m)
    return stories


# --- Turning stored items into members ---


def _notable(source: str, level: str, details: dict[str, Any]) -> bool:
    if source == "rfs":
        return level in ("critical", "elevated", "watch")  # Watch and Act or Emergency Warning
    if source == "livetraffic":
        return bool(details.get("major"))
    if source == "bom":
        return bool(details.get("sydney")) and level in ("critical", "elevated")
    if source == "transit":
        return True  # sitrep only keeps unplanned disruptions
    if source == "advice":
        return level in ("critical", "elevated")  # an FCDO update about terrorism, unrest and the like
    return True  # news and social items always count as their kind


def member_from_item(row: dict[str, Any], names: dict[str, tuple[str, Category]]) -> Member | None:
    """A stored item as a story member, or None if it has no place in Sydney."""
    d = row["details"]
    source = row["source_id"]
    lat, lon = d.get("lat"), d.get("lon")
    places = d.get("places") or []
    place: str | None = None
    if lat is not None and lon is not None:
        if source == "news":
            place = next((p for p in places if (g := gazetteer().get(p)) and g.sydney), None)
        else:
            place = d.get("suburb") or None
        cell = cell_of(lat, lon)
        sydney_wide = False
        if not _in_sydney(lat, lon):
            return None
    else:
        cell = None
        sydney_wide = bool(d.get("sydney_wide")) or source == "transit" or (source == "bom" and d.get("sydney"))
        if not sydney_wide:
            return None
    name, category = names.get(source, (source, Category.INFO))
    return Member(
        key=f"{source}:{row['item_id']}", source=source, source_name=name, source_type=row["source_type"],
        category=category, title=row["title"], at=row["ts"] or row["version_seen"], url=row["url"],
        words=words(row["title"], [*places, *([place] if place else [])]), place=place, cell=cell,
        sydney_wide=sydney_wide, notable=_notable(source, row["level"], d),
        emergency=bool(d.get("emergency")) or d.get("level") == "Emergency Warning",
        alertable=bool(row["alertable"]), lat=lat, lon=lon, outlet=d.get("outlet", ""),
    )


def _in_sydney(lat: float, lon: float) -> bool:
    """Roughly Greater Sydney: the map's area. Stories outside it aren't local stories."""
    return -34.40 <= lat <= -32.95 and 149.95 <= lon <= 151.70


class Stories:
    """Works out the current stories every scoring pass, and what they mean for scoring."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time, *,
                 names: dict[str, tuple[str, Category]] | None = None,
                 keyword_groups: Callable[[str], list[str]] | None = None,
                 social: Callable[[float], list[Member]] | None = None) -> None:
        self.db = db
        self.clock = clock
        self.names = names or {}  # source id -> (name, category)
        self.keyword_groups = keyword_groups or (lambda text: [])
        self.social = social or (lambda since: [])  # recent Bluesky posts about Sydney places
        self.stories: list[Story] = []
        self._announced: dict[str, str] = {}  # story id -> what the timeline last said about it

    def update(self, now: float | None = None) -> list[Story]:
        now = self.clock() if now is None else now
        since = now - WINDOW_HOURS * 3600
        rows = self.db.query(
            """SELECT source_id, item_id, source_type, ts, version_seen, alertable, level, title, url, details
               FROM items WHERE COALESCE(ts, version_seen) >= ? AND source_type IN ('official', 'mainstream', 'social')""",
            (since,),
        )
        members = []
        for row in rows:
            row = {**dict(row), "details": json.loads(row["details"])}
            if (m := member_from_item(row, self.names)) is not None:
                members.append(m)
        members += self.social(since)
        stories = [s for s in group(members) if len(s.members) >= 2]
        for story in stories:
            story.keywords = tuple(self.keyword_groups(" \n".join(m.title for m in story.members)))
        self.stories = sorted(stories, key=lambda s: (-len(s.kinds()), -max(m.at for m in s.members)))
        self._remember(now)
        return self.stories

    @property
    def corroborated(self) -> list[Story]:
        return [s for s in self.stories if len(s.kinds()) >= 2]

    def views(self, now: float, alertable_only: bool = False) -> list[SourceView]:
        """Stand-in source views, one per category a corroborated story spans, for scoring."""
        by_category: dict[Category, list[tuple[Story, Level]]] = {}
        for story in self.stories:
            level = story.level(alertable_only)
            if level is Level.NORMAL or not story.active(now):
                continue
            for category in {m.category for m in story.members if m.notable and (m.alertable or not alertable_only)}:
                by_category.setdefault(category, []).append((story, level))
        views = []
        for category, found in by_category.items():
            worst = Level.CRITICAL if any(lv is Level.CRITICAL for _, lv in found) else Level.ELEVATED
            signals = tuple(
                Signal(f"cluster:{s.id}", lv, s.scopes(), title=s.describe(), alertable=alertable_only or self._fresh(s),
                       emergency=any(m.emergency for m in s.members), keywords=s.keywords)
                for s, lv in found
            )
            reason = "; ".join(s.describe() for s, _ in found)
            views.append(SourceView(
                id=f"stories:{category.value}", name="Story clusters", category=category, link=None, level=worst,
                reason=reason, flags=frozenset(), details={}, last_known_level=worst, last_known_reason=reason,
                observed_at=now, last_success_at=now, last_attempt_at=now, last_error=None, failures=0,
                signals=signals, derived=True,
            ))
        return views

    @staticmethod
    def _fresh(story: Story) -> bool:
        return story.level(alertable_only=True) is not Level.NORMAL

    def assessment(self, now: float) -> Assessment:
        """What the "Story clusters" tile shows. It's information only: scoring uses views()."""
        shown = self.corroborated
        tri = [s for s in shown if s.triangulated]
        if shown:
            reason = f"{len(shown)} stor{'y' if len(shown) == 1 else 'ies'} with more than one kind of source"
            if tri:
                reason += f", {len(tri)} triangulated (official, news and social)"
            reason += ": " + "; ".join(s.describe() for s in shown[:3])
        else:
            reason = "No story has more than one kind of source"
        level = Level.NORMAL
        live = [s for s in shown if s.active(now)]
        if live:
            level = Level.CRITICAL if any(s.level() is Level.CRITICAL for s in live) else Level.ELEVATED
        details = {
            "window_hours": WINDOW_HOURS,
            "stories": [self._story_dict(s, now) for s in self.stories[:20]],
            "map": [
                {"kind": "story", "id": s.id, "title": s.title, "label": "Story: " + " + ".join(
                    TYPE_WORDS[t] for t in TYPES if t in s.kinds()), "mark": s.level().value if s.level() is not
                 Level.NORMAL else "info", "lat": s.placed.lat, "lon": s.placed.lon,
                 "at": max(m.at for m in s.members), "detail": f"{len(s.members)} items", "km": None}
                for s in shown if s.placed is not None
            ],
        }
        return Assessment(level, reason, details, content_at=None)

    def _story_dict(self, s: Story, now: float) -> dict[str, Any]:
        return {
            "id": s.id, "title": s.title, "where": s.where, "kinds": [t for t in TYPES if t in s.kinds()],
            "triangulated": s.triangulated, "level": s.level().value if s.active(now) else "info",
            "active": s.active(now), "keywords": list(s.keywords),
            "members": [m.to_dict() for m in s.members[:12]], "size": len(s.members),
            "first_at": min(m.at for m in s.members), "last_at": max(m.at for m in s.members),
        }

    def _remember(self, now: float) -> None:
        """Keep corroborated stories for 7 days, and put them on the timeline as they grow."""
        for s in self.stories:
            kinds = s.kinds()
            if len(kinds) < 2:
                continue
            self.db.execute(
                """INSERT INTO clusters (id, first_at, last_at, title, place, level, kinds, members, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET last_at = excluded.last_at, title = excluded.title,
                       place = excluded.place, level = excluded.level, kinds = excluded.kinds,
                       members = excluded.members, updated_at = excluded.updated_at""",
                (s.id, min(m.at for m in s.members), max(m.at for m in s.members), s.title, s.where,
                 s.level().value, json.dumps(sorted(kinds)), json.dumps([m.kept() for m in s.members[:50]]), now),
            )
            stage = "triangulated" if s.triangulated else "corroborated"
            if self._announced.get(s.id) != stage:
                self._announced[s.id] = stage
                self.db.add_event(
                    now, "stories", "info", s.level().value,
                    f"{'Triangulated story' if s.triangulated else 'Story'}: {s.title}",
                    url=next((m.url for m in s.members if m.source_type == "mainstream" and m.url), None),
                    details={"reason": s.describe()}, dedupe_key=f"cluster:{s.id}:{stage}",
                )
