"""Entry point: wire config, storage and clients, then run the notifier until stopped."""

import asyncio
import logging
import os
import signal
from pathlib import Path

from euroleague_notifier.api import EuroleagueClient
from euroleague_notifier.butler import ButlerClient
from euroleague_notifier.config import load_settings
from euroleague_notifier.store import EventStore
from euroleague_notifier.tracker import Notifier

log = logging.getLogger("euroleague_notifier")


async def run() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    events = await EventStore.open(settings.db_path)
    async with (
        EuroleagueClient() as euroleague,
        ButlerClient(settings.pibutler_url, settings.pibutler_api_key, settings.project_id) as butler,
    ):
        # Docker's healthcheck looks at this file's age (see Dockerfile).
        heartbeat = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/heartbeat"))
        notifier = Notifier(settings, euroleague, butler, events, on_tick=heartbeat.touch)
        main = asyncio.create_task(notifier.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, main.cancel)
        log.info("started: competitions %s, season %d", ",".join(settings.competitions), settings.season_year)
        try:
            await main
        except asyncio.CancelledError:
            log.info("stopping")
        finally:
            for task in asyncio.all_tasks() - {asyncio.current_task()}:
                task.cancel()
            await events.close()


def main() -> None:
    asyncio.run(run())
