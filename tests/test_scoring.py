"""Scoring: categories, the overall level, and its hysteresis."""

from __future__ import annotations

from sitrep.config import ScoringSettings
from sitrep.models import INTERNET_DOWN, Category, Level, Overall
from sitrep.scoring import OverallResult, OverallTracker, score_categories, score_overall
from sitrep.state import SourceView

CFG = ScoringSettings()


def view(source_id: str, category: Category, level: Level, reason: str = "something", flags=()) -> SourceView:
    return SourceView(
        id=source_id, name=source_id.title(), category=category, link=None, level=level,
        reason=reason, flags=frozenset(flags), details={}, last_known_level=level,
        last_known_reason=reason, observed_at=0, last_success_at=0, last_attempt_at=0,
        last_error=None, failures=0,
    )


def overall(*views: SourceView) -> OverallResult:
    return score_overall(list(views), CFG)[0]


# --- Categories ---


def test_category_takes_its_worst_live_source():
    cats = score_categories(
        [
            view("ioda", Category.NETWORK, Level.ELEVATED, "Telstra dropping"),
            view("radar", Category.NETWORK, Level.NORMAL),
        ],
        CFG,
    )
    assert cats[Category.NETWORK].level == Level.ELEVATED
    assert cats[Category.NETWORK].reason == "Ioda: Telstra dropping"


def test_stale_and_learning_sources_do_not_count():
    cats = score_categories(
        [
            view("ioda", Category.NETWORK, Level.STALE),
            view("jetstream", Category.SOCIAL, Level.LEARNING),
        ],
        CFG,
    )
    assert cats[Category.NETWORK].level is None
    assert cats[Category.SOCIAL].level is None
    assert cats[Category.OFFICIAL].reason == "No sources set up yet"


def test_two_social_sources_elevated_make_social_critical():
    cats = score_categories(
        [
            view("jetstream", Category.SOCIAL, Level.ELEVATED, "'sydney explosion' 9x normal"),
            view("wikipedia", Category.SOCIAL, Level.ELEVATED, "new article: 2026 Sydney explosion"),
        ],
        CFG,
    )
    assert cats[Category.SOCIAL].level == Level.CRITICAL
    assert "2 sources elevated together" in cats[Category.SOCIAL].reason


def test_two_network_sources_elevated_stay_elevated():
    cats = score_categories(
        [view("ioda", Category.NETWORK, Level.ELEVATED), view("radar", Category.NETWORK, Level.ELEVATED)],
        CFG,
    )
    assert cats[Category.NETWORK].level == Level.ELEVATED


# --- Overall ---


def test_nothing_live_is_unknown():
    assert overall(view("probes", Category.CONNECTIVITY, Level.PENDING)).level == Overall.UNKNOWN


def test_all_normal_is_all_clear():
    result = overall(view("probes", Category.CONNECTIVITY, Level.NORMAL))
    assert result.level == Overall.NORMAL
    assert result.reason == "Nothing unusual from any source"


def test_one_elevated_category_is_not_enough():
    result = overall(
        view("probes", Category.CONNECTIVITY, Level.NORMAL),
        view("ioda", Category.NETWORK, Level.ELEVATED, "Optus dropping"),
    )
    assert result.level == Overall.NORMAL
    assert "isn't enough on its own" in result.reason


def test_two_elevated_categories_raise_overall_to_elevated():
    result = overall(
        view("ioda", Category.NETWORK, Level.ELEVATED, "Telstra and Optus dropping"),
        view("jetstream", Category.SOCIAL, Level.ELEVATED, "'telstra down' posts 8x normal"),
    )
    assert result.level == Overall.ELEVATED
    assert "Telstra and Optus dropping" in result.reason
    assert "'telstra down' posts 8x normal" in result.reason


def test_one_critical_category_is_critical():
    result = overall(
        view("ioda", Category.NETWORK, Level.CRITICAL, "NSW critical on 2 methods"),
        view("acsc", Category.OFFICIAL, Level.ELEVATED, "new alert"),
    )
    assert result.level == Overall.CRITICAL
    assert result.reason.startswith("Ioda: NSW critical on 2 methods")
    assert "Also elevated" in result.reason


def test_internet_down_flag_means_offline():
    reason = "Can't reach the internet: your router answers, but nothing outside has for 90 seconds"
    result = overall(
        view("probes", Category.CONNECTIVITY, Level.CRITICAL, reason, [INTERNET_DOWN]),
        view("ioda", Category.NETWORK, Level.NORMAL),
    )
    assert result.level == Overall.OFFLINE
    # The headline already says "Can't reach the internet", so the reason doesn't repeat it.
    assert result.reason == (
        "Your router answers, but nothing outside has for 90 seconds. Other tiles show their last-known state"
    )


# --- Hysteresis ---


def result(level: Overall, reason: str = "r") -> OverallResult:
    return OverallResult(level, reason)


def test_first_result_shows_immediately():
    tracker = OverallTracker(CFG, now=0)
    assert tracker.update(result(Overall.NORMAL), now=6)
    assert tracker.current.level == Overall.NORMAL


def test_raising_needs_two_checks_in_a_row():
    tracker = OverallTracker(CFG, now=0)
    tracker.update(result(Overall.NORMAL), now=0)
    assert not tracker.update(result(Overall.ELEVATED), now=30)
    assert tracker.current.level == Overall.NORMAL
    assert tracker.update(result(Overall.ELEVATED), now=60)
    assert tracker.current.level == Overall.ELEVATED


def test_a_single_spike_does_not_raise():
    tracker = OverallTracker(CFG, now=0)
    tracker.update(result(Overall.NORMAL), now=0)
    tracker.update(result(Overall.CRITICAL), now=30)
    tracker.update(result(Overall.NORMAL), now=60)
    tracker.update(result(Overall.CRITICAL), now=90)
    assert tracker.current.level == Overall.NORMAL


def test_lowering_waits_for_fifteen_calm_minutes():
    tracker = OverallTracker(CFG, now=0)
    tracker.update(result(Overall.ELEVATED), now=0)
    assert not tracker.update(result(Overall.NORMAL), now=60)
    assert tracker.easing_to.level == Overall.NORMAL
    assert tracker.lowers_at == 60 + 15 * 60
    assert not tracker.update(result(Overall.NORMAL), now=60 + 14 * 60)
    assert tracker.update(result(Overall.NORMAL), now=60 + 15 * 60)
    assert tracker.current.level == Overall.NORMAL


def test_easing_resets_if_it_flares_up_again():
    tracker = OverallTracker(CFG, now=0)
    tracker.update(result(Overall.ELEVATED), now=0)
    tracker.update(result(Overall.NORMAL), now=60)
    tracker.update(result(Overall.ELEVATED), now=600)
    assert tracker.easing_since is None
    assert not tracker.update(result(Overall.NORMAL), now=1000)
    assert not tracker.update(result(Overall.NORMAL), now=1000 + 14 * 60)


def test_offline_switches_immediately_both_ways():
    tracker = OverallTracker(CFG, now=0)
    tracker.update(result(Overall.NORMAL), now=0)
    assert tracker.update(result(Overall.OFFLINE), now=30)
    assert tracker.update(result(Overall.NORMAL), now=60)
    assert tracker.current.level == Overall.NORMAL


def test_same_level_refreshes_the_reason():
    tracker = OverallTracker(CFG, now=0)
    tracker.update(result(Overall.NORMAL, "old"), now=0)
    assert not tracker.update(result(Overall.NORMAL, "new"), now=30)
    assert tracker.current.reason == "new"
    assert tracker.since == 0
