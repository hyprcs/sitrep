"""Place names: finding NSW suburbs and localities in headlines and posts.

The list is sitrep/data/places_nsw.csv, made by tools/build_places.py from the ABS's
Suburbs and Localities (© Commonwealth of Australia, ABS, CC BY 4.0). Each place has a
point, so a headline that names a suburb can go on the map and into the 2 km grid.

Matching is deliberately strict, because a wrong place is worse than none:

- A name only matches with its own capitals ("Ryde", "Macquarie Park"), as a whole word.
  Headlines capitalise place names, so "the ruse" doesn't match the suburb of Ruse.
- Longer names win: "Macquarie Park" rather than a "Macquarie" inside it.
- Some names are also places elsewhere: another state (the ABS marks those), somewhere
  famous overseas (Liverpool, Newcastle, Penrith, Richmond, ...), or more than one place
  in NSW. Those only count when the text is clearly about Sydney or NSW, or comes from a
  NSW-only feed.
- Outside Greater Sydney, a one-word name (Bell, Paterson) is often a surname, so it
  needs the same Sydney-or-NSW context. In social posts, which come from all over the
  world, every one-word name needs it ("Miller" is a Sydney suburb and a common surname).
- A name followed by a word that turns it into something else isn't a place: the
  "Sunshine Coast" isn't the NSW locality of Sunshine, and the "Penrith Panthers" are a
  football team.
- "Sydney" on its own means the city as a whole, not the CBD suburb: it makes a story
  Sydney-wide rather than placing it. "Sydney CBD", "the CBD" and a few CBD landmarks
  (Martin Place, Circular Quay, Wynyard, Town Hall, Darling Harbour) place it in the CBD.
  "NSW" helps recognise place names, but doesn't make a story Sydney-wide.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PLACES_FILE = Path(__file__).parent / "data" / "places_nsw.csv"

# Famous places overseas that share a name with an NSW suburb or town.
FOREIGN_NAMESAKES = {
    "Liverpool", "Newcastle", "Penrith", "Ryde", "Richmond", "Windsor", "Camden", "Paddington", "Kensington",
    "Greenwich", "Lewisham", "Croydon", "Canterbury", "Brighton", "Hampton", "Epping", "Concord", "Waterloo",
    "Hamilton", "Wellington", "Toronto", "Cambridge", "Oxford", "Bristol", "Darlington", "Exeter", "Chester",
    "Lancaster", "Leichhardt", "Belmont", "Albany", "Dover", "Portland", "Hastings", "Milton", "Sutherland",
    "Como", "Ascot", "Wembley", "Putney", "Chelsea", "Mayfield", "Stanmore", "Hornsby", "Peterborough",
}
# Words that mean a story is about Sydney or NSW. "Sydney" followed by another capitalised
# word is usually something else ("Sydney Sweeney", "Sydney Morning Herald"), except these.
SYDNEY_THINGS = "CBD|Harbour|Trains|Metro|Airport|Water|Opera|Olympic|Uni|University|Road|Police|Swans|FC"
CITY = rf"Sydney(?!\s+(?!(?:{SYDNEY_THINGS})\b)[A-Z])|Sydneysiders?|Western Sydney"
CONTEXT = re.compile(rf"\b(?:{CITY}|NSW|New South Wales)\b")
NAMES_SYDNEY = re.compile(rf"\b(?:{CITY})\b")
SYDNEY_WIDE = {"Sydney"}  # names that mean the whole city, not a point
# Names that mean the CBD (the suburb called Sydney), when the text is about Sydney or NSW.
CBD = re.compile(r"\b(Sydney CBD|CBD|Martin Place|Circular Quay|Wynyard|Town Hall|Darling Harbour)\b")
# Words that, straight after a place name, make it the name of something else.
NOT_A_PLACE_AFTER = {
    "Coast", "Plains", "Valley", "Ranges", "Panthers", "Eels", "Rabbitohs", "Roosters", "Sharks", "Bulldogs",
    "Dragons", "Knights", "Tigers", "Wanderers", "Warriors", "Sea", "FC", "United", "Giants", "Swans", "Kings",
    "Thunder", "Sixers", "Football", "Rugby",
    # Roads and train lines named after a place usually run far from it ("Parramatta Road").
    "Road", "Rd", "Street", "St", "Highway", "Hwy", "Motorway", "Avenue", "Ave", "Drive", "Parade", "Line",
}


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    sydney: bool  # inside Greater Sydney
    ambiguous: bool  # the name is also a place somewhere else
    code: str


class Gazetteer:
    def __init__(self, places: list[Place]) -> None:
        by_name: dict[str, list[Place]] = {}
        for place in places:
            by_name.setdefault(place.name, []).append(place)
        self._places: dict[str, Place] = {}
        for name, found in by_name.items():
            if len(found) == 1:
                self._places[name] = found[0]
                continue
            in_sydney = [p for p in found if p.sydney]
            # Several NSW places share the name: use the Sydney one if there's exactly one, marked ambiguous.
            if len(in_sydney) == 1:
                p = in_sydney[0]
                self._places[name] = Place(p.name, p.lat, p.lon, p.sydney, True, p.code)
        names = sorted(self._places, key=len, reverse=True)
        self._pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w-])")

    def __len__(self) -> int:
        return len(self._places)

    def get(self, name: str) -> Place | None:
        return self._places.get(name)

    def nearest(self, lat: float, lon: float) -> Place:
        """The Greater Sydney suburb whose point is nearest."""
        from .geo import km_between

        return min((p for p in self._places.values() if p.sydney), key=lambda p: km_between(lat, lon, p.lat, p.lon))

    def get_lower(self, name: str) -> Place | None:
        """A place by its name in any case ("mcmahons point")."""
        if not hasattr(self, "_lower"):
            self._lower = {n.lower(): p for n, p in self._places.items()}
        return self._lower.get(name.lower())

    def find(self, text: str, *, nsw_feed: bool = False, strict: bool = False) -> list[Place]:
        """The places a text names, in order, each once. strict is for social posts: every
        one-word name then needs a clue that the text is about Sydney or NSW."""
        about_nsw = nsw_feed or bool(CONTEXT.search(text))
        found: dict[str, Place] = {}
        for match in self._pattern.finditer(text):
            name = match.group(1)
            if name in SYDNEY_WIDE or name in found:
                continue
            following = re.match(r"\s+([A-Z][\w]*)", text[match.end():])
            if following and following.group(1) in NOT_A_PLACE_AFTER:
                continue
            place = self._places[name]
            one_word = " " not in name
            needs_context = (place.ambiguous or name in FOREIGN_NAMESAKES or (one_word and (strict or not place.sydney)))
            if needs_context and not about_nsw:
                continue
            found[name] = place
        cbd = self._places.get("Sydney")
        if cbd and about_nsw and "Sydney" not in found and CBD.search(text):
            found["Sydney"] = cbd
        return list(found.values())

    @staticmethod
    def about_nsw(text: str) -> bool:
        """True if the text names Sydney or NSW."""
        return bool(CONTEXT.search(text))

    @staticmethod
    def sydney_wide(text: str) -> bool:
        """True if the text is about Sydney as a whole: it names the city. Naming only NSW isn't
        enough, since a storm "in NSW" is as likely to be on the Southern Tablelands."""
        return bool(NAMES_SYDNEY.search(text))


def load(path: Path = PLACES_FILE) -> Gazetteer:
    places = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            places.append(Place(
                name=row["name"], lat=float(row["lat"]), lon=float(row["lon"]), sydney=row["sydney"] == "1",
                ambiguous=row["elsewhere"] == "1", code=row["code"],
            ))
    return Gazetteer(places)


@lru_cache(maxsize=1)
def gazetteer() -> Gazetteer:
    """The NSW place list, loaded once."""
    return load()
