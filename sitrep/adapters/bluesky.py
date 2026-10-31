"""Bluesky: spikes in posts about trouble in Sydney and Australia, read from Jetstream v2.

sitrep keeps one connection open to Bluesky's Jetstream service, which sends every new
public post on Bluesky as it's made (about 25 a second, about 2 GB a day). Each post
is checked against a few keyword groups, such as "Telstra, Optus, TPG or NBN" together
with "down" or "outage". Nothing else about the post is kept. sitrep stores how many
different people posted matching words each minute, plus a small sample of matching
posts (text, link and time) so you can see what's being said. A sample is deleted as
soon as its author deletes the post or their account.

How it's judged:

- For each group, sitrep learns how many people usually post at each hour of the
  week, from the last four weeks. It needs a week of history before it can judge,
  and shows "learning" until then.
- A group spikes when the number of people posting in the last 5 minutes would be
  very unlikely at the usual rate (odds below one in a million, by a Poisson test)
  and is at least a handful of people. A spike makes Bluesky elevated.
- Bluesky on its own never makes sitrep critical: the social category only goes
  critical when two social sources spike at the same time.
- Each group says what its spikes are about (its scope): "telco down" and "internet
  down" are national internet trouble, "cyber attack" is national cyber trouble, and
  "Sydney incident" is about Sydney as a whole. Another category only backs up a
  spike when it's about the same kind of thing.
- A spike first noticed while sitrep is catching up on old posts (more than 15
  minutes behind) shows on the dashboard, but can't raise an alert.
- If the newest post sitrep has read is more than 5 minutes old, the stream has
  frozen (or sitrep is still catching up), and the source shows as frozen.

For story clusters, sitrep also keeps the last 6 hours of posts that name a Sydney suburb
or Sydney as a whole, in memory only (never saved), and drops a post as soon as its author
deletes it. A saved story records "a Bluesky post", never its text, link or author.

If the connection drops, sitrep picks up where it left off (Jetstream keeps about a
day), so no posts are missed. After a break of more than two hours it starts from now,
rather than downloading everything it missed.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from ..config import Settings
from ..geo import cell_of
from ..models import SYDNEY, Assessment, Category, Level, Signal, valid_scope
from ..places import gazetteer
from ..stories import WINDOW_HOURS, Member, words
from ..terms import blocklist_of, record, who_code
from ..util import format_duration
from .base import AdapterContext, SourceError, StreamingAdapter, StreamingSettings

POST = "app.bsky.feed.post"
# Account states that mean the person's posts should no longer be kept.
GONE = {"deleted", "deactivated", "takendown", "suspended"}


class KeywordGroup(Settings):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    # A post matches when it contains at least one phrase from every list. Phrases match
    # whole words, ignoring case; end one with * to match the start of a word ("evacuat*").
    all_of: list[list[str]] = Field(min_length=1)
    # What a spike is about: national:internet, national:cyber, ..., or sydney.
    scope: str = SYDNEY

    @field_validator("scope")
    @classmethod
    def _scope_is_known(cls, value: str) -> str:
        if not valid_scope(value) or value.startswith(("area:", "cluster:")):
            raise ValueError("scope must be sydney or national:<theme> (internet, cyber, power, fuel or security)")
        return value


AUSTRALIA = ["Australia", "Australian", "Australians", "Aussie", "Sydney", "NSW", "New South Wales"]

DEFAULT_GROUPS = [
    {
        "id": "sydney_incident",
        "name": "Sydney or NSW incident",
        "all_of": [
            ["Sydney", "NSW", "New South Wales"],
            ["shooting", "shooter", "shots fired", "explosion", "explosions", "exploded", "lockdown",
             "locked down", "evacuate", "evacuated", "evacuating", "evacuation"],
        ],
        "scope": "sydney",
    },
    {
        "id": "telco_down",
        "name": "Telco down",
        "all_of": [
            ["Telstra", "Optus", "TPG", "NBN", "Vodafone", "iiNet", "Aussie Broadband", "Belong"],
            ["down", "outage", "outages", "not working", "no signal", "no service", "offline"],
        ],
        "scope": "national:internet",
    },
    {
        "id": "internet_down",
        "name": "Internet down in Australia",
        "all_of": [
            ["internet down", "internet is down", "internet's down", "internet outage", "internet outages",
             "no internet", "internet not working"],
            AUSTRALIA,
        ],
        "scope": "national:internet",
    },
    {
        "id": "cyber_attack",
        "name": "Cyber attack in Australia",
        "all_of": [
            ["cyber attack", "cyberattack", "cyber-attack", "cyber attacks", "cyberattacks", "cyber incident",
             "ransomware", "data breach"],
            AUSTRALIA,
        ],
        "scope": "national:cyber",
    },
]


class BlueskySettings(StreamingSettings):
    url: str = "wss://jetstream.us-east.bsky.network/xrpc/network.bsky.jetstream.subscribeEvents"
    groups: list[KeywordGroup] = Field(default_factory=lambda: [KeywordGroup(**g) for g in DEFAULT_GROUPS], min_length=1)
    window_minutes: int = Field(5, ge=1)
    # A group spikes when the count in the window is less likely than this at the usual rate...
    spike_probability: float = Field(1e-6, gt=0, lt=1)
    # ...and at least this many people posted.
    spike_min_people: int = Field(5, ge=1)
    # The usual rate for an hour of the week comes from this many previous weeks.
    baseline_weeks: int = Field(4, ge=1)
    # The usual count is never taken as lower than this (people per window), so a group
    # that's usually silent can't spike on a couple of posts.
    min_expected: float = Field(0.5, gt=0)
    samples_per_group: int = Field(20, ge=0)
    # After a break longer than this, start from now instead of catching up.
    max_catch_up_hours: float = 2


def _phrase(text: str) -> str:
    text = text.strip()
    prefix = text.endswith("*")
    words = text.rstrip("*").split()
    return r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + ("" if prefix else r"(?!\w)")


def compile_groups(groups: list[KeywordGroup]) -> dict[str, list[re.Pattern[str]]]:
    return {
        g.id: [re.compile("|".join(_phrase(p) for p in phrases if p.strip()), re.IGNORECASE) for phrases in g.all_of]
        for g in groups
    }


def matching_groups(text: str, compiled: dict[str, list[re.Pattern[str]]]) -> list[str]:
    return [gid for gid, patterns in compiled.items() if all(p.search(text) for p in patterns)]


# --- Reading Jetstream's messages ---


@dataclass(frozen=True)
class Post:
    uri: str
    author: str  # the poster's DID
    time: float
    text: str
    match_text: str  # the text plus the title of any linked article, for matching
    url: str


@dataclass(frozen=True)
class Message:
    kind: str  # "post", "delete", "gone", "info" or "other"
    seq: int | None = None
    time: float | None = None
    post: Post | None = None
    uri: str | None = None  # for "delete"
    author: str | None = None  # for "gone"
    info: str | None = None  # for "info": Jetstream's name and message


def _time(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_message(raw: str | bytes) -> Message:
    try:
        payload = json.loads(raw).get("payload") or {}
    except (ValueError, AttributeError):
        return Message("other")
    kind = str(payload.get("$type", "")).rsplit("#", 1)[-1]
    seq = payload.get("seq") if isinstance(payload.get("seq"), int) else None
    when = _time(payload.get("time"))
    did = str(payload.get("did") or "")
    if kind == "commit" and payload.get("collection") == POST:
        uri = f"at://{did}/{POST}/{payload.get('rkey')}"
        if payload.get("operation") == "delete":
            return Message("delete", seq, when, uri=uri)
        record = payload.get("record") or {}
        if payload.get("operation") == "create" and isinstance(record, dict):
            text = str(record.get("text") or "")
            external = ((record.get("embed") or {}).get("external") or {}) if isinstance(record.get("embed"), dict) else {}
            title = str(external.get("title") or "") if isinstance(external, dict) else ""
            post = Post(
                uri=uri, author=did, time=when or time.time(), text=text,
                match_text=f"{text}\n{title}" if title else text,
                url=f"https://bsky.app/profile/{did}/post/{payload.get('rkey')}",
            )
            return Message("post", seq, when, post=post)
    if kind == "account":
        account = payload.get("account") or {}
        if isinstance(account, dict) and account.get("active") is False and account.get("status") in GONE:
            return Message("gone", seq, when, author=did or account.get("did"))
    if kind == "info":
        return Message("info", seq, when, info=f"{payload.get('name')}: {payload.get('message')}")
    return Message("other", seq, when)


# --- Judging ---


def poisson_tail(k: int, lam: float) -> float:
    """The chance of seeing k or more, when lam is expected (Poisson)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    log_pmf = lambda i: -lam + i * math.log(lam) - math.lgamma(i + 1)  # noqa: E731
    if k <= lam:
        return max(0.0, 1.0 - sum(math.exp(log_pmf(i)) for i in range(k)))
    term = math.exp(log_pmf(k))
    total, i = term, k
    while term > total * 1e-12 and i < k + 100_000:
        i += 1
        term *= lam / i
        total += term
    return min(1.0, total)


def usual_rate(counts: dict[int, float], minutes: dict[int, float], at: float, weeks: int) -> float | None:
    """People per minute usually seen at this hour of the week (and the hour either side),
    from the previous `weeks` weeks. Falls back to the whole last week if that's too thin."""
    hour = int(at // 3600 * 3600)
    week = 7 * 86400
    hours = [hour - w * week + offset for w in range(1, weeks + 1) for offset in (-3600, 0, 3600)]
    seen = sum(minutes.get(h, 0) for h in hours)
    if seen < 60:
        hours = [h for h in minutes if hour - week <= h < hour]
        seen = sum(minutes.get(h, 0) for h in hours)
    if seen < 60:
        return None
    return sum(counts.get(h, 0) for h in hours) / seen


@dataclass
class GroupView:
    id: str
    name: str
    people: int  # different people posting matching words in the window
    usual: float | None  # how many is usual for the window
    probability: float | None
    spike: bool = False
    learning_left: float = 0.0  # a group added later learns on its own before it can spike
    chart: dict[str, Any] | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)


def judge_group(
    group: KeywordGroup, people: int, rate: float | None, s: BlueskySettings, learning_left: float = 0.0,
) -> GroupView:
    learning_left = max(0.0, learning_left)
    if rate is None:
        return GroupView(group.id, group.name, people, None, None, learning_left=learning_left)
    usual = max(rate * s.window_minutes, s.min_expected)
    probability = poisson_tail(people, usual)
    spike = probability < s.spike_probability and people >= s.spike_min_people and not learning_left
    return GroupView(group.id, group.name, people, usual, probability, spike, learning_left)


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def assess(
    views: list[GroupView], s: BlueskySettings, *, learning_left: float, behind: float, posts_per_second: float | None,
    alertable: dict[str, bool] | None = None,
) -> Assessment:
    """alertable says, per group, whether its spike was first seen soon enough to alert on."""
    window = f"{s.window_minutes} minutes"
    spikes = [v for v in views if v.spike]
    if learning_left > 0:
        level = Level.LEARNING
        total = sum(v.people for v in views)
        reason = (f"Learning what's usual: {format_duration(learning_left)} to go. "
                  f"{_people(total)} posted about these topics in the last {window}")
    elif spikes:
        level = Level.ELEVATED
        reason = "; ".join(
            f"Spike in posts about {v.name.lower()}: {_people(v.people)} in {window}, usually about {v.usual:.1f}"
            for v in spikes
        )
    else:
        level = Level.NORMAL
        busiest = max(views, key=lambda v: v.people - (v.usual or 0))
        reason = f"No spikes. Busiest: {busiest.name.lower()}, {_people(busiest.people)} in the last {window}"
        if busiest.usual is not None:
            reason += f" (usually about {busiest.usual:.1f})"
    new = [v for v in views if v.learning_left > 0]
    if learning_left <= 0 and new:
        names = ", ".join(v.name.lower() for v in new)
        reason += f". Still learning a new group ({names}): {format_duration(max(v.learning_left for v in new))} to go"
    if behind > 300:
        reason = f"Catching up after a break ({format_duration(behind)} behind). {reason}"
    details = {
        "groups": [vars(v) for v in views],
        "window_minutes": s.window_minutes,
        "learning_left": max(0.0, learning_left),
        "behind": behind,
        "posts_per_second": posts_per_second,
    }
    scopes = {g.id: g.scope for g in s.groups}
    signals = tuple(
        Signal(f"bluesky:{v.id}", Level.ELEVATED, (scopes.get(v.id, SYDNEY),),
               title=f"Spike in Bluesky posts about {v.name.lower()}: {_people(v.people)} in {window}",
               alertable=(alertable or {}).get(v.id, True))
        for v in spikes
    ) if level == Level.ELEVATED else ()
    return Assessment(level, reason, details, signals=signals)


# --- The adapter ---


@dataclass
class _Minute:
    first_seq: int | None = None
    posts: int = 0
    people: dict[str, set[str]] = field(default_factory=dict)


class BlueskyAdapter(StreamingAdapter):
    id = "bluesky"
    name = "Bluesky posts"
    category = Category.SOCIAL
    link = "https://bsky.app/"
    Settings = BlueskySettings
    source_type = "social"
    # Jetstream sends about 25 posts a second, so the newest one is never more than seconds old.
    content_budget_minutes = 5
    settings: BlueskySettings

    # Minutes are counted this long after they end, in case events arrive a little late.
    settle_seconds = 90
    # Judge at most this often while catching up quickly.
    assess_every_seconds = 10

    def __init__(self, ctx: AdapterContext, settings: BlueskySettings) -> None:
        super().__init__(ctx, settings)
        self._compiled = compile_groups(settings.groups)
        self._minutes: dict[int, _Minute] = {}
        self._last_seq: int | None = None
        self._position: float | None = None  # time of the latest event read
        self._assessed_at = float("-inf")
        self._spiking: set[str] = set()
        self._spike_alertable: dict[str, bool] = {}  # group -> could its current spike alert?
        # Recent posts about Sydney places, for story clusters: (author, member). Memory only.
        self._recent: deque[tuple[str, Member]] = deque(maxlen=5000)
        self._blocklist = blocklist_of(ctx.config.terms)
        self._uris, self._authors = ctx.db.sample_keys(self.id)
        saved = ctx.db.kv_get(self._cursor_key)
        self._cursor: dict[str, float] | None = json.loads(saved) if saved else None

    @property
    def _cursor_key(self) -> str:
        return f"{self.id}:cursor"

    # --- Connecting ---

    def url(self) -> str:
        url = f"{self.settings.url}?collections={POST}&kinds=commit&kinds=account"
        cursor = self._cursor
        if cursor and self.ctx.clock() - cursor["time"] <= self.settings.max_catch_up_hours * 3600:
            url += f"&cursor={int(cursor['seq'])}"
        return url

    async def stream(self) -> None:
        url = self.url()
        resuming = "cursor=" in url
        try:
            async with connect(
                url, user_agent_header=self.ctx.config.http.user_agent,
                subprotocols=[Subprotocol("xrpc.v1.json")], max_size=2**22, open_timeout=30,
            ) as ws:
                self.log.info("Connected to Bluesky's Jetstream%s", " (catching up)" if resuming else "")
                async for raw in ws:
                    self.handle(raw)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 400 and resuming:
                self._forget_cursor()
                raise SourceError("Bluesky wouldn't resume from where sitrep left off, so it's starting from now") from exc
            raise SourceError(f"Bluesky's stream refused the connection (HTTP {status})") from exc

    def _forget_cursor(self) -> None:
        self._cursor = None
        self.ctx.db.execute("DELETE FROM kv WHERE key = ?", (self._cursor_key,))

    # --- Reading ---

    def handle(self, raw: str | bytes) -> None:
        message = read_message(raw)
        if message.seq is not None:
            if self._last_seq is not None and message.seq <= self._last_seq:
                return  # Jetstream resends the event at the cursor, and may repeat others
            self._last_seq = message.seq
        if message.time is not None:
            self._position = max(self._position or 0.0, message.time)
            minute = self._minutes.setdefault(int(message.time // 60 * 60), _Minute())
            if minute.first_seq is None and message.seq is not None:
                minute.first_seq = message.seq
        else:
            minute = None

        if message.kind == "post" and message.post and minute is not None:
            minute.posts += 1
            for gid in matching_groups(message.post.match_text, self._compiled):
                minute.people.setdefault(gid, set()).add(message.post.author)
                self._keep_sample(gid, message.post)
            self._remember_for_stories(message.post)
        elif message.kind == "delete":
            if message.uri in self._uris:
                self.ctx.db.delete_samples(self.id, uri=message.uri)
                self._uris.discard(message.uri)
            self._forget_for_stories(lambda author, m: m.key == f"{self.id}:{message.uri}")
        elif message.kind == "gone":
            if message.author in self._authors:
                self.ctx.db.delete_samples(self.id, author=message.author)
                self._authors.discard(message.author)
            self._forget_for_stories(lambda author, m: author == message.author)
        elif message.kind == "info":
            self.log.info("Jetstream says %s", message.info)

        self._settle()

    def _remember_for_stories(self, post: Post) -> None:
        """Keep a post that names a Sydney suburb, or Sydney itself, for story clusters."""
        places = gazetteer()
        found = places.find(post.text, strict=True)
        local = next((p for p in found if p.sydney), None)
        if local is None and not places.sydney_wide(post.text):
            return
        fresh = self.ctx.clock() - post.time <= self.recency_limit
        self._recent.append((post.author, Member(
            key=f"{self.id}:{post.uri}", source=self.id, source_name=self.name, source_type="social",
            category=self.category, title=post.text[:300], at=post.time, url=post.url,
            words=words(post.text, [p.name for p in found]), place=local.name if local else None,
            cell=cell_of(local.lat, local.lon) if local else None, sydney_wide=local is None, notable=True,
            emergency=False, alertable=fresh,
            lat=local.lat if local else None, lon=local.lon if local else None,
        )))
        if self.ctx.config.terms.enabled:
            # Only the terms are counted, with a one-way code for the account: no text, no handle.
            record(self.ctx.db, post.text, at=post.time, kind="social", who=who_code(post.author), fresh=fresh,
                   blocklist=self._blocklist)

    def _forget_for_stories(self, gone) -> None:
        if any(gone(author, m) for author, m in self._recent):
            self._recent = deque(((a, m) for a, m in self._recent if not gone(a, m)), maxlen=self._recent.maxlen)

    def recent_posts(self, since: float) -> list[Member]:
        """Posts about Sydney places since a time, for story clusters (oldest dropped after 6 hours)."""
        horizon = self.ctx.clock() - WINDOW_HOURS * 3600
        while self._recent and self._recent[0][1].at < horizon:
            self._recent.popleft()
        return [m for _, m in self._recent if m.at >= since]

    def recent_mentions(self, since: float) -> list[tuple[str, Member]]:
        """Like recent_posts, with a one-way code for each post's author, to count different people."""
        self.recent_posts(since)  # drops posts older than 6 hours
        return [(who_code(author), m) for author, m in self._recent if m.at >= since]

    def _keep_sample(self, gid: str, post: Post) -> None:
        if not self.settings.samples_per_group:
            return
        self.ctx.db.add_sample(
            self.id, gid, uri=post.uri, author=post.author, ts=post.time, text=post.text[:600],
            url=post.url, keep=self.settings.samples_per_group,
        )
        self._uris.add(post.uri)
        self._authors.add(post.author)

    def _settle(self) -> None:
        """Write out minutes that have finished, save where sitrep is up to, and judge."""
        if self._position is None:
            return
        done = sorted(m for m in self._minutes if m + 60 + self.settle_seconds <= self._position)
        if not done:
            return
        db = self.ctx.db
        for m in done:
            minute = self._minutes.pop(m)
            if minute.posts:
                db.add_count(self.id, "_minutes", m, 1)
                db.add_count(self.id, "_posts", m, minute.posts)
            for gid, people in minute.people.items():
                db.add_count(self.id, gid, m, len(people))
        if db.kv_get(f"{self.id}:first_seen") is None:
            db.kv_set(f"{self.id}:first_seen", str(done[0]))
        # Resume from the first event of the oldest minute not yet written, so a restart
        # recounts it in full rather than missing its start.
        pending = [m.first_seq for m in self._minutes.values() if m.first_seq is not None]
        seq = min(pending) if pending else self._last_seq
        if seq is not None:
            self._cursor = {"seq": seq, "time": done[-1] + 60}
            db.kv_set(self._cursor_key, json.dumps(self._cursor))
        now = self.ctx.clock()
        caught_up = now - self._position < 120
        if caught_up or now - self._assessed_at >= self.assess_every_seconds:
            self._assessed_at = now
            self.report(self.judge(done[-1] + 60, now))

    def judge(self, upto: float, now: float) -> Assessment:
        """Judge the window of finished minutes ending at `upto`."""
        s = self.settings
        db = self.ctx.db
        since_hourly = upto - (s.baseline_weeks * 7 + 1) * 86400
        minutes = dict(db.counts_since(self.id, "_minutes", since_hourly, hourly=True))
        first_seen = float(db.kv_get(f"{self.id}:first_seen") or upto)
        learning = self.ctx.config.scoring.learning_days * 86400
        learning_left = learning - (upto - first_seen)
        buckets = db.bucketed_counts(self.id, upto - 86400, 900)
        views = []
        for group in s.groups:
            recent = db.counts_since(self.id, group.id, upto - s.window_minutes * 60)
            people = int(sum(v for t, v in recent if t < upto))
            hourly = dict(db.counts_since(self.id, group.id, since_hourly, hourly=True))
            # A group added after the first week has no history yet, so it learns on its own.
            key = f"{self.id}:first_seen:{group.id}"
            group_seen = db.kv_get(key)
            if group_seen is None:
                group_seen = str(first_seen if learning_left > 0 else upto)
                db.kv_set(key, group_seen)
            group_left = learning - (upto - float(group_seen))
            view = judge_group(group, people, usual_rate(hourly, minutes, upto, s.baseline_weeks), s, group_left)
            view.chart = _chart(buckets, group.id, upto)
            view.samples = db.samples(self.id, group.id, limit=3)
            views.append(view)
        posts = [(t, v) for t, v in db.counts_since(self.id, "_posts", upto - 300) if t < upto]
        rate = sum(v for _, v in posts) / (60 * len(posts)) if posts else None
        self._note_spikes(views, upto, now)
        result = assess(views, s, learning_left=learning_left, behind=max(0.0, now - upto), posts_per_second=rate,
                        alertable=self._spike_alertable)
        if result.level != Level.LEARNING:
            self._add_to_timeline(views, upto)
        return replace(result, content_at=self._position)

    def _note_spikes(self, views: list[GroupView], upto: float, now: float) -> None:
        """Remember each new spike, and whether it was new enough when noticed to alert on."""
        spiking = {v.id: v for v in views if v.spike}
        for gid, view in spiking.items():
            if gid not in self._spike_alertable:
                seen = self.see_item(f"spike:{gid}:{int(upto)}", upto, level=Level.ELEVATED.value, now=now,
                                     title=f"Spike in posts about {view.name.lower()}: {_people(view.people)}")
                self._spike_alertable[gid] = seen.alertable
        self._spike_alertable = {g: a for g, a in self._spike_alertable.items() if g in spiking}

    def _add_to_timeline(self, views: list[GroupView], upto: float) -> None:
        for view in views:
            if view.spike and view.id not in self._spiking:
                self.ctx.db.add_event(
                    upto, self.id, self.category.value, Level.ELEVATED.value,
                    f"Bluesky spike: {view.name}",
                    details={"reason": f"{_people(view.people)} in {self.settings.window_minutes} minutes, "
                                       f"usually about {view.usual:.1f}"},
                    dedupe_key=f"{self.id}:{view.id}:{int(upto)}",
                )
        self._spiking = {v.id for v in views if v.spike}


def _chart(buckets: dict[tuple[int, str], float], gid: str, upto: float) -> dict[str, Any]:
    """People per 15 minutes over the last day; None where sitrep wasn't connected."""
    step = 900
    start = int((upto - 86400) // step * step + step)
    values = []
    for b in range(start, int(upto), step):
        live = buckets.get((b, "_minutes"), 0)
        values.append(int(buckets.get((b, gid), 0)) if live else None)
    return {"from": start, "step": step, "values": values}
