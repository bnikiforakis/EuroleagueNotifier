"""Weekly team schedules: one silent message per followed team, Monday morning (ADR-025).

Each message lists the team's games from Monday to Sunday in every competition it plays:

    📅 Panathinaikos weekly schedule
    Tue 27 Oct, 21:00 · vs Olimpia Milano (H) · EuroLeague
    Sun 1 Nov, 19:00 · vs Promitheas (A) · GBL

A club that plays the GBL too (it has a ``link``) is announced by the GBL service only, which adds
its EuroLeague/EuroCup games; every other club by its own competition's service. So nobody gets a
team twice, and the message reaches everyone following the team in any competition (ADR-024).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

from euroleague_notifier.models import Club, Game
from euroleague_notifier.reports import iso_z

COMPETITION_NAMES = {"E": "EuroLeague", "U": "EuroCup", "G": "GBL"}
LINK_OWNER = "G"  # the domestic league announces linked clubs


@dataclass(frozen=True)
class Fixture:
    competition: str
    game: Game
    home: bool  # the team plays at home

    @property
    def opponent(self) -> Club:
        return self.game.away if self.home else self.game.home


def week_of(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Monday 00:00 to the next Monday 00:00 (exclusive) of ``day``'s week, in ``tz``."""
    monday = day - timedelta(days=day.weekday())
    start = datetime.combine(monday, time(0), tz)
    return start, datetime.combine(monday + timedelta(days=7), time(0), tz)


def announces(club: Club, competitions: tuple[str, ...]) -> bool:
    """Whether this service sends the club's weekly schedule (see the module docstring)."""
    return club.link is None or LINK_OWNER in competitions


def team_weeks(
    own: Iterable[tuple[str, Game]],
    partners: Iterable[tuple[str, Game]],
    clubs: Iterable[Club],
    competitions: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> dict[str, tuple[Club, list[Fixture]]]:
    """Per club code (in this service's competitions): the club and its fixtures in [start, end).

    ``partners``: other competitions' games; they join one of our ``clubs`` through its link, even
    in a week (or before the season) with no game of ours."""
    weeks: dict[str, tuple[Club, list[Fixture]]] = {}
    by_link = {club.link: club for club in clubs if club.link}
    for comp, game in own:
        for club, home in ((game.home, True), (game.away, False)):
            if start <= game.tipoff < end and announces(club, competitions):
                weeks.setdefault(club.code, (club, []))[1].append(Fixture(comp, game, home))
    for comp, game in partners:
        if not start <= game.tipoff < end:
            continue
        for side, home in ((game.home, True), (game.away, False)):
            club = by_link.get(side.link) if side.link else None
            if club is not None and announces(club, competitions):
                weeks.setdefault(club.code, (club, []))[1].append(Fixture(comp, game, home))
    for _, fixtures in weeks.values():
        fixtures.sort(key=lambda f: (f.game.tipoff, f.competition))
    return weeks


def message(club: Club, fixtures: list[Fixture]) -> str:
    lines = [f"📅 <b>{escape(club.short_name)} weekly schedule</b>", ""]
    for f in fixtures:
        where = "H" if f.home else "A"
        competition = COMPETITION_NAMES.get(f.competition, f.competition)
        lines.append(
            f"{{{{time:{iso_z(f.game.tipoff)}}}}} · vs <b>{escape(f.opponent.short_name)}</b> ({where})"
            f" · {competition}"
        )
    return "\n".join(lines)


def topic(club: Club, monday: date) -> dict:
    """The week's audience: everyone auto-following the club (directly or through its link)."""
    return {
        "id": f"week:{monday.isoformat()}:{club.code}",
        "title": f"{club.short_name} weekly schedule",
        "auto_follow": {"teams": [club.code]},
    }
