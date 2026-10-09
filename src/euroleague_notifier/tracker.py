"""Schedule sync, per-game tracking and notification decisions (ADR-008, ADR-014).

Who gets what:
- everyone, silently: the daily schedule at noon (one Follow button per game) and the nightly
  results after the day's games (one 📊 button per game). Nothing else about unfollowed games;
- followers of a game (pressed Follow, or auto-follow by favourite team), with sound: a reminder
  before tip-off, the tip-off, a score card after every quarter and the final card.

Idle cost is near zero: the schedule is refreshed every couple of hours, and each game gets a
task that sleeps until its reminder / tip-off, polls every ~45 s while live, then exits.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from euroleague_notifier import reports
from euroleague_notifier.api import ApiError, EuroleagueClient
from euroleague_notifier.butler import ButlerClient, ButlerError
from euroleague_notifier.config import Settings
from euroleague_notifier.models import REGULATION_PERIODS, BoxScore, Club, Game, PeriodState
from euroleague_notifier.store import EventStore

log = logging.getLogger(__name__)

KINDS = [
    {"value": "schedule", "label": "Daily schedule"},
    {"value": "results", "label": "Nightly results"},
]
RESULTS_WINDOW = timedelta(hours=11)  # after RESULTS_TIME; later than that the summary is stale
RESULTS_PATIENCE = timedelta(hours=2)  # how long the summary waits for an unfinished or missing game
SCHEDULE_REFRESH = timedelta(hours=2)
LOOP_INTERVAL = timedelta(minutes=10)
LOOKAHEAD = timedelta(hours=26)  # start tracking (sleeping) tasks this far ahead
MAX_GAME_LENGTH = timedelta(hours=4)  # stop polling a game this long after tip-off
LIVE_LEAD = timedelta(minutes=2)  # start polling shortly before tip-off
QUARTER_TTL = timedelta(minutes=30)  # undelivered quarter reports go stale (AC11)
REMINDER_RETRY = timedelta(minutes=1)
MAX_NAP = timedelta(minutes=1)  # longest single sleep while waiting for a time
CATCH_UP_LIMIT = timedelta(minutes=3)  # stop waiting for the box score to match the play-by-play

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def results_day(due: datetime) -> date:
    """The game day a results summary sent at ``due`` covers: the evening before if it's sent
    in the early hours (before noon), else that same day."""
    return due.date() - timedelta(days=1) if due.hour < 12 else due.date()


def build_manifest(clubs: list[Club], name: str, command: str, callback_url: str | None = None) -> dict:
    """Project manifest; with a ``callback_url`` PiButler also routes the live-scores command to it."""
    unique = sorted({c.code: c for c in clubs}.values(), key=lambda c: c.short_name)
    manifest = {
        "name": name,
        "settings": [
            {
                "key": "teams",
                "label": "Auto-follow teams",
                "default": [],  # not "All": auto-following everything would follow every game
                "options": [{"value": c.code, "label": c.short_name} for c in unique],
            },
            {
                # "daily", not the old "kind": selections saved for the old start/final options
                # must not hide the new nightly results.
                "key": "daily",
                "label": "Notifications",
                "options": KINDS,
                "default": [k["value"] for k in KINDS],
            },
        ],
    }
    if callback_url:
        commands = [{"command": command, "description": f"Live scores of today's {name} games"}]
        manifest |= {"commands": commands, "callback_url": callback_url}
    return manifest


def topic(game: Game) -> dict:
    name = reports.title(game)
    return {
        "id": game.identifier,
        "title": name,
        "auto_follow": {"teams": [game.home.code, game.away.code]},
        "follow_text": (
            f"⭐ You're following <b>{escape(name)}</b> ({{{{hm:{reports.iso_z(game.tipoff)}}}}}).\n"
            "You'll get a reminder before tip-off, a score card after every quarter and the final result, "
            "each with 📊 for the full stats."
        ),
        "unfollow_text": f"Unfollowed {name}",
    }


def follow_button(game: Game) -> dict:
    return {
        "label": "☆ Follow this game",
        "label_active": "⭐ Following · tap to unfollow",
        "follow": game.identifier,
    }


def stats_button(full_report: str) -> dict:
    """Reveals the full tables on demand, keeping the notification itself compact."""
    return {"label": "📊 Stats", "reveal": full_report}


def followers(game: Game) -> dict:
    return {"topic": game.identifier, "following": True}


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
        self._tracked: dict[str, Game] = {}  # the version of each game its task was started with
        self._games: dict[str, tuple[str, Game]] = {}  # identifier -> (competition, game)
        self._schedule_at: datetime | None = None
        self._lagging: dict[str, datetime] = {}
        self._result_boxes: dict[
            str, dict[str, BoxScore]
        ] = {}  # results key -> finished box scores  # message key -> when its box score was first behind

    # --- main loop ---------------------------------------------------------------------------

    async def run(self) -> None:
        await self.register()
        while True:
            try:
                await self.tick()
                self._beat()
            except Exception:
                log.exception("main loop error")
            await self.sleep(LOOP_INTERVAL.total_seconds())

    def games(self) -> list[tuple[str, Game]]:
        """The loaded schedule as ``(competition, game)`` pairs (read-only snapshot)."""
        return list(self._games.values())

    def _beat(self) -> None:
        if self.on_tick:
            self.on_tick()

    async def register(self) -> None:
        """Register the manifest, retrying until both the API and PiButler are reachable."""
        delay = 5.0
        while True:
            self._beat()  # alive while waiting, not only once ticking
            try:
                clubs = [
                    c
                    for comp in self.settings.competitions
                    for c in await self.el.clubs(comp, self.settings.season(comp))
                ]
                s = self.settings
                await self.butler.register(
                    build_manifest(clubs, s.project_name, s.score_command, s.callback_url)
                )
                log.info("registered manifest with %d teams", len({c.code for c in clubs}))
                return
            except (ApiError, ButlerError) as exc:
                log.warning("registration failed (%s), retrying in %.0fs", exc, delay)
                await self.sleep(delay)
                delay = min(delay * 2, 300)

    async def tick(self) -> None:
        """Refresh the schedule, send the daily digest and (re)start game tasks.

        A failing refresh or digest doesn't stop tracking the games already known; only before any
        schedule has loaded does the error propagate.
        """
        now = self.now()
        if self._schedule_at is None or now - self._schedule_at >= SCHEDULE_REFRESH:
            try:
                await self.refresh_schedule()
                self._schedule_at = now
            except Exception:
                if self._schedule_at is None:
                    raise
                log.exception("schedule refresh failed, keeping the previous one")
        try:
            await self.maybe_send_schedule()
        except Exception:
            log.exception("daily schedule failed")
        try:
            await self.maybe_send_results()
        except Exception:
            log.exception("nightly results failed")
        for gid, (comp, game) in self._games.items():
            running = gid in self._tasks and not self._tasks[gid].done()
            if running and self._tracked[gid].tipoff != game.tipoff and not await self._started(gid):
                log.info(
                    "%s: tip-off moved %s → %s, restarting",
                    gid,
                    reports.iso_z(self._tracked[gid].tipoff),
                    reports.iso_z(game.tipoff),
                )
                self._tasks[gid].cancel()
                running = False
            if not running and self._should_track(game, now) and not await self.final_sent(gid):
                log.info(
                    "%s: tracking %s vs %s (tip-off %s)",
                    gid,
                    game.home.code,
                    game.away.code,
                    reports.iso_z(game.tipoff),
                )
                self._tasks[gid] = asyncio.create_task(self.track(comp, game), name=gid)
                self._tracked[gid] = game

    async def refresh_schedule(self) -> None:
        games: dict[str, tuple[str, Game]] = {}
        for comp in self.settings.competitions:
            for game in await self.el.games(comp, self.settings.season(comp)):
                games[game.identifier] = (comp, game)
        self._games = games
        log.info("schedule refreshed: %d games", len(games))

    def _should_track(self, game: Game, now: datetime) -> bool:
        return now - MAX_GAME_LENGTH < game.tipoff < now + LOOKAHEAD

    async def _started(self, gid: str) -> bool:
        """True once anything live has been sent; a schedule change then no longer matters."""
        return await self.events.has(f"{gid}:start") or await self.events.last_period(gid) > 0

    async def final_sent(self, gid: str) -> bool:
        """True once the game's final message has gone out (the game is over)."""
        return await self.events.has(f"{gid}:final")

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
            reports.daily_schedule(upcoming, self.settings.project_name, self.settings.results_time),
            tags={"daily": ["schedule"]},
            topics=[topic(g) for g in upcoming],
            silent=True,
            buttons=buttons,
        )

    # --- nightly results ---------------------------------------------------------------------

    async def maybe_send_results(self) -> None:
        """At RESULTS_TIME, one silent message with every result of that game day.

        Waits (up to RESULTS_PATIENCE) for games that are still running or whose box score is
        missing, and gives up once the summary would be stale (RESULTS_WINDOW).
        """
        tz = ZoneInfo(self.settings.digest_timezone)
        local_now = self.now().astimezone(tz)
        due = datetime.combine(local_now.date(), self.settings.results_time, tz)
        if not due <= local_now < due + RESULTS_WINDOW:
            return
        day = results_day(due)
        key = f"results:{day.isoformat()}"
        played = sorted(
            ((c, g) for c, g in self._games.values() if g.tipoff.astimezone(tz).date() == day),
            key=lambda cg: (cg[1].tipoff, cg[1].code),
        )
        if not played or await self.events.has(key):
            return
        boxes = self._result_boxes.setdefault(key, {})
        missing = [g for _, g in played if g.identifier not in boxes]
        fetched = await asyncio.gather(
            *(self.el.boxscore(g.season, g.code) for g in missing), return_exceptions=True
        )
        for game, box in zip(missing, fetched, strict=True):
            over = await self.final_sent(game.identifier)  # the tracker saw the final buzzer
            if isinstance(box, BoxScore) and (not box.live or over):
                boxes[game.identifier] = box  # finished games don't change: fetch once
        pending = [g for _, g in played if g.identifier not in boxes]
        if pending and local_now < due + RESULTS_PATIENCE:
            log.info("%s: waiting for %s", key, ", ".join(g.identifier for g in pending))
            return
        if not boxes:
            return
        finished = [(g, boxes[g.identifier]) for _, g in played if g.identifier in boxes]
        buttons = [
            [{"label": reports.result_label(g, box), "reveal": reports.final_report(g, box)}]
            for g, box in finished
        ]
        await self._send(
            key,
            reports.daily_results(
                [g for g, _ in finished],
                day,
                self.settings.project_name,
                self.settings.score_command,
                missing=len(pending),
            ),
            tags={"daily": ["results"]},
            buttons=buttons,
            silent=True,
        )
        self._result_boxes.pop(key, None)

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
        key = f"{game.identifier}:reminder:{reports.iso_z(game.tipoff)}"  # a rescheduled game gets a new one
        while self.now() < game.tipoff and not await self.events.has(key):
            try:
                await self._send(
                    key,
                    reports.reminder(game, minutes),
                    expires_at=reports.iso_z(game.tipoff),
                    topics=[topic(game)],
                    audience=followers(game),
                    buttons=[[follow_button(game)]],
                )
            except ButlerError as exc:
                log.warning("%s: reminder failed, retrying: %s", game.identifier, exc)
                left = (game.tipoff - self.now()).total_seconds()
                await self.sleep(max(0.0, min(REMINDER_RETRY.total_seconds(), left)))

    async def poll(self, competition: str, game: Game) -> bool:
        """One live poll. Returns True once the game is over and both final messages are out."""
        season, gid = game.season, game.identifier
        state = await self.el.period_state(season, game.code)
        if state.game_over:
            return await self._finals(competition, game, state)
        started = state.current_period >= 1 and state.ended_periods == 0
        if started and not await self.events.has(f"{gid}:start"):
            await self._send(
                f"{gid}:start",
                reports.tipoff(game),
                expires_at=reports.iso_z(self.now() + QUARTER_TTL),
                topics=[topic(game)],
                audience=followers(game),  # followers only
                buttons=[[follow_button(game)]],
            )
        period = state.ended_periods
        # Only the latest ended period: after a restart we don't replay old quarters.
        if period > await self.events.last_period(gid) and _another_period_follows(state):
            key = f"{gid}:p{period}"
            box = await self.el.boxscore(season, game.code)
            if box is None or not self._caught_up(key, _box_reached(box, state.score)):
                return False
            await self._send(
                key,
                reports.quarter_card(game, box, period, state.score),
                expires_at=reports.iso_z(self.now() + QUARTER_TTL),
                topics=[topic(game)],
                audience=followers(game),
                buttons=[
                    [stats_button(reports.quarter_report(game, box, period, state.score))],
                    [follow_button(game)],
                ],
            )
        return False

    async def _finals(self, competition: str, game: Game, state: PeriodState) -> bool:
        gid = game.identifier
        if await self.final_sent(gid):
            return True
        box = await self.el.boxscore(game.season, game.code)
        if box is None:
            return False
        final = not box.live and (state.score is None or (box.home.points, box.away.points) == state.score)
        if not self._caught_up(f"{gid}:final", final):
            return False
        full = reports.final_report(game, box)
        url = reports.game_url(game, competition)
        link = [{"label": "🔗 Game center", "url": url}] if url else []
        buttons = [[stats_button(full), *link]]
        await self._send(
            f"{gid}:final",
            reports.final_card(game, box),
            topics=[topic(game)],
            audience=followers(game),
            buttons=buttons,
        )
        return True

    def _caught_up(self, key: str, caught_up: bool) -> bool:
        """Whether to send ``key`` now: once its box score has caught up, or after ``CATCH_UP_LIMIT``.

        The Boxscore and PlayByPlay are cached independently, so the box score can lag by a poll or
        two; the limit keeps a broken feed from blocking the message forever.
        """
        if caught_up:
            self._lagging.pop(key, None)
            return True
        since = self._lagging.setdefault(key, self.now())
        if self.now() - since < CATCH_UP_LIMIT:
            log.info("%s: box score behind the play-by-play, waiting", key)
            return False
        log.warning("%s: box score still behind after %s, sending anyway", key, CATCH_UP_LIMIT)
        return True

    async def _send(
        self, key: str, text: str, tags: dict | None = None, expires_at: str | None = None, **extra
    ) -> None:
        await self.butler.notify(key, text, tags, expires_at, **extra)
        await self.events.add(key)

    async def _sleep_until(self, when: datetime) -> None:
        # Short naps against the wall clock: asyncio timers don't advance while the host sleeps
        # (e.g. a laptop lid), so one long sleep could wake up far too late.
        while (delay := (when - self.now()).total_seconds()) > 0:
            await self.sleep(min(delay, MAX_NAP.total_seconds()))


def _another_period_follows(state: PeriodState) -> bool:
    """False when the ended period may be the last one, so the final report covers it instead.

    From Q4 on, a period is followed by overtime only if it ends tied; otherwise ``EG`` is imminent.
    An unknown score keeps the report.
    """
    if state.ended_periods < REGULATION_PERIODS or state.score is None:
        return True
    home, away = state.score
    return home == away


def _box_reached(box: BoxScore, score: tuple[int, int] | None) -> bool:
    """Box score totals are at least the play-by-play score (stats are game-to-date)."""
    return score is None or (box.home.points >= score[0] and box.away.points >= score[1])
