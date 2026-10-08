"""PiButler command callback: ``/score`` as one message navigated in place (list → game → stats).

PiButler POSTs ``{"type": "command" | "action", ...}`` with the project's API key and shows the
``{"text", "buttons"}`` reply, so every view is rebuilt from the notifier's loaded schedule plus the
live Header/Boxscore feeds. Those lookups go through a short TTL cache: PiButler gives up after 8 s,
and many users (or one impatient one) must not multiply the requests to the EuroLeague API.

Action data stays within ``ACTION_RE``: ``l`` (list), ``g|<ref>`` (game), ``s|<ref>`` (stats), where
``<ref>`` is the game code, prefixed by the competition when following several (``g|E|36``).
"""

import asyncio
import hmac
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import web

from euroleague_notifier import reports
from euroleague_notifier.api import ApiError
from euroleague_notifier.models import REGULATION_PERIODS, BoxScore, Game, LiveHeader, period_label
from euroleague_notifier.tracker import MAX_GAME_LENGTH, Notifier

log = logging.getLogger(__name__)

ACTION_RE = re.compile(r"^[A-Za-z0-9_.|=-]{1,32}$")
CACHE_TTL = 15.0
FETCH_TIMEOUT = 5.0  # well inside PiButler's 8 s
MAX_TEXT = 4000
MAX_LABEL = 64
MAX_ROWS = 12
UNAVAILABLE = "⚠️ Live data unavailable right now."
TOO_LONG = "⚠️ That's too much to show in one message."
MAX_GAMES = MAX_ROWS - 1  # one button row per game; keep a row free
BACK = {"label": "⬅️ Back", "action": "l"}

State = Literal["upcoming", "live", "final", "unknown"]


class TtlCache:
    """Caches awaitable results for ``ttl`` seconds; concurrent callers share one in-flight fetch.

    Failures aren't cached. A fetch that outlives ``timeout`` keeps running and fills the cache for
    the next caller, while this one gets ``TimeoutError``.
    """

    def __init__(
        self, ttl: float, timeout: float = FETCH_TIMEOUT, clock: Callable[[], float] = time.monotonic
    ):
        self.ttl = ttl
        self.timeout = timeout
        self.clock = clock
        self._entries: dict[Any, tuple[float, asyncio.Future]] = {}

    async def get(self, key: Any, fetch: Callable[[], Awaitable[Any]]) -> Any:
        now = self.clock()
        entry = self._entries.get(key)
        if entry is None or entry[0] <= now or _failed(entry[1]):
            self._entries = {k: e for k, e in self._entries.items() if e[0] > now}
            future = asyncio.ensure_future(fetch())
            future.add_done_callback(_failed)  # retrieves the error even if every caller timed out
            entry = (now + self.ttl, future)
            self._entries[key] = entry
        try:
            return await asyncio.wait_for(asyncio.shield(entry[1]), self.timeout)
        except Exception:
            if self._entries.get(key) is entry and entry[1].done():
                del self._entries[key]
            raise


def _failed(future: asyncio.Future) -> bool:
    return future.done() and (future.cancelled() or future.exception() is not None)


@dataclass(frozen=True)
class Status:
    state: State
    header: LiveHeader | None = None
    score: tuple[int, int] | None = None


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[dict]] = field(default_factory=list)

    def as_dict(self) -> dict:
        # Never cut HTML mid-tag: an over-long message is replaced, not truncated.
        text = self.text if len(self.text) <= MAX_TEXT else TOO_LONG
        return {"text": text, "buttons": self.buttons[:MAX_ROWS]}


def _zone(name: Any, default: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo(default)


def _fit(prefix: str, rest: str) -> str:
    """``prefix + rest`` cut to ``MAX_LABEL`` characters, shortening ``rest`` (never a placeholder)."""
    room = MAX_LABEL - len(prefix)
    return prefix + (rest if len(rest) <= room else rest[: room - 1] + "…")


def _period_name(index: int) -> str:
    # The Header lumps every overtime into one entry, so the fifth is "OT", not "OT1".
    return period_label(index) if index <= REGULATION_PERIODS else "OT"


def _clock(header: LiveHeader) -> str:
    """``Q3 05:12``, or ``Break`` while the Header has no current quarter."""
    if header.quarter is None:
        return "Break"
    return " ".join(filter(None, (period_label(header.quarter), header.remaining)))


def _clock_line(header: LiveHeader) -> str:
    """``Q3 · 05:12 left`` for the game view."""
    if header.quarter is None:
        return "Break"
    period = period_label(header.quarter)
    return f"{period} · {header.remaining} left" if header.remaining else period


def _periods_line(scores: list[tuple[int, int]], exact: bool = False) -> str:
    """``Q1 22-18 · Q2 15-17``. ``exact`` periods name each overtime (OT1, OT2); the Header's don't."""
    name = period_label if exact else _period_name
    return " · ".join(f"{name(i)} {h}{reports.DASH}{a}" for i, (h, a) in enumerate(scores, 1))


def _started(scores: list[tuple[int, int]], header: LiveHeader | None) -> list[tuple[int, int]]:
    """Box-score periods that have begun (the feed reports 0-0 for periods not yet played)."""
    if header is None or not header.live:
        return list(scores)
    return list(scores[: max(header.quarter or 0, len(header.quarter_scores))])


def _matchup(game: Game) -> str:
    return reports.matchup(game)


class Scoreboard:
    """Builds the ``/score`` views from the notifier's schedule and the live feeds."""

    def __init__(self, notifier: Notifier, cache_ttl: float = CACHE_TTL, timeout: float = FETCH_TIMEOUT):
        self.notifier = notifier
        self.settings = notifier.settings
        self.el = notifier.el
        self.cache = TtlCache(cache_ttl, timeout)

    # --- entry point -------------------------------------------------------------------------

    async def reply(self, body: dict) -> Reply:
        user = body.get("user")
        tz = _zone(user.get("timezone") if isinstance(user, dict) else None, self.settings.digest_timezone)
        kind = body.get("type")
        try:
            if kind == "command" and body.get("command") == "score":
                return await self.list_view(tz)
            if kind == "action":
                return await self.action(str(body.get("data") or ""), tz)
        except Exception:
            log.exception("score view failed")
            return Reply(UNAVAILABLE, [[BACK]])
        return Reply("🤷 Unknown command. Try /score for today's games.")

    async def action(self, data: str, tz: ZoneInfo) -> Reply:
        view, _, ref = data.partition("|")
        if data == "l":
            return await self.list_view(tz)
        if view in ("g", "s") and (found := self._find(ref)):
            return await (self.game_view if view == "g" else self.stats_view)(*found)
        if view in ("g", "s"):
            return Reply("That game isn't on the schedule any more.", [[BACK]])
        return Reply("🤷 That button has expired. Try /score again.")

    # --- views -------------------------------------------------------------------------------

    async def list_view(self, tz: ZoneInfo) -> Reply:
        now = self.notifier.now()
        today = now.astimezone(tz).date()
        games = self.notifier.games()
        if not games:
            return Reply("⏳ The schedule is still loading. Try again in a minute.")

        def is_today(game: Game) -> bool:
            return game.tipoff.astimezone(tz).date() == today

        candidates = [(c, g) for c, g in games if is_today(g) or now - MAX_GAME_LENGTH < g.tipoff <= now]
        statuses = await asyncio.gather(*(self._status(g) for _, g in candidates))
        shown = sorted(
            (
                (c, g, s)
                for (c, g), s in zip(candidates, statuses, strict=True)
                if is_today(g) or s.state == "live"
            ),
            key=lambda row: (row[1].tipoff, row[1].code),
        )
        if not shown:
            text = "🏀 <b>Today's EuroLeague games</b>\n\nNo EuroLeague games today."
            upcoming = [g for _, g in games if g.tipoff > now]
            if upcoming:
                nxt = min(upcoming, key=lambda g: (g.tipoff, g.code))
                text += f"\nNext: {{{{time:{reports.iso_z(nxt.tipoff)}}}}} {_matchup(nxt)}"
            return Reply(text)
        lines = ["🏀 <b>Today's EuroLeague games</b>", "", "Tap a game for the live score."]
        if any(s.state == "unknown" for _, _, s in shown):
            lines += ["", UNAVAILABLE]
        if len(shown) > MAX_GAMES:
            lines += ["", f"…and {len(shown) - MAX_GAMES} more later today."]
            shown = shown[:MAX_GAMES]
        buttons = [[{"label": self._label(g, s), "action": f"g|{self._ref(c, g)}"}] for c, g, s in shown]
        return Reply("\n".join(lines), buttons)

    async def game_view(self, competition: str, game: Game) -> Reply:
        ref = self._ref(competition, game)
        status, box = await self._status_and_box(game)
        refresh = {"label": "🔄 Refresh", "action": f"g|{ref}"}
        stats = {"label": "📊 Stats", "action": f"s|{ref}"}
        if status.state == "upcoming":
            lines = [f"⏳ {_matchup(game)}", f"Tip-off {{{{time:{reports.iso_z(game.tipoff)}}}}}"]
            if game.venue:
                lines.append(f"📍 {escape(game.venue.title())}")
            return Reply("\n".join(lines), [[refresh], [BACK]])
        if status.state == "unknown":
            return Reply(f"🏀 {_matchup(game)}\n\n{UNAVAILABLE}", [[refresh], [BACK]])
        header, (home, away) = status.header, status.score or (0, 0)
        # The box score has each overtime separately; the Header lumps them together.
        periods = _started(box.quarter_scores, header) if box else (header.quarter_scores if header else [])
        if status.state == "live" and header:
            lines = [f"🔴 {reports.score_line(game, home, away, bold_both=True)}", _clock_line(header)]
        else:
            score = reports.score_line(game, home, away, bold_winner=True)
            lines = [f"✅ <b>Final</b>{reports.ot_suffix(len(periods))} · {score}"]
        if periods:
            lines += ["", _periods_line(periods, exact=box is not None)]
        return Reply("\n".join(lines), [[stats, refresh], [BACK]])

    async def stats_view(self, competition: str, game: Game) -> Reply:
        ref = self._ref(competition, game)
        buttons = [[{"label": "🔄 Refresh", "action": f"s|{ref}"}, {"label": "⬅️ Game", "action": f"g|{ref}"}]]
        status, box = await self._status_and_box(game)
        if status.state in ("live", "final") and box is None:
            return Reply(f"📊 {_matchup(game)}\n\n{UNAVAILABLE}", buttons)
        if box is None:
            text = UNAVAILABLE if status.state == "unknown" else "No stats yet: the game hasn't started."
            return Reply(f"📊 {_matchup(game)}\n\n{text}", buttons)
        home, away = status.score or (box.home.points, box.away.points)
        score = escape(reports.code_score(game, home, away))
        if status.state == "live" and status.header:
            header = status.header
            head = f"📊 <b>Live stats</b> · {_clock(header)} · {score}"
            sections = reports.box_sections(
                game, box, _started(box.quarter_scores, header), reports.QUARTER_STATS
            )
        else:
            head = f"📊 <b>Final</b>{reports.ot_suffix(len(box.quarter_scores))} · {score}"
            sections = reports.box_sections(game, box, box.quarter_scores, reports.FINAL_STATS)
        return Reply("\n".join([head, "", *sections]), buttons)

    # --- helpers -----------------------------------------------------------------------------

    def _ref(self, competition: str, game: Game) -> str:
        return str(game.code) if len(self.settings.competitions) == 1 else f"{competition}|{game.code}"

    def _find(self, ref: str) -> tuple[str, Game] | None:
        competition, _, code = ref.rpartition("|")
        competition = competition or self.settings.competitions[0]
        return next(
            ((c, g) for c, g in self.notifier.games() if c == competition and str(g.code) == code), None
        )

    def _label(self, game: Game, status: Status) -> str:
        names = f"{game.home.short_name} {reports.DASH} {game.away.short_name}"
        hm = f"{{{{hm:{reports.iso_z(game.tipoff)}}}}} "
        if status.state == "live" and status.header:
            return _fit("🔴 ", f"{reports.code_score(game, *status.score)} · {_clock(status.header)}")
        if status.state == "final" and status.score:
            return _fit("✅ ", reports.code_score(game, *status.score))
        if status.state == "final":
            return _fit("✅ ", names)
        return _fit(("⏳ " if status.state == "upcoming" else "🏀 ") + hm, names)

    async def _header(self, game: Game) -> LiveHeader | None:
        return await self.cache.get(
            ("header", game.season, game.code), lambda: self.el.header(game.season, game.code)
        )

    async def _boxscore(self, game: Game) -> BoxScore | None:
        return await self.cache.get(
            ("box", game.season, game.code), lambda: self.el.boxscore(game.season, game.code)
        )

    async def _status_and_box(self, game: Game) -> tuple[Status, BoxScore | None]:
        """Header and box score fetched together, so one tap stays well inside PiButler's 8 s."""
        if game.tipoff > self.notifier.now():
            return Status("upcoming"), None

        async def box() -> BoxScore | None:
            try:
                return await self._boxscore(game)
            except (ApiError, TimeoutError) as exc:
                log.warning("%s: boxscore unavailable: %s", game.identifier, exc)
                return None

        return await asyncio.gather(self._status(game), box())

    async def _status(self, game: Game) -> Status:
        """Where the game stands; only games that have tipped off hit the live feed."""
        if game.tipoff > self.notifier.now():
            return Status("upcoming")
        try:
            header = await self._header(game)
        except (ApiError, TimeoutError) as exc:
            log.warning("%s: header unavailable: %s", game.identifier, exc)
            return Status("unknown")
        final_sent = await self.notifier.final_sent(game.identifier)
        if header is None:
            if final_sent or game.played:
                played = game.home_score is not None and game.away_score is not None
                return Status("final", score=(game.home_score, game.away_score) if played else None)
            return Status("upcoming")  # past tip-off time, but the feed hasn't started
        score = (header.home_score, header.away_score)
        if header.live and not final_sent:
            return Status("live", header, score)
        if not final_sent and score == (0, 0):
            return Status("upcoming")  # pre-game Header, not a 0-0 final
        return Status("final", header, score)


def create_app(scoreboard: Scoreboard, api_key: str, path: str = "/pibutler") -> web.Application:
    """aiohttp app answering PiButler's command/action callbacks on ``POST <path>``."""
    expected = f"Bearer {api_key}".encode()

    async def handle(request: web.Request) -> web.Response:
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "expected a JSON object"}, status=400)
        reply = await scoreboard.reply(body)
        return web.json_response(reply.as_dict())

    app = web.Application()
    app.router.add_post(path, handle)
    return app


def callback_path(url: str) -> str:
    """The path part of ``CALLBACK_URL`` that the server listens on."""
    return urlsplit(url).path or "/"
