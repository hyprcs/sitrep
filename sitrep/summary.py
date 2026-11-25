"""Headlines: what is happening and where, in a few words, for alerts and the dashboard.

The overall level comes from signals agreeing, and its reason lists them all ("Convergence:
Convergence at Sydney CBD: road hazard, posts, aircraft circling; and Emerging terms: ...").
That says how sitrep decided, not what is going on. A summary leads instead with the one
signal that says most about what's happening ("Gas leak, Sydney CBD"), a few lines on what's
behind it, and the rest on two short lines: what else backs it up ("also"), and what is
raised at the same time without being about the same thing ("not linked": busy talk about
a court case doesn't back up a gas leak in the CBD).

Which signal leads: of those that caused the level, the most severe; then an emergency-level
one; then a story (it has a headline), a convergence, a single source, and busy words last;
then the first in category order (official before news and social).

Busy words come as topics (see terms.py): every word of a topic carries the topic's
headline, so 18 busy words about one court case read as one thing, with a headline about
it. Lone everyday words ("making") are only counted.

Rules only, no AI: a model that's wrong, slow or switched off mustn't be able to garble or
hide an alert. Nothing here changes a level or when alerts are sent.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .models import SEVERITY, Signal

MAX_ALSO = 3  # other things named on a line; the rest are counted
MAX_TOPICS = 2  # busy topics named on a line
MAX_WORDS = 3  # busy words named, for words with no topic


@dataclass(frozen=True)
class Summary:
    headline: str = ""  # "Gas leak, Sydney CBD"; empty when nothing is raised
    lines: tuple[str, ...] = ()  # what's behind it, most telling first
    also: str = ""  # other things behind the level, on one line
    unlinked: str = ""  # things raised at the same time that aren't about the same thing


def _term(s: Signal) -> bool:
    return s.key.startswith("term:")


def _kind(s: Signal) -> int:
    if _term(s):
        return 0
    if s.key.startswith("cluster:"):
        return 3
    if s.key.startswith("convergence:"):
        return 2
    return 1


def headline_of(s: Signal) -> str:
    return s.headline or s.title


def busy_words(words: list[str]) -> str:
    named = ", ".join(f'"{w}"' for w in words[:MAX_WORDS])
    more = len(words) - MAX_WORDS
    return f"busy words {named}" + (f" and {more} more" if more > 0 else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe_others(signals: list[Signal], skip: str = "") -> str:
    """Signals on one line: each thing once, busy words as topics, lone busy words counted."""
    plain = list(dict.fromkeys(headline_of(s) for s in signals if not _term(s)))
    plain = [p for p in plain if p and p != skip]
    parts = plain[:MAX_ALSO]
    if len(plain) > MAX_ALSO:
        parts.append(f"{len(plain) - MAX_ALSO} more")
    terms = [s for s in signals if _term(s)]
    topics = list(dict.fromkeys((s.headline, s.lines[0] if s.lines else "") for s in terms if s.headline))
    topics = [t for t in topics if t[0] != skip]
    for headline, line in topics[:MAX_TOPICS]:
        parts.append(f"{headline} ({line})" if line else headline)
    lone = [s for s in terms if not s.headline]
    if topics and (lone or len(topics) > MAX_TOPICS):
        words = len(lone) + sum(1 for s in terms if s.headline in {h for h, _ in topics[MAX_TOPICS:]})
        parts.append(f"{_plural(words, 'other busy word')}")
    elif lone:
        parts.append(busy_words([s.key.removeprefix("term:") for s in lone]))
    return "; ".join(parts)


def summarise(drivers: tuple[Signal, ...] | list[Signal], linked: Collection[str] | None = None) -> Summary:
    """The headline, lines, "also" and "not linked" lines for the signals behind a level.

    linked holds the keys of the signals that caused the level (scoring works them out:
    those backed up by another category). None means all of them did."""
    unique = list({s.key: s for s in drivers}.values())  # a convergence is in several categories
    if not unique:
        return Summary()
    causes = [s for s in unique if linked is None or s.key in linked] or unique
    order = {s.key: i for i, s in enumerate(unique)}
    lead = max(causes, key=lambda s: (SEVERITY.get(s.level, 0), s.emergency, _kind(s), -order[s.key]))
    if _term(lead):  # busy words are the news
        headline = lead.headline or busy_words([s.key.removeprefix("term:") for s in causes if _term(s)])
        headline = headline[:1].upper() + headline[1:]
    else:
        headline = headline_of(lead)
    rest = [s for s in unique if s is not lead and not (_term(lead) and s.headline == lead.headline and _term(s))]
    if _term(lead) and not lead.headline:
        rest = [s for s in rest if not (_term(s) and not s.headline)]  # already named in the headline
    caused = {s.key for s in causes}
    also = describe_others([s for s in rest if s.key in caused], skip=headline_of(lead))
    unlinked = describe_others([s for s in rest if s.key not in caused], skip=headline_of(lead))
    return Summary(headline, lead.lines, also, unlinked)
