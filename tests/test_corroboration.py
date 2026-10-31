"""The revised scoring: which elevated categories back each other up, and what alerts see."""

from __future__ import annotations

from sitrep.config import ScoringSettings
from sitrep.models import Category, Level, Overall, Signal, area, cluster
from sitrep.scoring import alerting_views, scopes_related, score_overall
from sitrep.state import SourceView

CFG = ScoringSettings()


def view(source_id, category, level, *signals: Signal, reason="something") -> SourceView:
    return SourceView(
        id=source_id, name=source_id, category=category, link=None, level=level, reason=reason,
        flags=frozenset(), details={}, last_known_level=level, last_known_reason=reason, observed_at=0,
        last_success_at=0, last_attempt_at=0, last_error=None, failures=0, signals=tuple(signals),
    )


def sig(key, scope, level=Level.ELEVATED, alertable=True) -> Signal:
    return Signal(key, level, (scope,), title=key, alertable=alertable)


def overall(*views, cfg=CFG):
    return score_overall(list(views), cfg)[0]


def test_scopes():
    assert scopes_related("national:internet", "national:internet", CFG)
    assert scopes_related("national:internet", "national:cyber", CFG)
    assert scopes_related("national:power", "national:internet", CFG)
    assert not scopes_related("national:power", "national:cyber", CFG)
    assert not scopes_related("national:fuel", "national:internet", CFG)
    assert scopes_related("sydney", "sydney", CFG)
    assert not scopes_related("sydney", "national:internet", CFG)
    assert scopes_related(area(10, 10), area(11, 9), CFG)  # neighbouring squares
    assert not scopes_related(area(10, 10), area(12, 10), CFG)
    assert scopes_related(cluster("abc"), cluster("abc"), CFG)
    assert not scopes_related(cluster("abc"), cluster("xyz"), CFG)


def test_related_national_categories_raise_overall():
    result = overall(
        view("ioda", Category.NETWORK, Level.ELEVATED, sig("ioda", "national:internet"), reason="Optus dropping"),
        view("bluesky", Category.SOCIAL, Level.ELEVATED, sig("bluesky:telco_down", "national:internet"),
             reason="telco down spike"),
    )
    assert result.level == Overall.ELEVATED
    assert {s.key for s in result.drivers} == {"ioda", "bluesky:telco_down"}


def test_unrelated_categories_stay_normal_and_say_why():
    result = overall(
        view("aemo", Category.STRESS, Level.ELEVATED, sig("aemo:lor2", "national:power"), reason="LOR2"),
        view("acsc", Category.OFFICIAL, Level.ELEVATED, sig("acsc:1", "national:cyber"), reason="advisory"),
    )
    assert result.level == Overall.NORMAL
    assert "don't seem to be about the same thing" in result.reason


def test_the_plain_both_national_rule_can_be_chosen():
    cfg = ScoringSettings(national_needs_related_themes=False)
    result = overall(
        view("aemo", Category.STRESS, Level.ELEVATED, sig("aemo:lor2", "national:power")),
        view("acsc", Category.OFFICIAL, Level.ELEVATED, sig("acsc:1", "national:cyber")),
        cfg=cfg,
    )
    assert result.level == Overall.ELEVATED


def test_sydney_wide_signals_back_each_other_up_but_not_national_ones():
    trains = view("transit", Category.OFFICIAL, Level.ELEVATED, sig("transit:all", "sydney"))
    posts = view("bluesky", Category.SOCIAL, Level.ELEVATED, sig("bluesky:sydney_incident", "sydney"))
    ioda = view("ioda", Category.NETWORK, Level.ELEVATED, sig("ioda", "national:internet"))
    assert overall(trains, posts).level == Overall.ELEVATED
    assert overall(trains, ioda).level == Overall.NORMAL


def test_a_chain_of_backing_joins_three_categories():
    result = overall(
        view("acsc", Category.OFFICIAL, Level.ELEVATED, sig("acsc:1", "national:cyber")),
        view("ioda", Category.NETWORK, Level.ELEVATED, sig("ioda", "national:internet")),
        view("aemo", Category.STRESS, Level.ELEVATED, sig("aemo:lor2", "national:power")),
    )
    assert result.level == Overall.ELEVATED
    assert len(result.drivers) == 3  # cyber-internet and internet-power join all three


def test_alerts_only_count_signals_new_enough_to_alert_on():
    views = [
        view("acsc", Category.OFFICIAL, Level.ELEVATED, sig("acsc:old", "national:cyber", alertable=False)),
        view("ioda", Category.NETWORK, Level.ELEVATED, sig("ioda", "national:internet")),
    ]
    assert overall(*views).level == Overall.ELEVATED  # the dashboard counts everything
    seen = alerting_views(views)
    assert seen[0].level == Level.NORMAL and seen[0].signals == ()
    assert seen[1] is views[1]
    assert score_overall(seen, CFG)[0].level == Overall.NORMAL


def test_a_source_with_some_backlog_keeps_its_new_signals():
    mixed = view("aemo", Category.STRESS, Level.CRITICAL,
                 sig("aemo:load_shed", "national:power", Level.CRITICAL, alertable=False),
                 sig("aemo:lor2", "national:power"))
    (seen,) = alerting_views([mixed])
    assert seen.level == Level.ELEVATED and [s.key for s in seen.signals] == ["aemo:lor2"]


def test_derived_signals_arent_independent_sources():
    """A story and a convergence built from the same posts aren't two social sources agreeing."""
    from dataclasses import replace

    story = replace(view("stories:social", Category.SOCIAL, Level.ELEVATED, sig("cluster:1", "area:1,1")), derived=True)
    spot = replace(view("convergence:social", Category.SOCIAL, Level.ELEVATED, sig("convergence:1,1", "area:1,1")),
                   derived=True)
    cats = score_overall([story, spot], CFG)[1]
    assert cats[Category.SOCIAL].level == Level.ELEVATED  # not pushed to critical
