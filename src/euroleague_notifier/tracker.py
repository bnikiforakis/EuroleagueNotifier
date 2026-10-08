"""Schedule sync, per-game tracking and notification decisions (ADR-008).

Idle cost is near zero: the schedule is refreshed every couple of hours, and each game gets a
task that sleeps until its reminder / tip-off, polls every ~45 s while live, then exits.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from euroleague_notifier import reports
from euroleague_notifier.api import ApiError, EuroleagueClient
from euroleague_notifier.butler import ButlerClient, ButlerError
from euroleague_notifier.config import Settings
from euroleague_notifier.models import Club, Game
from euroleague_notifier.store import EventStore

log = logging.getLogger(__name__)

KINDS = [
    {"value": "schedule", "label": "Daily schedule"},
    {"value": "reminder", "label": "Tip-off reminder"},
    {"value": "quarter", "label": "Quarter reports"},
    {"value": "final", "label": "Final report"},
]
SCHEDULE_REFRESH = timedelta(hours=2)
LOOP_INTERVAL = timedelta(minutes=10)
LOOKAHEAD = timedelta(hours=26)  # start tracking (sleeping) tasks this far ahead
MAX_GAME_LENGTH = timedelta(hours=4)  # stop polling a game this long after tip-off
LIVE_LEAD = timedelta(minutes=2)  # start polling shortly before tip-off
QUARTER_TTL = timedelta(minutes=30)  # undelivered quarter reports go stale (AC11)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_manifest(clubs: list[Club]) -> dict:
    unique = {c.code: c for c in clubs}
    return {
        "name": "EuroLeague",
        "settings": [
            {
                "key": "teams",
                "label": "Teams",
                "all_label": "All teams",
                "options": [
                    {"value": c.code, "label": c.short_name}
                    for c in sorted(unique.values(), key=lambda c: c.short_name)
                ],
            },
            {
                "key": "kind",
                "label": "Notifications",
                "options": KINDS,
                "default": [k["value"] for k in KINDS],
            },
        ],
    }


def tags(game: Game, kind: str) -> dict[str, list[str]]:
    return {"teams": [game.home.code, game.away.code], "kind": [kind]}


class Notifier:
    def __init__(
        self,
        settings: Settings,
        euroleague: EuroleagueClient,
        butler: ButlerClient,
        events: EventStore,
        clock: Clock = _utcnow,
        sleep: Sleep = asyncio.sleep,
        on_tick: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.el = euroleague
        self.butler = butler
        self.events = events
        self.now = clock
        self.sleep = sleep
        self.on_tick = on_tick
        self._tasks: dict[str, asyncio.Task] = {}
        self._games: dict[str, tuple[str, Game]] = {}  # identifier -> (competition, game)
        self._schedule_at: datetime | None = None

    # --- main loop ---------------------------------------------------------------------------

    async def run(self) -> None:
        await self.register()
        while True:
            try:
                await self.tick()
                if self.on_tick:
                    self.on_tick()
            except Exception:
                log.exception("main loop error")
            await self.sleep(LOOP_INTERVAL.total_seconds())

    async def register(self) -> None:
        """Register the manifest, retrying until both the API and PiButler are reachable."""
        delay = 5.0
        while True:
            try:
                clubs = [
                    c
                    for comp in self.settings.competitions
                    for c in await self.el.clubs(comp, self.settings.season(comp))
                ]
                await self.butler.register(build_manifest(clubs))
                log.info("registered manifest with %d teams", len({c.code for c in clubs}))
                return
            except (ApiError, ButlerError) as exc:
                log.warning("registration failed (%s), retrying in %.0fs", exc, delay)
                await self.sleep(delay)
                delay = min(delay * 2, 300)

    async def tick(self) -> None:
        now = self.now()
        if self._schedule_at is None or now - self._schedule_at >= SCHEDULE_REFRESH:
            await self.refresh_schedule()
            self._schedule_at = now
        await self.maybe_send_digest()
        for gid, (comp, game) in self._games.items():
            running = gid in self._tasks and not self._tasks[gid].done()
            if not running and self._should_track(game, now) and not await self.events.has(f"{gid}:final"):
                log.info(
                    "%s: tracking %s vs %s (tip-off %s)",
                    gid,
                    game.home.code,
                    game.away.code,
                    _iso(game.tipoff),
                )
                self._tasks[gid] = asyncio.create_task(self.track(comp, game), name=gid)

    async def refresh_schedule(self) -> None:
        games: dict[str, tuple[str, Game]] = {}
        for comp in self.settings.competitions:
            for game in await self.el.games(comp, self.settings.season(comp)):
                games[game.identifier] = (comp, game)
        self._games = games
        log.info("schedule refreshed: %d games", len(games))

    def _should_track(self, game: Game, now: datetime) -> bool:
        return now - MAX_GAME_LENGTH < game.tipoff < now + LOOKAHEAD

    # --- daily digest ------------------------------------------------------------------------

    async def maybe_send_digest(self) -> None:
        tz = ZoneInfo(self.settings.digest_timezone)
        local_now = self.now().astimezone(tz)
        if local_now.time() < self.settings.digest_time:
            return
        today = [g for _, g in self._games.values() if g.tipoff.astimezone(tz).date() == local_now.date()]
        if not today or all(g.tipoff <= self.now() for g in today):
            return  # no games, or too late for a "today" digest to be useful
        key = f"digest:{local_now.date().isoformat()}"
        if await self.events.has(key):
            return
        teams = sorted({c for g in today for c in (g.home.code, g.away.code)})
        text = reports.schedule_digest(today, "Today's EuroLeague games", time_format="hm")
        await self._send(key, text, {"teams": teams, "kind": ["schedule"]})

    # --- per-game tracking -------------------------------------------------------------------

    async def track(self, competition: str, game: Game) -> None:
        gid = game.identifier
        try:
            await self._reminder(game)
            await self._sleep_until(game.tipoff - LIVE_LEAD)
            log.info("%s: polling live", gid)
            while self.now() < game.tipoff + MAX_GAME_LENGTH:
                try:
                    if await self.poll(competition, game):
                        log.info("%s: done", gid)
                        return
                except (ApiError, ButlerError) as exc:
                    log.warning("%s: poll failed: %s", gid, exc)
                await self.sleep(self.settings.poll_seconds)
            log.warning("%s: gave up, no final after %s", gid, MAX_GAME_LENGTH)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: tracker crashed", gid)

    async def _reminder(self, game: Game) -> None:
        minutes = self.settings.reminder_minutes
        await self._sleep_until(game.tipoff - timedelta(minutes=minutes))
        key = f"{game.identifier}:reminder"
        if self.now() < game.tipoff and not await self.events.has(key):
            try:
                await self._send(
                    key, reports.reminder(game, minutes), tags(game, "reminder"), _iso(game.tipoff)
                )
            except ButlerError as exc:
                log.warning("%s: reminder failed: %s", game.identifier, exc)

    async def poll(self, competition: str, game: Game) -> bool:
        """One live poll. Returns True once the game is over and the final report is out."""
        season, gid = game.season, game.identifier
        state = await self.el.period_state(season, game.code)
        if state.game_over:
            if not await self.events.has(f"{gid}:final"):
                box = await self.el.boxscore(season, game.code)
                if box is None:
                    return False
                url = reports.game_url(game, competition)
                await self._send(f"{gid}:final", reports.final_report(game, box, url), tags(game, "final"))
            return True
        last = await self.events.last_period(gid)
        if state.ended_periods > last:
            # Only the latest ended period: after a restart we don't replay old quarters.
            period = state.ended_periods
            box = await self.el.boxscore(season, game.code)
            if box is None:
                return False
            expires = _iso(self.now() + QUARTER_TTL)
            await self._send(
                f"{gid}:p{period}", reports.quarter_report(game, box, period), tags(game, "quarter"), expires
            )
        return False

    async def _send(self, key: str, text: str, tag: dict, expires_at: str | None = None) -> None:
        await self.butler.notify(key, text, tag, expires_at)
        await self.events.add(key)

    async def _sleep_until(self, when: datetime) -> None:
        while (delay := (when - self.now()).total_seconds()) > 0:
            await self.sleep(min(delay, 3600))
