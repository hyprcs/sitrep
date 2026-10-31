"""Alerts: when a message is sent, what it says, and how Telegram is talked to."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from sitrep.alerts import Alert, AlertPolicy, Alerts, Notifier, NotifyError
from sitrep.config import AlertsSettings, Config, TelegramSettings
from sitrep.db import Database
from sitrep.models import Overall
from sitrep.telegram import TelegramClient, TelegramNotifier, format_alert, off_status, setup_command
from sitrep.util import sydney_clock, sydney_datetime, sydney_timestamp

N, E, C = Overall.NORMAL, Overall.ELEVATED, Overall.CRITICAL
OFFLINE, UNKNOWN = Overall.OFFLINE, Overall.UNKNOWN
TOKEN = "1234567890:AAH" + "x" * 32
CHAT = "98765"
T0 = sydney_timestamp(datetime(2026, 10, 29, 14, 0))  # 2 pm, Thursday 29 October (AEDT)
MIN = 60


def policy(**overrides) -> AlertPolicy:
    return AlertPolicy(AlertsSettings(**overrides))


def feed(p: AlertPolicy, steps):
    """steps: (minutes after T0, level). Returns [(minute, alert)] for each alert sent."""
    sent = []
    for minute, level in steps:
        alert = p.update(level, f"reason at {minute}", T0 + minute * MIN)
        if alert:
            sent.append((minute, alert))
    return sent


# --- When to send ---


def test_starting_up_calm_sends_nothing():
    p = policy()
    assert feed(p, [(0, UNKNOWN), (1, N), (2, N)]) == []
    assert p.told == N


def test_worse_news_goes_out_at_once_and_only_critical_makes_a_sound():
    sent = feed(policy(), [(0, N), (1, E), (2, E), (5, C)])
    assert [(m, a.kind, a.level, a.loud) for m, a in sent] == [(1, "raised", E, False), (5, "raised", C, True)]
    assert sent[1][1].told == E


def test_elevated_can_make_a_sound_if_you_ask():
    sent = feed(policy(elevated_makes_sound=True), [(0, N), (1, E)])
    assert [(a.level, a.loud) for _, a in sent] == [(E, True)]


def test_all_clear_waits_and_is_silent():
    p = policy(all_clear_after_minutes=30)
    sent = feed(p, [(0, N), (1, E), (20, N), (49, N), (50, N), (51, N)])
    assert [(m, a.kind, a.level, a.loud) for m, a in sent] == [(1, "raised", E, False), (50, "lowered", N, False)]
    assert sent[1][1].lowered_at == T0 + 20 * MIN


def test_a_flare_up_during_the_wait_sends_nothing():
    """The cooldown: elevated, calm for a while, elevated again = one alert, not three."""
    sent = feed(policy(), [(0, N), (1, E), (20, N), (40, E), (60, N), (80, E), (100, E)])
    assert [m for m, _ in sent] == [1]


def test_easing_from_critical_to_elevated_then_back():
    sent = feed(policy(), [(0, N), (1, C), (20, E), (35, C), (40, E), (70, E), (71, N)])
    # Eased at 40 (the dip at 20 was cancelled by the flare-up at 35), told at 70.
    # Normal at 71 starts a new calm stretch, so nothing more is sent yet.
    assert [(m, a.kind, a.level) for m, a in sent] == [(1, "raised", C), (70, "lowered", E)]


def test_a_short_internet_blip_sends_nothing():
    sent = feed(policy(internet_outage_min_minutes=5), [(0, N), (1, OFFLINE), (4, OFFLINE), (5, N)])
    assert sent == []


def test_a_long_outage_is_summarised_once_the_sources_have_caught_up():
    sent = feed(policy(internet_outage_min_minutes=5), [(0, N), (1, OFFLINE), (34, OFFLINE), (35, N), (36, N), (37, N)])
    assert len(sent) == 1
    minute, alert = sent[0]
    assert (minute, alert.kind, alert.level, alert.loud) == (37, "summary", N, False)  # after the 2-minute grace
    assert alert.offline == (T0 + MIN, T0 + 35 * MIN)


def test_back_online_into_critical_trouble_makes_a_sound():
    sent = feed(policy(), [(0, N), (1, OFFLINE), (30, C), (32, C)])
    assert [(a.kind, a.level, a.loud) for _, a in sent] == [("summary", C, True)]


def test_back_online_into_backlog_is_one_silent_summary():
    """The dashboard shows critical from backlog, but nothing new: one quiet message, no burst."""
    p = policy()
    feed(p, [(0, N), (1, OFFLINE)])
    assert p.update(N, "calm", T0 + 30 * MIN, shown=E, shown_reason="ACSC advisory from 2 hours ago") is None
    alert = p.update(N, "calm", T0 + 32 * MIN, shown=E, shown_reason="ACSC advisory from 2 hours ago")
    assert (alert.kind, alert.level, alert.shown, alert.loud) == ("summary", N, E, False)
    assert p.update(N, "calm", T0 + 40 * MIN, shown=E) is None


def test_outage_reports_can_be_switched_off():
    sent = feed(policy(report_internet_outages=False), [(0, N), (1, OFFLINE), (30, N), (32, E)])
    assert [(a.kind, a.level) for _, a in sent] == [("raised", E)]


def test_starting_up_in_trouble_is_announced():
    sent = feed(policy(), [(0, UNKNOWN), (1, E)])
    assert [(a.kind, a.level) for _, a in sent] == [("raised", E)]


def test_what_you_were_told_survives_a_restart(tmp_path):
    db = Database(tmp_path / "a.db")
    notifier = RecordingNotifier(db)
    alerts = Alerts(AlertsSettings(), db, [notifier])
    alerts.update(N, "calm", T0)
    alerts.update(E, "trouble", T0 + MIN)
    assert notifier.pending.level == E

    again = RecordingNotifier(db)
    restarted = Alerts(AlertsSettings(), db, [again])
    assert restarted.policy.told == E
    assert restarted.update(E, "still trouble", T0 + 5 * MIN) is None
    assert again.pending is None
    db.close()


def test_nothing_is_tracked_when_no_notifier_is_set_up(tmp_path):
    db = Database(tmp_path / "a.db")
    alerts = Alerts(AlertsSettings(), db, [], off=[off_status(TelegramSettings())])
    assert alerts.update(E, "trouble", T0) is None
    assert db.kv_get("alerts:told") is None
    assert alerts.status()[0]["state"] == "off"
    assert "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in alerts.status()[0]["detail"]
    db.close()


# --- What the message says ---


def test_raised_message():
    alert = Alert("raised", C, "official: AEMO: NSW load shedding & LOR3 <now>, there isn't enough", T0,
                  told=E, told_at=T0 - 30 * MIN)
    text = format_alert(alert, AlertsSettings(dashboard_url="http://ds920:8080"))
    assert text.splitlines() == [
        "\U0001f6d1 <b>sitrep: Critical</b>",
        "Official: AEMO: NSW load shedding &amp; LOR3 &lt;now&gt;, there isn't enough.",
        "Up from Elevated (since 1:30 pm).",
        "Since 2:00 pm.",
        '<a href="http://ds920:8080">Open the dashboard</a>',
    ]


def test_all_clear_message():
    alert = Alert("lowered", N, "Nothing unusual from any source", T0 + 50 * MIN,
                  told=E, told_at=T0, lowered_at=T0 + 20 * MIN, loud=False)
    assert format_alert(alert, AlertsSettings()).splitlines() == [
        "✅ <b>sitrep: All clear</b>",
        "Nothing unusual from any source.",
        "It was Elevated from 2:00 pm until 2:20 pm.",
    ]


def test_back_online_message():
    alert = Alert("summary", N, "Nothing unusual from any source", T0 + 35 * MIN,
                  told=N, told_at=T0 - 3600, offline=(T0 + MIN, T0 + 35 * MIN), loud=False)
    assert format_alert(alert, AlertsSettings()).splitlines() == [
        "✅ <b>sitrep: All clear</b>",
        "Your internet was down for 34 minutes (2:01 pm to 2:35 pm), so sitrep couldn't send alerts then. "
        "It's back now.",
        "Nothing new came in meanwhile.",
        "Now: Nothing unusual from any source.",
    ]


def test_offline_summary_lists_what_came_in_and_explains_the_dashboard():
    items = {"total": 3, "counts": {"AEMO electricity grid": 2, "ACSC alerts and advisories": 1},
             "top": [{"source": "AEMO electricity grid", "title": "Actual LOR2 in NSW", "level": "elevated",
                      "ts": T0 + 10 * MIN, "url": None}]}
    alert = Alert("summary", N, "Nothing new", T0 + 35 * MIN, offline=(T0 + MIN, T0 + 33 * MIN), shown=E,
                  shown_reason="AEMO electricity grid: Actual LOR2 in NSW", items=items, loud=False, restarted=True)
    lines = format_alert(alert, AlertsSettings()).splitlines()
    assert lines[0] == "⚠️ <b>sitrep: Elevated</b>"  # what the dashboard shows
    assert lines[1].startswith("sitrep itself wasn't running for 32 minutes")
    assert lines[2] == "Meanwhile: 3 new items (AEMO electricity grid 2, ACSC alerts and advisories 1)."
    assert lines[3] == "⚠️ AEMO electricity grid: Actual LOR2 in NSW (2:10 pm)"
    assert lines[4] == "Now: AEMO electricity grid: Actual LOR2 in NSW."
    assert "didn't alert one by one" in lines[5]


def test_another_event_message():
    alert = Alert("another", C, "Load shedding in NSW", T0 + 20 * MIN, told=C, told_at=T0)
    assert format_alert(alert, AlertsSettings()).splitlines() == [
        "🛑 <b>sitrep: Critical, another event</b>",
        "Load shedding in NSW.",
        "Still critical since 2:00 pm.",
    ]


def test_very_long_reasons_are_cut():
    alert = Alert("raised", E, "x" * 5000, T0)
    assert len(format_alert(alert, AlertsSettings())) < 4096


def test_dashboard_url_must_be_a_web_address():
    with pytest.raises(ValidationError):
        AlertsSettings(dashboard_url='javascript:alert("hi")')
    with pytest.raises(ValidationError):
        AlertsSettings(dashboard_url='http://x" onclick="y')
    assert AlertsSettings(dashboard_url="https://sitrep.tail1234.ts.net").dashboard_url


def test_sydney_clock():
    assert sydney_clock(T0, T0) == "2:00 pm"
    assert sydney_clock(T0 - 86400, T0) == "Wed 2:00 pm"
    assert sydney_clock(T0 - 10 * 86400, T0) == "19 Oct 2:00 pm"
    winter = sydney_timestamp(datetime(2026, 6, 1, 9, 5))
    assert sydney_clock(winter, winter) == "9:05 am"
    assert sydney_datetime(winter).utcoffset().total_seconds() == 10 * 3600
    assert sydney_datetime(T0).utcoffset().total_seconds() == 11 * 3600
    # Clocks went forward at 2 am (AEST) on Sunday 4 October 2026, which is 16:00 UTC on the 3rd,
    # and back at 3 am (AEDT) on Sunday 5 April 2026, also 16:00 UTC the day before.
    forward = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc).timestamp()
    assert sydney_datetime(forward - 1).hour == 1 and sydney_datetime(forward).hour == 3
    back = datetime(2026, 4, 4, 16, 0, tzinfo=timezone.utc).timestamp()
    assert sydney_datetime(back - 1).hour == 2 and sydney_datetime(back).hour == 2
    assert sydney_datetime(back - 1).utcoffset() != sydney_datetime(back).utcoffset()


# --- Talking to Telegram ---


def fake_telegram(answers: dict[str, list], seen: list):
    """A fake Bot API: answers[method] is a list of (status, body) handed out in turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.path.startswith(f"/bot{TOKEN}/")
        seen.append((method, json.loads(request.content or b"{}")))
        status, body = answers[method].pop(0) if len(answers[method]) > 1 else answers[method][0]
        if isinstance(body, Exception):
            raise body
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def settings(chat_id: str = CHAT, **overrides) -> AlertsSettings:
    return AlertsSettings(telegram=TelegramSettings(bot_token=TOKEN, chat_id=chat_id), **overrides)


OK_ME = (200, {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "sitrep", "username": "my_sitrep_bot"}})
OK_CHAT = (200, {"ok": True, "result": {"id": int(CHAT), "type": "private"}})
OK_SENT = (200, {"ok": True, "result": {"message_id": 7}})


@pytest.mark.parametrize(
    ("status", "description", "final", "says"),
    [
        (401, "Unauthorized", True, "doesn't recognise the bot token"),
        (404, "Not Found", True, "doesn't recognise the bot token"),
        (400, "Bad Request: chat not found", True, "can't find that chat"),
        (403, "Forbidden: bot was blocked by the user", True, "blocked the bot"),
        (403, "Forbidden: bot can't initiate conversation with a user", True, "press Start"),
        (502, "Bad Gateway", False, "Telegram had a problem"),
    ],
)
async def test_telegram_errors_in_plain_english(status, description, final, says):
    seen = []
    http = fake_telegram({"getMe": [(status, {"ok": False, "error_code": status, "description": description})]}, seen)
    with pytest.raises(NotifyError) as err:
        await TelegramClient(http, settings().telegram).call("getMe")
    assert says in str(err.value)
    assert err.value.final is final
    assert TOKEN not in str(err.value)


async def test_flood_control_waits_as_long_as_telegram_says():
    seen = []
    body = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 17",
            "parameters": {"retry_after": 17}}
    http = fake_telegram({"sendMessage": [(429, body)]}, seen)
    with pytest.raises(NotifyError) as err:
        await TelegramClient(http, settings().telegram).call("sendMessage", chat_id=CHAT, text="hi")
    assert err.value.retry_after == 17 and not err.value.final


async def test_unreachable_telegram_never_shows_the_token():
    seen = []
    error = httpx.ConnectError(f"failed for https://api.telegram.org/bot{TOKEN}/getMe")
    http = fake_telegram({"getMe": [(0, error)]}, seen)
    with pytest.raises(NotifyError) as err:
        await TelegramClient(http, settings().telegram).call("getMe")
    assert str(err.value) == "Couldn't reach Telegram (couldn't connect)"
    assert not err.value.final


async def test_an_echoed_token_is_blanked_out():
    seen = []
    body = {"ok": False, "description": f"Bad Request: something about {TOKEN}"}
    http = fake_telegram({"getMe": [(400, body)]}, seen)
    with pytest.raises(NotifyError) as err:
        await TelegramClient(http, settings().telegram).call("getMe")
    assert TOKEN not in str(err.value) and "<bot token>" in str(err.value)


async def test_notifier_checks_then_sends_and_retries(tmp_path):
    db = Database(tmp_path / "a.db")
    seen = []
    down = (0, httpx.ConnectError("no route"))
    http = fake_telegram({"getMe": [OK_ME], "getChat": [OK_CHAT], "sendMessage": [down, OK_SENT]}, seen)
    clock = lambda: T0 + 3 * MIN  # noqa: E731
    notifier = TelegramNotifier(settings(), http, db, clock)
    notifier.retry_first_seconds = 0.01
    task = asyncio.create_task(notifier.run())
    notifier.queue(Alert("lowered", N, "Nothing unusual from any source", T0, told=E, told_at=T0 - 600, loud=False))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if notifier.last_sent_at:
            break
    task.cancel()

    assert [m for m, _ in seen] == ["getMe", "getChat", "sendMessage", "sendMessage"]
    params = seen[-1][1]
    assert params["chat_id"] == CHAT and params["parse_mode"] == "HTML"
    assert params["disable_notification"] is True  # good news arrives silently
    assert params["link_preview_options"] == {"is_disabled": True}
    assert notifier.status() == {"name": "Telegram", "state": "on", "detail": "sending as @my_sitrep_bot",
                                 "last_sent_at": T0 + 3 * MIN, "waiting": False}
    assert db.kv_get("alerts:telegram:last_sent") == str(T0 + 3 * MIN)
    db.close()


async def test_a_wrong_chat_shows_as_a_problem(tmp_path):
    db = Database(tmp_path / "a.db")
    seen = []
    missing = (400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
    http = fake_telegram({"getMe": [OK_ME], "getChat": [missing]}, seen)
    notifier = TelegramNotifier(settings(), http, db, lambda: T0)
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0.05)
    task.cancel()
    assert notifier.status()["state"] == "problem"
    assert "can't find that chat" in notifier.status()["detail"]
    assert [m for m, _ in seen] == ["getMe", "getChat"]  # a definite answer isn't retried
    db.close()


async def test_a_newer_alert_replaces_one_still_waiting(tmp_path):
    db = Database(tmp_path / "a.db")
    notifier = RecordingNotifier(db)
    notifier.queue(Alert("raised", E, "first", T0))
    notifier.queue(Alert("raised", C, "second", T0 + MIN))
    task = asyncio.create_task(notifier.run())
    await asyncio.sleep(0.05)
    task.cancel()
    assert [a.reason for a in notifier.sent] == ["second"]
    db.close()


# --- python -m sitrep telegram ---


def config(chat_id: str = "", token: str = TOKEN) -> Config:
    return Config(alerts=AlertsSettings(telegram=TelegramSettings(bot_token=token, chat_id=chat_id)))


async def test_setup_without_a_token_explains_what_to_do():
    lines = []
    assert await setup_command(config(token=""), fake_telegram({}, []), lines.append) == 1
    assert "@BotFather" in lines[0]


async def test_setup_finds_the_chat_id():
    seen, lines = [], []
    updates = (200, {"ok": True, "result": [
        {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 555, "type": "private", "first_name": "Sam"},
                                     "text": "/start"}},
    ]})
    http = fake_telegram({"getMe": [OK_ME], "getUpdates": [updates]}, seen)
    assert await setup_command(config(), http, lines.append) == 1
    assert "your bot is @my_sitrep_bot" in lines[0]
    assert lines[-1].strip() == "your private chat with the bot (Sam):  TELEGRAM_CHAT_ID=555"


async def test_setup_with_no_messages_yet_says_to_press_start():
    seen, lines = [], []
    http = fake_telegram({"getMe": [OK_ME], "getUpdates": [(200, {"ok": True, "result": []})]}, seen)
    assert await setup_command(config(), http, lines.append) == 1
    assert "press Start" in lines[-1]


async def test_setup_sends_a_test_message():
    seen, lines = [], []
    http = fake_telegram({"getMe": [OK_ME], "sendMessage": [OK_SENT]}, seen)
    assert await setup_command(config(chat_id=CHAT), http, lines.append) == 0
    assert seen[-1][0] == "sendMessage" and seen[-1][1]["chat_id"] == CHAT
    assert "sitrep test message" in seen[-1][1]["text"]
    assert all(TOKEN not in line for line in lines)


class RecordingNotifier(Notifier):
    name = "Test"

    def __init__(self, db: Database) -> None:
        super().__init__(db, lambda: T0)
        self.sent: list[Alert] = []

    async def check(self) -> None:
        self.who = "test"

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)
