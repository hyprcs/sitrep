"""Start sitrep with:  python -m sitrep

Other commands:
  python -m sitrep telegram   check the Telegram set-up and send a test message
  python -m sitrep simulate   a 7-minute pretend emergency, to watch the dashboard and alerts work
  python -m sitrep simulate --story cyber   the same, but the emergency is a cyber attack on a telco
  python -m sitrep simulate --replay   replay realistic scenarios and say which would alert
  python -m sitrep map        download the Greater Sydney basemap (about 40 MB) for the map page
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx
import uvicorn

from . import basemap
from .app import create_app
from .config import Config, ConfigError, load_config
from .runtime import Runtime
from .scenarios import SCENARIOS
from .scenarios import main as replay_scenarios
from .simulate import STORIES, SimulatedRuntime, simulation_config
from .telegram import setup_command


def telegram(cfg: Config) -> int:
    async def run() -> int:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": cfg.http.user_agent}) as http:
            return await setup_command(cfg, http)

    return asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m sitrep", description="Run the sitrep dashboard.")
    parser.add_argument("command", nargs="?", choices=["telegram", "simulate", "map"],
                        help="telegram: check the Telegram set-up and send a test message; "
                             "simulate: a pretend emergency, to watch the dashboard and alerts work; "
                             "map: download the Greater Sydney basemap for the map page")
    parser.add_argument("--port", type=int, help="simulate: the port for its dashboard (default: one above sitrep's)")
    parser.add_argument("--story", choices=list(STORIES), default="outage",
                        help="simulate: which pretend emergency to play: outage (a telco outage, the default) or "
                             "cyber (a cyber attack on a telco)")
    parser.add_argument("--replay", nargs="*", metavar="SCENARIO",
                        help="simulate: replay scenarios on a pretend clock and say which would alert, instead of "
                             f"starting a dashboard. Scenarios: {', '.join(s.name for s in SCENARIOS)} (default: all)")
    args = parser.parse_args()
    try:
        cfg = load_config()
        if args.command == "telegram":
            return telegram(cfg)
        if args.command == "map":
            return asyncio.run(basemap.run(cfg))
        if args.command == "simulate" and args.replay is not None:
            logging.basicConfig(level=logging.WARNING)
            return replay_scenarios(cfg, args.replay)
        if args.command == "simulate":
            cfg = simulation_config(cfg, args.port)
            runtime = SimulatedRuntime(cfg, script=STORIES[args.story])
        else:
            runtime = Runtime(cfg)
    except ConfigError as exc:
        print(f"sitrep can't start.\n{exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=cfg.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Both log request addresses otherwise, and the Telegram bot token is part of those.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sitrep").info(
        "Dashboard: http://localhost:%d (or this computer's address on your network)", cfg.server.port
    )
    if args.command == "simulate":
        minutes = round(runtime.simulation.script[-1].at / 60)
        telegram_on = (
            "Telegram messages are real, and marked SIMULATION." if cfg.alerts.telegram.configured
            else "Telegram isn't set up, so no messages will be sent."
        )
        logging.getLogger("sitrep").info(
            "SIMULATION: nothing here is real. It takes about %d minutes. %s Press Ctrl+C to stop.",
            minutes, telegram_on,
        )
    uvicorn.run(
        create_app(runtime),
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
