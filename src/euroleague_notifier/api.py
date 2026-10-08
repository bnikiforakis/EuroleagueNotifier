"""Async client and pure parsers for the public EuroLeague APIs.

Sources:
- Schedule and clubs: ``api-live.euroleague.net/v2`` (schedule is cached ``max-age=7200``, so its
  ``played``/score fields can lag by up to two hours; use the live feeds for in-game state).
- Header, PlayByPlay and Boxscore: ``live.euroleague.net/api`` (``max-age=60``).

The box score comes from ``live.euroleague.net/api/Boxscore`` rather than the v3 ``/stats`` endpoint:
both are cached for 60s, but the live feed also carries per-period scores (``ByQuarter``, including
every overtime separately) and team totals in one payload, and shares its home/away ordering
(``A``/first entry = home) with Header and PlayByPlay.

Live feeds answer ``200`` with an empty body before tip-off; ``header`` and ``boxscore`` return
``None`` then and ``period_state`` reports nothing started.
"""

import asyncio
import json
import re
from datetime import datetime
from types import TracebackType
from typing import Any, Self

import httpx

from euroleague_notifier.models import (
    REGULATION_PERIODS,
    BoxScore,
    Club,
    Game,
    LiveHeader,
    PeriodState,
    PlayerLine,
    TeamLine,
)

API_BASE = "https://api-live.euroleague.net"
LIVE_BASE = "https://live.euroleague.net/api"
USER_AGENT = "Mozilla/5.0 (compatible; euroleague-notifier/0.1)"
TIMEOUT = 15.0
ATTEMPTS = 3
BACKOFF = 1.0

PBP_SECTIONS = ("FirstQuarter", "SecondQuarter", "ThirdQuarter", "ForthQuarter", "ExtraTime")


class ApiError(Exception):
    """Raised when an endpoint keeps failing or returns something unparseable."""


def _data(payload: Any) -> list[dict]:
    return payload["data"] if isinstance(payload, dict) else payload


def _int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(str(value).strip())


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _club(raw: dict) -> Club:
    name = _str(raw.get("name"))
    return Club(code=_str(raw.get("code")), name=name, short_name=_str(raw.get("abbreviatedName")) or name)


def parse_clubs(payload: Any) -> list[Club]:
    return [_club(raw) for raw in _data(payload)]


def parse_games(payload: Any) -> list[Game]:
    """Parse the v2 schedule; tip-off comes from ``utcDate`` and is timezone aware."""
    games = []
    for raw in _data(payload):
        played = bool(raw.get("played"))
        venue = raw.get("venue") or {}
        games.append(
            Game(
                season=_str(raw["season"]["code"]) if isinstance(raw.get("season"), dict) else "",
                code=int(raw["gameCode"]),
                round=raw.get("round"),
                tipoff=datetime.fromisoformat(raw["utcDate"].replace("Z", "+00:00")),
                home=_club(raw["local"]["club"]),
                away=_club(raw["road"]["club"]),
                played=played,
                home_score=raw["local"].get("score") if played else None,
                away_score=raw["road"].get("score") if played else None,
                venue=_str(venue.get("name")) or None,
            )
        )
    return games


def _per_period(cumulative: list[int]) -> list[int]:
    return [max(0, cur - prev) for prev, cur in zip([0, *cumulative], cumulative, strict=False)]


def parse_header(payload: Any) -> LiveHeader | None:
    """Parse the live Header; ``None`` for the empty pre-game payload.

    Header period scores are cumulative and lump all overtimes into ``ScoreExtraTime``, so
    overtime appears as one combined entry here. Use ``BoxScore.quarter_scores`` for per-OT detail.
    """
    if not payload:
        return None
    home_cum = [_int(payload.get(f"ScoreQuarter{i}A")) for i in range(1, REGULATION_PERIODS + 1)]
    away_cum = [_int(payload.get(f"ScoreQuarter{i}B")) for i in range(1, REGULATION_PERIODS + 1)]
    started = [i for i in range(REGULATION_PERIODS) if home_cum[i] or away_cum[i]]
    periods = started[-1] + 1 if started else 0
    home_cum, away_cum = home_cum[:periods], away_cum[:periods]
    home_ot, away_ot = _int(payload.get("ScoreExtraTimeA")), _int(payload.get("ScoreExtraTimeB"))
    if periods == REGULATION_PERIODS and (home_ot or away_ot):
        home_cum.append(home_ot)
        away_cum.append(away_ot)
    quarter_match = re.search(r"\d+", _str(payload.get("Quarter")))
    return LiveHeader(
        live=bool(payload.get("Live")),
        home_score=_int(payload.get("ScoreA")),
        away_score=_int(payload.get("ScoreB")),
        quarter=int(quarter_match.group()) if quarter_match else None,
        remaining=_str(payload.get("RemainingPartialTime")) or None,
        quarter_scores=list(zip(_per_period(home_cum), _per_period(away_cum), strict=True)),
    )


def parse_period_state(payload: Any) -> PeriodState:
    """Derive period progress from PlayByPlay ``BP``/``EP``/``EG`` markers.

    The last period of a game may close with ``EG`` only (no ``EP``), so a game that is over counts
    every begun period as ended. Markers carry no score themselves; plays that score carry the running
    ``POINTS_A``/``POINTS_B`` (home/away), so the last one before a marker is the score at that marker.
    """
    if not payload:
        return PeriodState(ended_periods=0, game_over=False, current_period=0)
    begun = ended = 0
    game_over = False
    running, score = (0, 0), None
    for play in (play for section in PBP_SECTIONS for play in payload.get(section) or []):
        kind = _str(play.get("PLAYTYPE"))
        if play.get("POINTS_A") is not None and play.get("POINTS_B") is not None:
            running = (_int(play["POINTS_A"]), _int(play["POINTS_B"]))
        if kind == "BP":
            begun += 1
        elif kind == "EP":
            ended += 1
            score = running
        elif kind == "EG":
            game_over = True
            score = running
    if game_over:
        ended = max(ended, begun)
    if begun == 0:  # nothing has started yet, whatever ActualQuarter says before tip-off
        return PeriodState(ended_periods=ended, game_over=game_over, current_period=0, score=score)
    actual = payload.get("ActualQuarter")
    current = _int(actual) if actual not in (None, "") else begun
    return PeriodState(
        ended_periods=ended, game_over=game_over, current_period=max(current, begun), score=score
    )


def _player(raw: dict) -> PlayerLine:
    return PlayerLine(
        name=_str(raw.get("Player")),
        team_code=_str(raw.get("Team")),
        minutes=_str(raw.get("Minutes")) or "DNP",
        points=_int(raw.get("Points")),
        rebounds=_int(raw.get("TotalRebounds")),
        assists=_int(raw.get("Assistances")),
        steals=_int(raw.get("Steals")),
        turnovers=_int(raw.get("Turnovers")),
        blocks=_int(raw.get("BlocksFavour")),
        fouls=_int(raw.get("FoulsCommited")),
        pir=_int(raw.get("Valuation")),
        fg2m=_int(raw.get("FieldGoalsMade2")),
        fg2a=_int(raw.get("FieldGoalsAttempted2")),
        fg3m=_int(raw.get("FieldGoalsMade3")),
        fg3a=_int(raw.get("FieldGoalsAttempted3")),
        ftm=_int(raw.get("FreeThrowsMade")),
        fta=_int(raw.get("FreeThrowsAttempted")),
        plus_minus=raw.get("Plusminus"),
        starter=bool(raw.get("IsStarter")),
    )


def _team(raw: dict, players: list[PlayerLine]) -> TeamLine:
    totals = raw["totr"]
    return TeamLine(
        code=players[0].team_code if players else "",
        name=_str(raw.get("Team")),
        points=_int(totals.get("Points")),
        fg2m=_int(totals.get("FieldGoalsMade2")),
        fg2a=_int(totals.get("FieldGoalsAttempted2")),
        fg3m=_int(totals.get("FieldGoalsMade3")),
        fg3a=_int(totals.get("FieldGoalsAttempted3")),
        ftm=_int(totals.get("FreeThrowsMade")),
        fta=_int(totals.get("FreeThrowsAttempted")),
        rebounds=_int(totals.get("TotalRebounds")),
        off_reb=_int(totals.get("OffensiveRebounds")),
        def_reb=_int(totals.get("DefensiveRebounds")),
        assists=_int(totals.get("Assistances")),
        steals=_int(totals.get("Steals")),
        turnovers=_int(totals.get("Turnovers")),
        blocks=_int(totals.get("BlocksFavour")),
        fouls=_int(totals.get("FoulsCommited")),
        pir=_int(totals.get("Valuation")),
    )


def _period_points(row: dict) -> list[int | None]:
    keys = [f"Quarter{i}" for i in range(1, REGULATION_PERIODS + 1)]
    keys += sorted((k for k in row if k.startswith("Extra")), key=lambda k: _int(k.removeprefix("Extra")))
    return [row.get(k) for k in keys]


def parse_boxscore(payload: Any) -> BoxScore | None:
    """Parse the live Boxscore (first team = home); ``None`` for the empty pre-game payload.

    ``quarter_scores`` stops at the first period with no data. During a game, periods not yet played
    may show as 0, so callers should slice by the period they know has ended.
    """
    if not payload or not payload.get("Stats"):
        return None
    sides = []
    for raw in payload["Stats"][:2]:
        players = [_player(p) for p in raw.get("PlayersStats") or [] if _str(p.get("Player_ID"))]
        sides.append((_team(raw, players), players))
    (home, home_players), (away, away_players) = sides
    quarters: list[tuple[int, int]] = []
    rows = payload.get("ByQuarter") or []
    if len(rows) >= 2:
        for h, a in zip(_period_points(rows[0]), _period_points(rows[1]), strict=False):
            if h is None or a is None:
                break
            quarters.append((int(h), int(a)))
    return BoxScore(
        home=home,
        away=away,
        home_players=home_players,
        away_players=away_players,
        quarter_scores=quarters,
        live=bool(payload.get("Live")),
    )


class EuroleagueClient:
    """Async EuroLeague API client; use as ``async with EuroleagueClient() as client:``."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, backoff: float = BACKOFF):
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._backoff = backoff

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET and decode JSON (``None`` for an empty body), retrying network errors and 5xx."""
        last: Exception | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last = exc
                continue
            if response.status_code >= 500:
                last = ApiError(f"{response.status_code} from {response.url}")
                continue
            if response.status_code >= 400:
                raise ApiError(f"{response.status_code} from {response.url}")
            if not response.content.strip():
                return None
            try:
                return response.json()
            except json.JSONDecodeError as exc:
                raise ApiError(f"invalid JSON from {response.url}") from exc
        raise ApiError(f"GET {url} failed after {ATTEMPTS} attempts: {last}") from last

    async def _live(self, feed: str, season: str, code: int) -> Any:
        return await self._get(f"{LIVE_BASE}/{feed}", {"gamecode": code, "seasoncode": season})

    async def _v2(self, path: str) -> Any:
        """Schedule/clubs payload; an empty body is an error here, so callers back off and retry."""
        url = f"{API_BASE}/v2/{path}"
        payload = await self._get(url)
        if payload is None:
            raise ApiError(f"empty response from {url}")
        return payload

    async def games(self, competition: str, season: str) -> list[Game]:
        return parse_games(await self._v2(f"competitions/{competition}/seasons/{season}/games"))

    async def clubs(self, competition: str, season: str) -> list[Club]:
        return parse_clubs(await self._v2(f"competitions/{competition}/seasons/{season}/clubs"))

    async def header(self, season: str, code: int) -> LiveHeader | None:
        return parse_header(await self._live("Header", season, code))

    async def period_state(self, season: str, code: int) -> PeriodState:
        return parse_period_state(await self._live("PlayByPlay", season, code))

    async def boxscore(self, season: str, code: int) -> BoxScore | None:
        return parse_boxscore(await self._live("Boxscore", season, code))
