"""Headlines: what is happening and where, in a few words, for alerts and the dashboard.

The overall level comes from signals agreeing, and its reason lists them all ("Convergence:
Convergence at Sydney CBD: road hazard, posts, aircraft circling; and Emerging terms: ...").
That says how sitrep decided, not what is going on. A summary leads instead with the one
signal that says most about what's happening ("Gas leak, Sydney CBD"), a few lines on what's
behind it, and anything else raised alongside it on one short line.

Which signal leads: the most severe; then an emergency-level one; then a story (it has a
headline), a convergence, a single source, and busy words last; then the first in category
order (official before news and social).

Rules only, no AI: a model that's wrong, slow or switched off mustn't be able to garble or
hide an alert. Nothing here changes a level or when alerts are sent.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import SEVERITY, Signal

MAX_ALSO = 3  # other things named on the "also" line; the rest are counted
MAX_WORDS = 3  # busy words named


@dataclass(frozen=True)
class Summary:
    headline: str = ""  # "Gas leak, Sydney CBD"; empty when nothing is raised
    lines: tuple[str, ...] = ()  # what's behind it, most telling first
    also: str = ""  # other things raised alongside it, on one line


def _kind(s: Signal) -> int:
    if s.key.startswith("term:"):
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


def summarise(drivers: tuple[Signal, ...] | list[Signal]) -> Summary:
    """The headline, lines and "also" line for the signals behind a level."""
    unique = list({s.key: s for s in drivers}.values())  # a convergence is in several categories
    if not unique:
        return Summary()
    order = {s.key: i for i, s in enumerate(unique)}
    lead = max(unique, key=lambda s: (SEVERITY.get(s.level, 0), s.emergency, _kind(s), -order[s.key]))
    words = [s.key.removeprefix("term:") for s in unique if s.key.startswith("term:")]
    if lead.key.startswith("term:"):  # only busy words: they're the news
        text = busy_words(words)
        return Summary(text[:1].upper() + text[1:], lead.lines)
    others = list(dict.fromkeys(headline_of(s) for s in unique
                                if s is not lead and not s.key.startswith("term:")))
    others = [o for o in others if o and o != headline_of(lead)]
    parts = others[:MAX_ALSO]
    if len(others) > MAX_ALSO:
        parts.append(f"{len(others) - MAX_ALSO} more")
    if words:
        parts.append(busy_words(words))
    return Summary(headline_of(lead), lead.lines, "; ".join(parts))
