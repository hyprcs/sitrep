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


# --- Topics: busy terms that belong together ---


def term(name, mentions=10, sources=8, place=False):
    from sitrep.terms import Term
    return Term(name, mentions, sources, 0.1, 0, mentions, mentions, sources, place=place, flagged=True)


def test_a_term_is_found_however_its_words_are_joined():
    from sitrep.terms import term_pattern
    assert term_pattern("lamarre condon").search("Beau Lamarre-Condon found guilty")
    assert term_pattern("macquarie park").search("Crash at Macquarie  Park")
    assert not term_pattern("ant").search("Fire ants spread")  # whole words only


def test_terms_from_the_same_texts_are_one_topic_and_an_everyday_word_doesnt_glue_two_stories():
    from sitrep.terms import group
    court = [term("lamarre"), term("lamarre condon"), term("guilty")]
    fire = [term("house fire"), term("evacuated")]
    shared = term("fire")  # in both stories' texts, but mostly the house fire's
    texts = {"lamarre": {1, 2, 3, 4}, "lamarre condon": {1, 2, 3, 4}, "guilty": {1, 2, 3},
             "house fire": {10, 11, 12}, "evacuated": {10, 11, 12}, "fire": {4, 10, 11, 12}}
    found = sorted(sorted(t.term for t in g) for g in group(court + fire + [shared], texts))
    assert found == [["evacuated", "fire", "house fire"], ["guilty", "lamarre", "lamarre condon"]]
    # One shared text isn't enough on its own ("harbour" and "finds" from one headline)...
    assert len(group([term("harbour"), term("finds")], {"harbour": {7}, "finds": {7}})) == 2
    # ...unless one is part of the other ("fire" and "fire ant").
    assert len(group([term("fire"), term("fire ant")], {"fire": {7, 8}, "fire ant": {7}})) == 1
    # With nothing to compare (posts gone after a restart), words inside phrases still join.
    assert len(group([term("beach"), term("bondi beach")], {})) == 1


def test_a_topic_is_named_after_its_phrases_and_shown_with_a_headline_about_it():
    from sitrep.terms import best_headline, name_of
    terms = [term("beau lamarre", 18), term("lamarre condon", 16), term("condon found", 7), term("lamarre", 20)]
    assert name_of(terms) == "beau lamarre condon"  # "condon found" isn't busy enough to join the name
    headlines = [{"title": "Men flee as police raid cannabis crop", "url": "a", "outlet": "ABC News"},
                 {"title": "Beau Lamarre-Condon found guilty of murdering Jesse Baird", "url": "b", "outlet": "ABC News"}]
    assert best_headline(terms, "beau lamarre condon", headlines)["text"].startswith("Beau Lamarre-Condon")
    police = [term("police"), term("police officer"), term("former police"), term("officer")]
    assert best_headline(police, "former police officer", headlines) is None  # "police" alone isn't about it


def test_busy_words_are_described_by_topic_and_lone_words_only_counted():
    from types import SimpleNamespace
    db = Database(":memory:")
    hits(db, "chemical smell in Kellyville", 6)
    hits(db, "making", 8, who_prefix="bot", sources=2)
    posts = [SimpleNamespace(title="chemical smell in Kellyville", at=NOW - 60) for _ in range(6)]
    terms = Terms(db, S, lambda: NOW, posts=lambda since: posts)
    db.kv_set("terms:first_seen", str(NOW - 8 * 86400))
    terms.update(NOW)
    named = [tp.name for tp in terms.topics if tp.named]
    assert named == ["chemical smell"]  # with "kellyville", "chemical", "smell": all from the same posts
    lone = next(tp for tp in terms.topics if tp.name == "making")
    assert not lone.named
    reason = terms.assessment(NOW).reason
    assert reason.startswith('"chemical smell" and ') and reason.endswith('also busy: "making"')
    (view,) = terms.views(NOW)
    headlines = {s.key: s.headline for s in view.signals}
    assert headlines["term:kellyville"] == 'busy talk about "chemical smell"' and headlines["term:making"] == ""
