"""Schedule sync, per-game tracking and notification decisions (ADR-008, ADR-014).

Who gets what:
- everyone: the daily schedule (one Follow button per game), a tip-off message (with Follow),
  and for games they don't follow, the final score with a 📊 Stats button;
- followers of a game (pressed Follow, or auto-follow by favourite team): a reminder before
  tip-off, a report after every quarter and the full final report.

Idle cost is near zero: the schedule is refreshed every couple of hours, and each game gets a
task that sleeps until its reminder / tip-off, polls every ~45 s while live, then exits.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from html import escape
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
    {"value": "start", "label": "Game start"},
    {"value": "final", "label": "Final scores"},
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
    unique = sorted({c.code: c for c in clubs}.values(), key=lambda c: c.short_name)
    return {
        "name": "EuroLeague",
        "settings": [
            {
                "key": "teams",
                "label": "Auto-follow teams",
                "default": [],  # not "All": auto-following everything would follow every game
                "options": [{"value": c.code, "label": c.short_name} for c in unique],
            },
            {
                "key": "kind",
                "label": "Notifications",
                "options": KINDS,
                "default": [k["value"] for k in KINDS],
            },
        ],
    }


def topic(game: Game) -> dict:
    name = reports.title(game)
    return {
        "id": game.identifier,
        "title": name,
        "auto_follow": {"teams": [game.home.code, game.away.code]},
        "follow_text": (
            f"⭐ You're following <b>{escape(name)}</b> ({{{{hm:{reports.iso_z(game)}}}}}).\n"
            "You'll get a reminder before tip-off, stats after every quarter and the full box score."
        ),
        "unfollow_text": f"Unfollowed {name}",
    }


def follow_button(game: Game) -> dict:
    return {
        "label": "☆ Follow this game",
        "label_active": "⭐ Following · tap to unfollow",
        "follow": game.identifier,
    }


def followers(game: Game, following: bool = True) -> dict:
    return {"topic": game.identifier, "following": following}


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
        await self.maybe_send_schedule()
        for gid, (comp, game) in self._games.items():
            running = gid in self._tasks and not self._tasks[gid].done()
            if not running and self._should_track(game, now) and not await self._finished(gid):
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

    async def _finished(self, gid: str) -> bool:
        return await self.events.has(f"{gid}:final") and await self.events.has(f"{gid}:result")

    # --- daily schedule ----------------------------------------------------------------------

    async def maybe_send_schedule(self) -> None:
        tz = ZoneInfo(self.settings.digest_timezone)
        local_now = self.now().astimezone(tz)
        if local_now.time() < self.settings.digest_time:
            return
        today = sorted(
            (g for _, g in self._games.values() if g.tipoff.astimezone(tz).date() == local_now.date()),
            key=lambda g: (g.tipoff, g.code),
        )
        upcoming = [g for g in today if g.tipoff > self.now()]
        if not upcoming:
            return  # no games today, or all already started
        key = f"schedule:{local_now.date().isoformat()}"
        if await self.events.has(key):
            return
        buttons = [
            [
                {
                    "label": reports.schedule_label(g, False),
                    "label_active": reports.schedule_label(g, True),
                    "follow": g.identifier,
                }
            ]
            for g in upcoming
        ]
        await self._send(
            key,
            reports.daily_schedule(upcoming),
            tags={"kind": ["schedule"]},
            topics=[topic(g) for g in upcoming],
            buttons=buttons,
        )

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
                    key,
                    reports.reminder(game, minutes),
                    expires_at=_iso(game.tipoff),
                    topics=[topic(game)],
                    audience=followers(game),
                    buttons=[[follow_button(game)]],
                )
            except ButlerError as exc:
                log.warning("%s: reminder failed: %s", game.identifier, exc)

    async def poll(self, competition: str, game: Game) -> bool:
        """One live poll. Returns True once the game is over and both final messages are out."""
        season, gid = game.season, game.identifier
        state = await self.el.period_state(season, game.code)
        if state.game_over:
            return await self._finals(competition, game)
        if (
            state.current_period >= 1
            and state.ended_periods == 0
            and not await self.events.has(f"{gid}:start")
        ):
            await self._send(
                f"{gid}:start",
                reports.tipoff(game),
                tags={"kind": ["start"]},
                expires_at=_iso(self.now() + QUARTER_TTL),
                topics=[topic(game)],
                buttons=[[follow_button(game)]],
            )
        if state.ended_periods > await self.events.last_period(gid):
            # Only the latest ended period: after a restart we don't replay old quarters.
            period = state.ended_periods
            box = await self.el.boxscore(season, game.code)
            if box is None:
                return False
            await self._send(
                f"{gid}:p{period}",
                reports.quarter_report(game, box, period),
                expires_at=_iso(self.now() + QUARTER_TTL),
                topics=[topic(game)],
                audience=followers(game),
                buttons=[[follow_button(game)]],
            )
        return False

    async def _finals(self, competition: str, game: Game) -> bool:
        gid = game.identifier
        if await self._finished(gid):
            return True
        box = await self.el.boxscore(game.season, game.code)
        if box is None:
            return False
        full = reports.final_report(game, box)
        url = reports.game_url(game, competition)
        link = [{"label": "🔗 Game center", "url": url}] if url else []
        if not await self.events.has(f"{gid}:final"):
            await self._send(
                f"{gid}:final",
                full,
                topics=[topic(game)],
                audience=followers(game),
                buttons=[link] if link else None,
            )
        if not await self.events.has(f"{gid}:result"):
            await self._send(
                f"{gid}:result",
                reports.final_score(game, box),
                tags={"kind": ["final"]},
                topics=[topic(game)],
                audience=followers(game, following=False),
                buttons=[[{"label": "📊 Stats", "reveal": full}, *link]],
            )
        return True

    async def _send(
        self, key: str, text: str, tags: dict | None = None, expires_at: str | None = None, **extra
    ) -> None:
        await self.butler.notify(key, text, tags, expires_at, **extra)
        await self.events.add(key)

    async def _sleep_until(self, when: datetime) -> None:
        while (delay := (when - self.now()).total_seconds()) > 0:
            await self.sleep(min(delay, 3600))
