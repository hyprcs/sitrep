"""Emerging terms: counting words and phrases, and when a term is flagged."""

from __future__ import annotations

from sitrep.config import ScoringSettings, TermsSettings
from sitrep.db import Database
from sitrep.models import Category, Level, Overall
from sitrep.scoring import score_overall
from sitrep.terms import Terms, blocklist_of, extract, record, who_code

S = TermsSettings()
BLOCK = blocklist_of(S)
NOW = 1_793_412_000.0
HOUR = 3600


def hits(db, text, n, *, kind="social", who_prefix="acct", ago_minutes=30, fresh=True, sources=None):
    """n mentions of a text, from `sources` different authors (all different by default)."""
    for i in range(n):
        who = f"{who_prefix}{i % (sources or n)}"
        record(db, text, at=NOW - ago_minutes * 60 + i, kind=kind, who=who_code(who), fresh=fresh, blocklist=BLOCK)


def engine(db, settings=S):
    terms = Terms(db, settings, lambda: NOW)
    db.kv_set("terms:first_seen", str(NOW - 8 * 86400))  # counting for over a week: finished learning
    return terms


def flagged(terms):
    return [t.term for t in terms.update(NOW) if t.flagged]


# --- Terms ---


def test_terms_are_words_and_pairs_with_suburbs_kept_whole():
    found = extract("Strange chemical smell across Kellyville tonight", BLOCK)
    assert {"chemical", "chemical smell", "kellyville", "strange chemical"} <= found
    assert "tonight" not in found and "across" not in found  # everyday words
    assert "macquarie park" in extract("Crash at Macquarie Park", BLOCK)
    assert "sydney" not in extract("Storms over Sydney", BLOCK)  # on the blocklist you can edit
    assert extract("M2 closed at 31t02 2026", BLOCK) == {"m2", "closed", "m2 closed"}  # road names, not bits of times
    assert extract("join the fun at payphonetag.com", BLOCK) == {"join", "fun", "payphonetag"}  # no "com"


def test_accounts_are_counted_by_a_one_way_code():
    code = who_code("did:plc:abcdef")
    assert code != "did:plc:abcdef" and "did" not in code and code == who_code("did:plc:abcdef")


# --- Flagging ---


def test_a_new_busy_term_from_several_sources_is_flagged():
    db = Database(":memory:")
    hits(db, "chemical smell in Kellyville", 6)
    terms = engine(db)
    assert {"chemical smell", "kellyville"} <= set(flagged(terms))
    kellyville = next(t for t in terms.terms if t.term == "kellyville")
    assert kellyville.place and kellyville.sources == 6
    assert terms.terms[0].place  # suburbs first


def test_all_four_conditions_must_hold():
    db = Database(":memory:")
    hits(db, "chemical smell", 5)  # five isn't more than five
    hits(db, "burst water main", 8, sources=1)  # one account only
    terms = engine(db)
    assert flagged(terms) == []


def test_busy_compared_with_usual():
    db = Database(":memory:")
    # Usually about 2 mentions per 2 hours over the past week (168 spread across 7 days).
    for i in range(168):
        record(db, "harbour bridge", at=NOW - 2 * HOUR - 1 - i * HOUR, kind="news", who=f"o{i % 4}", fresh=True,
               blocklist=BLOCK)
    hits(db, "harbour bridge", 6)
    assert "harbour bridge" not in flagged(engine(db))  # 6 isn't more than 3 times 2
    hits(db, "harbour bridge", 2, who_prefix="more")
    terms = engine(db)
    assert "harbour bridge" in flagged(terms)


def test_not_flagged_again_within_30_minutes_of_going_quiet():
    db = Database(":memory:")
    clock = [NOW]
    terms = Terms(db, S, lambda: clock[0])
    db.kv_set("terms:first_seen", str(NOW - 8 * 86400))
    hits(db, "chemical smell", 6)
    assert "chemical smell" in [t.term for t in terms.update(NOW) if t.flagged]
    db.execute("DELETE FROM term_hits WHERE term = 'chemical smell'")
    clock[0] = NOW + 120
    assert "chemical smell" not in [t.term for t in terms.update(clock[0]) if t.flagged]
    hits(db, "chemical smell", 6, who_prefix="again", ago_minutes=-5)
    clock[0] = NOW + 600  # 10 minutes after the first flag: too soon
    assert "chemical smell" not in [t.term for t in terms.update(clock[0]) if t.flagged]
    clock[0] = NOW + 31 * 60
    assert "chemical smell" in [t.term for t in terms.update(clock[0]) if t.flagged]


def test_nothing_is_flagged_while_learning():
    db = Database(":memory:")
    hits(db, "chemical smell in Kellyville", 6)
    terms = Terms(db, S, lambda: NOW)
    assert flagged(terms) == []
    tile = terms.assessment(NOW)
    assert tile.level is Level.LEARNING and tile.reason.startswith("Learning what's usual: 7 days to go")


# --- What a flagged term means for scoring ---


def test_a_flagged_term_raises_one_category_and_never_critical():
    db = Database(":memory:")
    hits(db, "chemical smell in Kellyville", 6)  # all posts
    terms = engine(db)
    terms.update(NOW)
    (view,) = terms.views(NOW)
    assert view.category is Category.SOCIAL and view.level is Level.ELEVATED
    scopes = {s.key: s.scopes for s in view.signals}
    assert scopes["term:kellyville"][0].startswith("area:")  # a suburb: its square of the map
    assert scopes["term:chemical smell"] == ("sydney",)  # anything else: Sydney as a whole
    result, _ = score_overall([view], ScoringSettings())
    assert result.level is Overall.NORMAL  # one category on its own isn't enough


def test_headlines_raise_news_and_backlog_cant_alert():
    db = Database(":memory:")
    hits(db, "burst water main floods Ryde streets", 6, kind="news", fresh=False)
    terms = engine(db)
    terms.update(NOW)
    assert {v.category for v in terms.views(NOW)} == {Category.NEWS}
    assert terms.views(NOW, alertable_only=True) == []  # seen too late to alert on
