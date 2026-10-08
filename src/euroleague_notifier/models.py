"""Domain models for EuroLeague schedules, live state and box scores."""

from dataclasses import dataclass, field
from datetime import datetime

REGULATION_PERIODS = 4


def period_label(period: int) -> str:
    """Human label for a 1-based period number: Q1..Q4, then OT1, OT2, ..."""
    return f"Q{period}" if period <= REGULATION_PERIODS else f"OT{period - REGULATION_PERIODS}"


def pct(made: int, attempted: int) -> float | None:
    return made / attempted * 100 if attempted else None


@dataclass(frozen=True)
class Club:
    code: str
    name: str
    short_name: str


@dataclass(frozen=True)
class Game:
    season: str
    code: int
    round: int | None
    tipoff: datetime
    home: Club
    away: Club
    played: bool
    home_score: int | None
    away_score: int | None
    venue: str | None

    @property
    def identifier(self) -> str:
        return f"{self.season}_{self.code}"


@dataclass(frozen=True)
class LiveHeader:
    """Live scoreboard. ``quarter_scores`` holds per-period points as (home, away)."""

    live: bool
    home_score: int
    away_score: int
    quarter: int | None
    remaining: str | None
    quarter_scores: list[tuple[int, int]] = field(default_factory=list)


@dataclass(frozen=True)
class PeriodState:
    """Period progress derived from play-by-play markers.

    ``score`` is the running (home, away) score at the latest ``EP``/``EG`` marker, if any.
    """

    ended_periods: int
    game_over: bool
    current_period: int
    score: tuple[int, int] | None = None


@dataclass(frozen=True)
class PlayerLine:
    name: str
    team_code: str
    minutes: str
    points: int
    rebounds: int
    assists: int
    steals: int
    turnovers: int
    blocks: int
    fouls: int
    pir: int
    fg2m: int
    fg2a: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    plus_minus: int | None = None
    starter: bool = False

    @property
    def played(self) -> bool:
        return ":" in self.minutes

    @property
    def short_name(self) -> str:
        """``"WARREN, TJ"`` -> ``"T. Warren"``."""
        last, _, first = self.name.partition(",")
        last, first = last.strip().title(), first.strip()
        return f"{first[0].upper()}. {last}" if first else last


@dataclass(frozen=True)
class TeamLine:
    code: str
    name: str
    points: int
    fg2m: int
    fg2a: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    rebounds: int
    off_reb: int
    def_reb: int
    assists: int
    steals: int
    turnovers: int
    blocks: int
    fouls: int
    pir: int

    @property
    def fg_pct(self) -> float | None:
        return pct(self.fg2m + self.fg3m, self.fg2a + self.fg3a)

    @property
    def fg3_pct(self) -> float | None:
        return pct(self.fg3m, self.fg3a)

    @property
    def ft_pct(self) -> float | None:
        return pct(self.ftm, self.fta)


@dataclass(frozen=True)
class BoxScore:
    home: TeamLine
    away: TeamLine
    home_players: list[PlayerLine]
    away_players: list[PlayerLine]
    quarter_scores: list[tuple[int, int]]
    live: bool = False

    @property
    def players(self) -> list[PlayerLine]:
        return self.home_players + self.away_players
