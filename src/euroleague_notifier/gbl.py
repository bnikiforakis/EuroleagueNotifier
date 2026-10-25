"""Greek Basket League (Stoiximan GBL): schedule, live state and box scores (ADR-023).

Sources:
- Schedule: esake.gr, the league's site. Server-rendered HTML, one page per round
  (``EsakeResults?series=NN``); the round page without ``series`` shows the current round. Each game
  has an esake id (hex), its Athens tip-off ("Σαβ 31 Οκτ - 15:00", no year), Greek team names, the
  teams' esake ids (in their logo paths) and, once played, the score.
- Live state and box score: FIBA LiveStats (Genius Sports, the league's stats provider), one JSON
  per game (``data/<matchId>/data.json``, cached 30 s). Its play-by-play has ``period start/end``
  and ``game end`` markers carrying the running score; unlike EuroLeague, the last period also gets
  a ``period end``. The feed answers 403 until the league sets the match up.
- Match ids: nothing links an esake game to its FIBA match, so they're discovered by team codes: the
  Genius ticker esake embeds lists recent matches (id, codes, date), and every match's
  ``othermatches`` lists the rest of its round, including games not started yet.

esake only shows Greek names and FIBA only English ones, so ``TEAMS`` maps each esake team to its
FIBA code and display names. A team missing from it is announced but not tracked live (logged).
"""

import asyncio
import html
import json
import logging
import re
import time as clock
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from euroleague_notifier.api import BACKOFF, ApiError, EuroleagueClient, _int, _str
from euroleague_notifier.highlights import fold
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

log = logging.getLogger(__name__)

COMPETITION = "G"
ESAKE = "https://www.esake.gr"
RESULTS_URL = f"{ESAKE}/el/action/EsakeResults"
GAME_URL = f"{ESAKE}/el/action/EsakegameView?idgame={{code:08X}}&mode=3"
FEED_URL = "https://fibalivestats.dcd.shared.geniussports.com/data/{match}/data.json"
TICKER_URL = "https://widget.wh.geniussports.com/widget/?RFJIN037I07QV520B7WEXKV1GYNA31"
ATHENS = ZoneInfo("Europe/Athens")
FEED_TTL = 20.0  # seconds; the feed itself is cached 30 s, so polling faster gains nothing
DISCOVERY_INTERVAL = 60.0  # seconds between match-id lookups for games that tipped off unmatched
MATCH_WINDOW = timedelta(days=4)  # how far a discovered match may be from its game's tip-off
LOOKBACK = timedelta(hours=30)  # unmatched games this old still count: the nightly results need them


@dataclass(frozen=True)
class Team:
    code: str  # FIBA LiveStats code
    name: str
    short_name: str
    link: str  # shared with the same club in other competitions (ADR-024)


# esake team id -> team. Update when the league changes (promotion/relegation, renames).
TEAMS = {
    "00000001": Team("PAO", "Panathinaikos AKTOR", "Panathinaikos", "gr-panathinaikos"),
    "00000002": Team("OLY", "Olympiacos Piraeus", "Olympiacos", "gr-olympiacos"),
    "00000005": Team("ARI", "Aris", "Aris", "gr-aris"),
    "0000000A": Team("KOL", "Kolossos H Hotels Collection", "Kolossos", "gr-kolossos"),
    "0000000C": Team("PBC", "PAOK", "PAOK", "gr-paok"),
    "0000000D": Team("PER", "Peristeri Betsson", "Peristeri", "gr-peristeri"),
    "0000000F": Team("MAR", "Maroussi", "Maroussi", "gr-maroussi"),
    "00000010": Team("AEK", "AEK", "AEK", "gr-aek"),
    "00000011": Team("IRA", "Iraklis", "Iraklis", "gr-iraklis"),
    "0041ADCB": Team("FAL", "Vikos Falcons", "Falcons", "gr-falcons"),
    "2A25C696": Team("PRO", "Promitheas Patras Vikos Cola", "Promitheas", "gr-promitheas"),
    "3CA10C07": Team("LEF", "Doxa Lefkadas ELABET", "Lefkada", "gr-lefkada"),
    "B742845D": Team("KAR", "Karditsa Iaponiki", "Karditsa", "gr-karditsa"),
    "BB4B460F": Team("MYK", "Mykonos Betsson", "Mykonos", "gr-mykonos"),
}

_MONTHS = {
    "ιαν": 1, "φεβ": 2, "μαρ": 3, "απρ": 4, "μαι": 5, "ιουν": 6,
    "ιουλ": 7, "αυγ": 8, "σεπ": 9, "οκτ": 10, "νοε": 11, "δεκ": 12,
}  # fmt: skip
_GAME_BLOCK = "esake-program-game-row-1"
_TIPOFF = re.compile(r"\w+\s+(\d{1,2})\s+(\w+)\s*-\s*(\d{1,2}):(\d{2})")
_TEAM = re.compile(
    r'<span>(?P<n1>.*?)</span><img src="/dat/esaketeam/(?P<t1>[0-9A-F]+)/'
    r'|<img src="/dat/esaketeam/(?P<t2>[0-9A-F]+)/[^>]*><span>(?P<n2>.*?)</span>',
    re.S,
)
_SCORE = re.compile(r"<span>\s*(\d+)\s*(?:&nbsp;|\s)*-(?:&nbsp;|\s)*(\d+)\s*</span>")
_ROUND = re.compile(r"(\d+)η\s+Αγωνιστική")
_CURRENT_ROUND = re.compile(r"\$j\('#series'\)\.val\('(\d+)'\);\s*\$j\('#series'\)\.selectric")
_ROUNDS = re.compile(r"\$j\('#series'\)\.append\(new Option\('[^']*',\s*'(\d+)'\)\)")
_INFO = re.compile(r'<div class="esake-program-game-info[^"]*">(?:<img[^>]*>)?\s*(.*?)</div>', re.S)


def _plain(text: str) -> str:
    """Tags dropped, entities decoded, whitespace collapsed."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<br\s*/?>|<[^>]+>", " ", text))).strip()


def club(esake_id: str, greek_name: str) -> Club:
    """The club for an esake team; one missing from ``TEAMS`` keeps its esake id and Greek name."""
    team = TEAMS.get(esake_id)
    if team is None:
        name = greek_name.title()
        return Club(code=esake_id, name=name, short_name=name, aliases=(greek_name,))
    return Club(team.code, team.name, team.short_name, aliases=(greek_name,), link=team.link)


def clubs() -> list[Club]:
    return [Club(t.code, t.name, t.short_name, link=t.link) for t in TEAMS.values()]


def _tipoff(text: str, season_year: int) -> datetime | None:
    """ "Σαβ 31 Οκτ - 15:00" in Athens time; the year follows from the season (Sep-Dec: its first)."""
    m = _TIPOFF.search(text)
    month = _MONTHS.get(fold(m.group(2))[:4]) or _MONTHS.get(fold(m.group(2))[:3]) if m else None
    if not m or not month:
        return None
    year = season_year if month >= 8 else season_year + 1
    day, hour, minute = int(m.group(1)), int(m.group(3)), int(m.group(4))
    return datetime(year, month, day, hour, minute, tzinfo=ATHENS)


def parse_round(page: str, season_year: int) -> list[Game]:
    """Games of an esake round page; a game without a readable tip-off or two teams is skipped."""
    games = []
    for block in page.split(_GAME_BLOCK)[1:]:
        block = block.split("esake-program-game-hidden-row", 1)[0]
        code = re.search(r"idgame=([0-9A-F]+)", block)
        info = [_plain(i) for i in _INFO.findall(block)]
        tipoff = _tipoff(info[0], season_year) if info else None
        teams = [(m["t1"] or m["t2"], _plain(m["n1"] or m["n2"])) for m in _TEAM.finditer(block)]
        if not code or tipoff is None or len(teams) != 2:
            log.warning("unreadable esake game skipped: %s", _plain(block)[:120])
            continue
        round_ = _ROUND.search(block)
        score = _SCORE.search(block)
        games.append(
            Game(
                season=f"{COMPETITION}{season_year}",
                code=int(code.group(1), 16),
                round=int(round_.group(1)) if round_ else None,
                tipoff=tipoff,
                home=club(*teams[0]),
                away=club(*teams[1]),
                played=score is not None,
                home_score=int(score.group(1)) if score else None,
                away_score=int(score.group(2)) if score else None,
                venue=info[1] if len(info) > 1 and info[1] else None,
            )
        )
    return games


def parse_rounds(page: str) -> tuple[int | None, list[int]]:
    """The round a page shows by default (the current one) and every round of the phase."""
    current = _CURRENT_ROUND.search(page)
    return (int(current.group(1)) if current else None), sorted({int(r) for r in _ROUNDS.findall(page)})


@dataclass(frozen=True)
class Listed:
    """A FIBA match seen in the ticker or in ``othermatches``: home/away codes, date if known."""

    match: int
    home: str
    away: str
    day: date | None = None


def parse_ticker(script: str) -> list[Listed]:
    """Matches in the Genius ticker script (HTML inside a JS string)."""
    text = script.replace("\\/", "/").replace('\\"', '"')
    found = []
    for item in re.findall(r'<li class="spls_lsmatch[^"]*">(.*?)</li>', text, re.S):
        match = re.search(r"/webcast/ESAKE/(\d+)/", item)
        codes = re.findall(r'<span class="teamname">([^<]*)</span>', item)
        day = re.search(r"(\d{2})/(\d{2})/(\d{4})", item)
        if match and len(codes) == 2:
            when = date(int(day[3]), int(day[2]), int(day[1])) if day else None
            found.append(Listed(int(match.group(1)), codes[0].strip(), codes[1].strip(), when))
    return found


def parse_other_matches(payload: Any) -> list[Listed]:
    return [
        Listed(_int(m["id"]), _str(m.get("team1Name")), _str(m.get("team2Name")))
        for m in (payload or {}).get("othermatches") or []
        if m.get("id")
    ]


@dataclass(frozen=True)
class Marker:
    period: int  # 1..4, then 5, 6, ... for overtimes
    kind: str  # "start" / "end" (a period), "game_end"
    score: tuple[int, int]


def _markers(payload: dict) -> list[Marker]:
    """Period and game markers, oldest first (the feed lists plays newest first)."""
    markers = []
    for play in reversed(payload.get("pbp") or []):
        action, sub = play.get("actionType"), play.get("subType")
        if action not in ("period", "game") or sub not in ("start", "end"):
            continue
        if action == "game" and sub == "start":
            continue
        period = _int(play.get("period"))
        if play.get("periodType") == "OVERTIME":
            period += REGULATION_PERIODS
        score = (_int(play.get("s1")), _int(play.get("s2")))
        markers.append(Marker(period, "game_end" if action == "game" else sub, score))
    return markers


def _period_scores(markers: list[Marker], current: tuple[int, int]) -> list[tuple[int, int]]:
    """Points per begun period, from the running score at each period's end (or now)."""
    ends = {m.period: m.score for m in markers if m.kind == "end"}
    begun = max((m.period for m in markers if m.kind == "start"), default=0)
    scores, prev = [], (0, 0)
    for period in range(1, begun + 1):
        cum = ends.get(period, current)
        scores.append((max(0, cum[0] - prev[0]), max(0, cum[1] - prev[1])))
        prev = cum
    return scores


def _current(payload: dict) -> tuple[int, int]:
    teams = payload.get("tm") or {}
    return _int((teams.get("1") or {}).get("score")), _int((teams.get("2") or {}).get("score"))


def parse_period_state(payload: Any) -> PeriodState:
    if not payload:
        return PeriodState(ended_periods=0, game_over=False, current_period=0)
    markers = _markers(payload)
    begun = max((m.period for m in markers if m.kind == "start"), default=0)
    ended = max((m.period for m in markers if m.kind == "end"), default=0)
    over = next((m for m in markers if m.kind == "game_end"), None)
    last = next((m for m in reversed(markers) if m.kind in ("end", "game_end")), None)
    if over:
        ended = max(ended, begun)
    return PeriodState(
        ended_periods=ended,
        game_over=over is not None,
        current_period=begun,
        score=last.score if last else None,
    )


def parse_header(payload: Any) -> LiveHeader | None:
    """Like EuroLeague's Header: overtimes lumped into one entry, no quarter during a break."""
    if not payload:
        return None
    markers = _markers(payload)
    if not any(m.kind == "start" for m in markers):
        return None  # set up, not started
    over = any(m.kind == "game_end" for m in markers)
    last = markers[-1]
    playing = last.kind == "start" and not over
    scores = _period_scores(markers, _current(payload))
    if len(scores) > REGULATION_PERIODS:
        extra = scores[REGULATION_PERIODS:]
        scores = [*scores[:REGULATION_PERIODS], (sum(h for h, _ in extra), sum(a for _, a in extra))]
    home, away = _current(payload)
    return LiveHeader(
        live=not over,
        home_score=home,
        away_score=away,
        quarter=last.period if playing else None,
        remaining=_str(payload.get("clock")) or None if playing else None,
        quarter_scores=scores,
    )


def _player(raw: dict, team_code: str) -> PlayerLine:
    last = _str(raw.get("internationalFamilyName")) or _str(raw.get("familyName"))
    first = _str(raw.get("internationalFirstName")) or _str(raw.get("firstName"))
    minutes = _str(raw.get("sMinutes"))
    return PlayerLine(
        name=f"{last}, {first}" if first else last,
        team_code=team_code,
        minutes=minutes if minutes and minutes not in ("0:00", "00:00") else "DNP",
        points=_int(raw.get("sPoints")),
        rebounds=_int(raw.get("sReboundsTotal")),
        assists=_int(raw.get("sAssists")),
        steals=_int(raw.get("sSteals")),
        turnovers=_int(raw.get("sTurnovers")),
        blocks=_int(raw.get("sBlocks")),
        fouls=_int(raw.get("sFoulsPersonal")),
        pir=_int(raw.get("eff_5")),  # eff_5 is the PIR ("efficiency") formula
        fg2m=_int(raw.get("sTwoPointersMade")),
        fg2a=_int(raw.get("sTwoPointersAttempted")),
        fg3m=_int(raw.get("sThreePointersMade")),
        fg3a=_int(raw.get("sThreePointersAttempted")),
        ftm=_int(raw.get("sFreeThrowsMade")),
        fta=_int(raw.get("sFreeThrowsAttempted")),
        plus_minus=raw.get("sPlusMinusPoints"),
        starter=bool(raw.get("starter")),
    )


def _team(raw: dict, code: str) -> TeamLine:
    def tot(key: str) -> int:
        return _int(raw.get(f"tot_s{key}"))

    return TeamLine(
        code=code,
        name=_str(raw.get("name")),
        points=tot("Points"),
        fg2m=tot("TwoPointersMade"),
        fg2a=tot("TwoPointersAttempted"),
        fg3m=tot("ThreePointersMade"),
        fg3a=tot("ThreePointersAttempted"),
        ftm=tot("FreeThrowsMade"),
        fta=tot("FreeThrowsAttempted"),
        rebounds=tot("ReboundsTotal"),
        off_reb=tot("ReboundsOffensive"),
        def_reb=tot("ReboundsDefensive"),
        assists=tot("Assists"),
        steals=tot("Steals"),
        turnovers=tot("Turnovers"),
        blocks=tot("Blocks"),
        fouls=tot("FoulsPersonal"),
        pir=_int(raw.get("tot_eff_5")),
    )


def parse_boxscore(payload: Any, home: str = "", away: str = "") -> BoxScore | None:
    """Box score (team 1 = home). ``home``/``away`` are the clubs' codes, so player lines match
    the game's clubs; they default to the feed's own codes."""
    if not payload or not payload.get("tm"):
        return None
    sides = []
    for key, code in (("1", home), ("2", away)):
        raw = payload["tm"].get(key) or {}
        code = code or _str(raw.get("code"))
        players = [_player(p, code) for p in (raw.get("pl") or {}).values()]
        sides.append((_team(raw, code), players))
    (home_line, home_players), (away_line, away_players) = sides
    markers = _markers(payload)
    return BoxScore(
        home=home_line,
        away=away_line,
        home_players=home_players,
        away_players=away_players,
        quarter_scores=_period_scores(markers, _current(payload)),
        live=not any(m.kind == "game_end" for m in markers),
    )


@dataclass
class _Feed:
    """One match's parsed feed (the raw JSON is large, so only the models are kept)."""

    fetched: float
    etag: str | None
    header: LiveHeader | None
    state: PeriodState
    box: BoxScore | None
    others: list[Listed] = field(default_factory=list)


class GblClient(EuroleagueClient):
    """GBL client with the EuroLeague client's interface; reuses its HTTP plumbing and YouTube feed.

    ``code`` arguments are esake game ids; the FIBA match id behind each is discovered as needed.
    """

    def __init__(
        self,
        season_year: int,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff: float = BACKOFF,
        monotonic: Callable[[], float] = clock.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        super().__init__(transport, backoff)
        self.season_year = season_year
        self._monotonic = monotonic
        self._now = now
        self._games: dict[int, Game] = {}  # esake code -> game, from the latest schedule
        self._matches: dict[int, int] = {}  # esake code -> FIBA match id
        self._feeds: dict[int, _Feed] = {}  # FIBA match id -> latest parsed feed
        self._locks: dict[int, asyncio.Lock] = {}
        self._discovered_at = float("-inf")

    async def _text(self, url: str, params: dict[str, Any] | None = None) -> str:
        response = await self._fetch(url, params)
        if not response.text.strip():
            raise ApiError(f"empty response from {response.url}")
        return response.text

    # --- schedule ------------------------------------------------------------------------------

    async def games(self, competition: str, season: str) -> list[Game]:
        """The current round, the one before and the next two: today, last night, and the whole
        coming week even when it has a midweek round (weekly schedules, ADR-025)."""
        page = await self._text(RESULTS_URL)
        current, rounds = parse_rounds(page)
        games = {g.code: g for g in parse_round(page, self.season_year)}
        if current is not None:
            for round_ in (r for r in (current - 1, current + 1, current + 2) if r in rounds):
                other = await self._text(RESULTS_URL, {"series": f"{round_:02d}"})
                games |= {g.code: g for g in parse_round(other, self.season_year)}
        self._games = games
        # Forget games that left the schedule window, so the caches don't grow all season.
        self._matches = {code: m for code, m in self._matches.items() if code in games}
        live = set(self._matches.values())
        self._feeds = {m: f for m, f in self._feeds.items() if m in live}
        self._locks = {m: lock for m, lock in self._locks.items() if m in live}
        try:
            await self._discover(force=True)
        except Exception:  # a side lookup must not cost the schedule
            log.exception("GBL match lookup failed")
        return list(games.values())

    async def clubs(self, competition: str, season: str) -> list[Club]:
        return clubs()

    # --- live ----------------------------------------------------------------------------------

    async def header(self, season: str, code: int) -> LiveHeader | None:
        feed = await self._feed(code)
        return feed.header if feed else None

    async def period_state(self, season: str, code: int) -> PeriodState:
        feed = await self._feed(code)
        return feed.state if feed else PeriodState(ended_periods=0, game_over=False, current_period=0)

    async def boxscore(self, season: str, code: int) -> BoxScore | None:
        feed = await self._feed(code)
        return feed.box if feed else None

    async def _feed(self, code: int) -> _Feed | None:
        """The game's parsed feed, or ``None`` while its FIBA match is unknown or not set up yet."""
        match = self._matches.get(code)
        if match is None:
            await self._discover()
            match = self._matches.get(code)
        return await self._load(match) if match is not None else None

    async def _load(self, match: int) -> _Feed | None:
        """The match's parsed feed; ``None`` while the league hasn't set it up (403)."""
        async with self._locks.setdefault(match, asyncio.Lock()):
            cached = self._feeds.get(match)
            now = self._monotonic()
            if cached and now - cached.fetched < FEED_TTL:
                return cached
            headers = {"If-None-Match": cached.etag} if cached and cached.etag else None
            try:
                response = await self._fetch(FEED_URL.format(match=match), headers=headers)
            except ApiError as exc:
                if exc.status == 403:
                    return None
                raise
            if response.status_code == 304 and cached:
                cached.fetched = now
                return cached
            try:
                payload = response.json()
            except json.JSONDecodeError as exc:
                raise ApiError(f"invalid JSON from {response.url}") from exc
            # Player lines carry the game's club codes, whoever asked for the feed.
            game = next((self._games.get(c) for c, m in self._matches.items() if m == match), None)
            home, away = (game.home.code, game.away.code) if game else ("", "")
            feed = _Feed(
                fetched=now,
                etag=response.headers.get("ETag"),
                header=parse_header(payload),
                state=parse_period_state(payload),
                box=parse_boxscore(payload, home, away),
                others=parse_other_matches(payload),
            )
            self._feeds[match] = feed
            return feed

    # --- match ids -----------------------------------------------------------------------------

    def _unmatched(self, now: datetime) -> list[Game]:
        """Games with no FIBA match yet, from those the nightly results still need to tomorrow's."""
        return [
            g
            for code, g in self._games.items()
            if code not in self._matches and now - LOOKBACK < g.tipoff < now + timedelta(days=1)
        ]

    async def _discover(self, force: bool = False) -> None:
        """Match games to FIBA matches from the ticker, then from known matches' ``othermatches``.

        Rate-limited unless ``force``d, since games before their set-up keep asking."""
        now = self._monotonic()
        if not force and now - self._discovered_at < DISCOVERY_INTERVAL:
            return
        self._discovered_at = now
        wall = self._now()
        if not self._unmatched(wall):
            return
        try:
            self._assign(parse_ticker(await self._text(TICKER_URL)), wall)
        except ApiError as exc:
            log.warning("GBL ticker unavailable: %s", exc)
        known = sorted(
            (
                m
                for code, m in self._matches.items()
                if code in self._games and abs(self._games[code].tipoff - wall) < MATCH_WINDOW
            ),
            reverse=True,
        )
        for match in known:  # newest first; a match not set up yet (403) can't tell
            if not self._unmatched(wall):
                return
            try:
                feed = await self._load(match)
            except ApiError as exc:
                log.warning("GBL feed %d unavailable for match lookup: %s", match, exc)
                continue
            if feed is not None:
                self._assign(feed.others, wall)
                break
        if unmatched := self._unmatched(wall):
            log.info("GBL: no FIBA match known yet for %d game(s)", len(unmatched))

    def _assign(self, listed: list[Listed], now: datetime) -> None:
        """Give each listed match to its game. Without a date, the same pairing twice within a few
        days (playoffs) goes in order: the lower match id to the earlier game."""
        taken = set(self._matches.values())
        for item in sorted(listed, key=lambda i: i.match):
            if item.match in taken:
                continue
            candidates = [
                g
                for code, g in self._games.items()
                if code not in self._matches
                and (g.home.code, g.away.code) == (item.home, item.away)
                and (item.day is None or g.tipoff.astimezone(ATHENS).date() == item.day)
                and abs(g.tipoff - now) < MATCH_WINDOW
            ]
            if candidates:
                game = min(candidates, key=lambda g: g.tipoff)
                self._matches[game.code] = item.match
                taken.add(item.match)
                log.info("GBL %s: FIBA match %d (%s-%s)", game.identifier, item.match, item.home, item.away)


def game_url(game: Game) -> str:
    return GAME_URL.format(code=game.code)
