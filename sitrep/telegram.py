"""Telegram: send alerts to your phone through a bot you make yourself.

The bot token and chat ID come from .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).
Telegram alerts are off until both are set. The token goes in the address of every
request, so nothing here ever logs an address, and every message from Telegram is
checked for the token before it's shown anywhere.

Bot API docs: https://core.telegram.org/bots/api (checked against version 10.3).
"""

from __future__ import annotations

import html
import re
from typing import Any, Callable

import httpx

from .alerts import Alert, Notifier, NotifyError
from .config import AlertsSettings, Config, TelegramSettings
from .db import Database
from .models import Overall
from .util import format_duration, sydney_clock

MAX_REASON = 1500  # Telegram allows 4096 characters; a reason this long is plenty

# The same shapes as the dashboard's icons, so the level never relies on colour alone.
HEADLINES = {
    Overall.NORMAL: ("✅", "All clear"),
    Overall.ELEVATED: ("⚠️", "Elevated"),
    Overall.CRITICAL: ("\U0001f6d1", "Critical"),
}

# Every message sent by `python -m sitrep simulate` starts with this.
SIMULATION_LINE = "\U0001f9ea <b>SIMULATION: not a real alert</b>"

_TOKEN_LIKE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")

FIX_TOKEN = "Check TELEGRAM_BOT_TOKEN in .env (copy it again from @BotFather), then restart sitrep"


def redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "<bot token>")
    return _TOKEN_LIKE.sub("<bot token>", text)


def explain(status: int, description: str) -> tuple[str, bool]:
    """Telegram's error, in plain English, and whether retrying could help."""
    lower = description.lower()
    if status in (401, 404):
        return f"Telegram doesn't recognise the bot token. {FIX_TOKEN}", True
    if "chat not found" in lower:
        return ("Telegram can't find that chat. Check TELEGRAM_CHAT_ID in .env, and make sure you've "
                "sent your bot a message first"), True
    if "blocked by the user" in lower:
        return "You've blocked the bot in Telegram. Unblock it to get alerts again", True
    if "can't initiate conversation" in lower:
        return "Open your bot in Telegram and press Start, so it's allowed to message you", True
    if "deactivated" in lower:
        return "That Telegram account has been deleted", True
    if status == 429:
        return "Telegram asked sitrep to slow down", False
    if status >= 500:
        return f"Telegram had a problem (HTTP {status})", False
    return f"Telegram said: {description[:200]}", status == 400 or status == 403


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, settings: TelegramSettings) -> None:
        self.http = http
        self.token = settings.bot_token.get_secret_value().strip()
        self.api_url = settings.api_url.rstrip("/")

    async def call(self, method: str, **params: Any) -> Any:
        """Call one Bot API method. Raises NotifyError (never showing the token) if it fails."""
        try:
            res = await self.http.post(f"{self.api_url}/bot{self.token}/{method}", json=params)
        except httpx.TimeoutException:
            raise NotifyError("Couldn't reach Telegram (timed out)") from None
        except httpx.HTTPError as exc:
            what = "couldn't connect" if isinstance(exc, httpx.ConnectError) else type(exc).__name__
            raise NotifyError(f"Couldn't reach Telegram ({what})") from None
        try:
            body = res.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            message, final = explain(res.status_code, f"HTTP {res.status_code}")
            raise NotifyError(redact(message, self.token), final=final)
        if body.get("ok"):
            return body.get("result")
        message, final = explain(res.status_code, str(body.get("description") or f"HTTP {res.status_code}"))
        wait = (body.get("parameters") or {}).get("retry_after")
        if isinstance(body.get("parameters"), dict) and body["parameters"].get("migrate_to_chat_id"):
            message = (f"That group became a supergroup; set TELEGRAM_CHAT_ID="
                       f"{body['parameters']['migrate_to_chat_id']} in .env")
            final = True
        raise NotifyError(redact(message, self.token), final=final,
                          retry_after=float(wait) if isinstance(wait, (int, float)) else None)


def sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return text
    text = text[:1].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def format_alert(alert: Alert, cfg: AlertsSettings, simulation: bool = False) -> str:
    """The message text, in Telegram's HTML style (only <b> and <a> are used)."""
    mark, label = HEADLINES[alert.level]
    reason = alert.reason if len(alert.reason) <= MAX_REASON else alert.reason[: MAX_REASON - 1] + "…"
    when = lambda ts: sydney_clock(ts, alert.at)  # noqa: E731
    lines = [SIMULATION_LINE] if simulation else []
    lines.append(f"{mark} <b>sitrep: {label}</b>")

    if alert.kind == "back_online" and alert.offline:
        start, end = alert.offline
        lines.append(
            f"Your internet was down for {format_duration(end - start)} ({when(start)} to {when(end)}), "
            "so sitrep couldn't send alerts then. It's back now."
        )
        lines.append(f"Now: {html.escape(sentence(reason), quote=False)}")
    else:
        lines.append(html.escape(sentence(reason), quote=False))
        if alert.kind == "lowered" and alert.told in HEADLINES and alert.told_at:
            was = HEADLINES[alert.told][1]
            until = f" until {when(alert.lowered_at)}" if alert.lowered_at else ""
            lines.append(f"It was {was} from {when(alert.told_at)}{until}.")
        elif alert.kind == "raised" and alert.told in (Overall.ELEVATED, Overall.CRITICAL) and alert.told_at:
            lines.append(f"Up from {HEADLINES[alert.told][1]} (since {when(alert.told_at)}).")
        if alert.kind == "raised":
            lines.append(f"Since {when(alert.at)}.")

    url = cfg.dashboard_url.strip()
    if url and not simulation:  # the link would open the real dashboard, not the simulation
        lines.append(f'<a href="{html.escape(url, quote=True)}">Open the dashboard</a>')
    return "\n".join(lines)


class TelegramNotifier(Notifier):
    name = "Telegram"

    def __init__(self, cfg: AlertsSettings, http: httpx.AsyncClient, db: Database, clock: Callable[[], float],
                 simulation: bool = False) -> None:
        super().__init__(db, clock)
        self.cfg = cfg
        self.simulation = simulation
        self.client = TelegramClient(http, cfg.telegram)
        self.chat_id = cfg.telegram.chat_id.strip()

    async def check(self) -> None:
        me = await self.client.call("getMe")
        await self.client.call("getChat", chat_id=self.chat_id)
        self.who = f"sending as @{me.get('username', 'your bot')}"

    async def send(self, alert: Alert) -> None:
        await self.client.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=format_alert(alert, self.cfg, self.simulation),
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            disable_notification=not alert.loud,
        )


def off_status(settings: TelegramSettings) -> dict[str, Any]:
    token = bool(settings.bot_token.get_secret_value().strip())
    missing = "TELEGRAM_CHAT_ID" if token else "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
    return {"name": "Telegram", "state": "off", "detail": f"Add {missing} to .env to get alerts on your phone",
            "last_sent_at": None, "waiting": False}


# --- python -m sitrep telegram: a guided check of the Telegram set-up ---


def _describe_chat(chat: dict[str, Any]) -> str:
    if chat.get("type") == "private":
        name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or "you"
        return f"your private chat with the bot ({name})"
    return f"the {chat.get('type', 'group')} \"{chat.get('title', '?')}\""


async def setup_command(cfg: Config, http: httpx.AsyncClient, say: Callable[[str], None] = print) -> int:
    """Check the token, help find the chat ID, then send a test message. Returns an exit code."""
    t = cfg.alerts.telegram
    if not t.bot_token.get_secret_value().strip():
        say("There's no bot token yet. Make a bot with @BotFather in Telegram, then put its token in .env as\n"
            "TELEGRAM_BOT_TOKEN=... and run this again. The README section \"Telegram alerts\" walks you through it.")
        return 1
    client = TelegramClient(http, t)
    try:
        me = await client.call("getMe")
    except NotifyError as exc:
        say(f"Problem: {exc}")
        return 1
    bot = f"@{me.get('username', '?')}"
    say(f"Bot token works: your bot is {bot} (\"{me.get('first_name', '')}\").")

    if not t.chat_id.strip():
        try:
            updates = await client.call("getUpdates", limit=100)
        except NotifyError as exc:
            say(f"Problem: couldn't look for messages to the bot. {exc}")
            return 1
        chats: dict[int, dict[str, Any]] = {}
        for update in updates or []:
            for key in ("message", "edited_message", "channel_post", "my_chat_member"):
                chat = (update.get(key) or {}).get("chat")
                if isinstance(chat, dict) and "id" in chat:
                    chats[chat["id"]] = chat
        if not chats:
            say(f"No chat ID yet. Open Telegram, find {bot}, press Start (or send it any message), "
                "then run this again within a day.")
            return 1
        say("Found these chats with the bot. Add the line for the one you want to .env, then run this again:")
        for chat_id, chat in chats.items():
            say(f"  {_describe_chat(chat)}:  TELEGRAM_CHAT_ID={chat_id}")
        return 1

    text = ("✅ <b>sitrep test message</b>\nTelegram alerts are set up. sitrep will message you here when "
            "its overall level changes.")
    if cfg.alerts.dashboard_url.strip():
        text += f'\n<a href="{html.escape(cfg.alerts.dashboard_url.strip(), quote=True)}">Open the dashboard</a>'
    try:
        await client.call("sendMessage", chat_id=t.chat_id.strip(), text=text, parse_mode="HTML",
                          link_preview_options={"is_disabled": True})
    except NotifyError as exc:
        say(f"Problem sending the test message: {exc}")
        return 1
    say("Sent a test message. Check Telegram: if it's there, alerts are working. Restart sitrep if it's running,")
    say("so it picks up the new settings.")
    return 0
