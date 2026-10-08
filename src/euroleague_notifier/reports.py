"""Telegram-HTML message builders for schedules, quarter and final reports.

Times are emitted as ``{{time:<ISO Z>}}`` / ``{{hm:<ISO Z>}}`` placeholders that PiButler renders in
each user's timezone. Tables use ``<pre>`` and stay within ~32 characters for phone screens.
"""

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC
from html import escape
from itertools import groupby

from euroleague_notifier.models import (
    REGULATION_PERIODS,
    BoxScore,
    Game,
    PlayerLine,
    TeamLine,
    period_label,
)

SITE = "https://www.euroleaguebasketball.net/en"
COMPETITION_PATHS = {"E": "euroleague"}
DASH = "\N{EN DASH}"


def iso_z(game: Game) -> str:
    return game.tipoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def game_url(game: Game, competition: str) -> str | None:
    """Official game-center page, e.g. ``.../euroleague/game-center/2026-27/<home>-<away>/E2026/31/``.

    Slugs are the clubs' full names lowercased and hyphenated. Only the EuroLeague pattern is
    confirmed; other competitions return ``None``.
    """
    path = COMPETITION_PATHS.get(competition)
    year = re.search(r"\d{4}", game.season)
    if not path or not year:
        return None
    start = int(year.group())
    alias = f"{start}-{(start + 1) % 100:02d}"
    teams = f"{_slug(game.home.name)}-{_slug(game.away.name)}"
    return f"{SITE}/{path}/game-center/{alias}/{teams}/{game.season}/{game.code}/"


def _names(game: Game) -> tuple[str, str]:
    return escape(game.home.short_name), escape(game.away.short_name)


def _ot_suffix(periods: int) -> str:
    extra = periods - REGULATION_PERIODS
    if extra <= 0:
        return ""
    return " (OT)" if extra == 1 else f" ({extra}OT)"


def _period_table(game: Game, scores: list[tuple[int, int]]) -> str:
    head = "".join(f"{period_label(i):>4}" for i in range(1, len(scores) + 1))
    home = "".join(f"{h:>4}" for h, _ in scores)
    away = "".join(f"{a:>4}" for _, a in scores)
    total_h, total_a = sum(h for h, _ in scores), sum(a for _, a in scores)
    rows = [
        f"{'':<3}{head}{'T':>5}",
        f"{escape(game.home.code):<3}{home}{total_h:>5}",
        f"{escape(game.away.code):<3}{away}{total_a:>5}",
    ]
    return "<pre>" + "\n".join(rows) + "</pre>"


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


StatRow = tuple[str, Callable[[TeamLine], str]]

QUARTER_STATS: list[StatRow] = [
    ("FG%", lambda t: _fmt_pct(t.fg_pct)),
    ("3P%", lambda t: _fmt_pct(t.fg3_pct)),
    ("REB", lambda t: str(t.rebounds)),
    ("AST", lambda t: str(t.assists)),
    ("TO", lambda t: str(t.turnovers)),
]
FINAL_STATS: list[StatRow] = [
    *QUARTER_STATS[:2],
    ("FT%", lambda t: _fmt_pct(t.ft_pct)),
    *QUARTER_STATS[2:4],
    ("STL", lambda t: str(t.steals)),
    QUARTER_STATS[4],
    ("PIR", lambda t: str(t.pir)),
]


def _team_table(game: Game, box: BoxScore, stats: list[StatRow]) -> str:
    rows = [f"{'':<5}{escape(game.home.code):>7}{escape(game.away.code):>7}"]
    rows += [f"{label:<5}{fmt(box.home):>7}{fmt(box.away):>7}" for label, fmt in stats]
    return "<pre>" + "\n".join(rows) + "</pre>"


def _player(player: PlayerLine) -> str:
    return escape(player.short_name)


def _top_scorers(box: BoxScore, limit: int = 3) -> list[str]:
    ranked = sorted(box.players, key=lambda p: (-p.points, -p.pir))[:limit]
    return [
        f"{i}. {_player(p)} ({escape(p.team_code)}) <b>{p.points}</b>" for i, p in enumerate(ranked, start=1)
    ]


def _top_performers(players: list[PlayerLine], limit: int = 3) -> list[str]:
    ranked = sorted(players, key=lambda p: (-p.pir, -p.points))[:limit]
    return [f"• {_player(p)}: {p.points} PTS, {p.rebounds} REB, {p.assists} AST, PIR {p.pir}" for p in ranked]


def _score_line(game: Game, home_score: int, away_score: int, bold_winner: bool = False) -> str:
    home, away = _names(game)
    if bold_winner and home_score != away_score:
        if home_score > away_score:
            home = f"<b>{home}</b>"
        else:
            away = f"<b>{away}</b>"
    return f"{home} {home_score}{DASH}{away_score} {away}"


def quarter_report(game: Game, box: BoxScore, period: int) -> str:
    """Report sent when ``period`` (1-based; 5+ are overtimes) has ended.

    The headline score is the sum of the first ``period`` period scores, so a box score fetched a
    little after the buzzer still reports the end-of-period score; team stats are game-to-date.
    """
    scores = box.quarter_scores[:period]
    home_score, away_score = box.home.points, box.away.points
    if len(scores) == period:
        home_score, away_score = sum(h for h, _ in scores), sum(a for _, a in scores)
    lines = [
        f"🏀 <b>End of {period_label(period)}</b> · {_score_line(game, home_score, away_score)}",
        "",
        _period_table(game, scores),
        "🔥 <b>Top scorers</b>",
        *_top_scorers(box),
        "",
        "📊 <b>Team stats</b>",
        _team_table(game, box, QUARTER_STATS),
    ]
    return "\n".join(lines)


def final_report(game: Game, box: BoxScore, url: str | None = None) -> str:
    """Full-game summary: result, period scores, team stats and top performers by PIR."""
    score = _score_line(game, box.home.points, box.away.points, bold_winner=True)
    home, away = _names(game)
    lines = [
        f"🏁 <b>FINAL</b>{_ot_suffix(len(box.quarter_scores))} · {score}",
        "",
        _period_table(game, box.quarter_scores),
        "📊 <b>Team stats</b>",
        _team_table(game, box, FINAL_STATS),
        f"⭐ <b>{home}</b>",
        *_top_performers(box.home_players),
        "",
        f"⭐ <b>{away}</b>",
        *_top_performers(box.away_players),
    ]
    if url:
        lines += ["", f'<a href="{escape(url)}">Game center</a>']
    return "\n".join(lines)


def reminder(game: Game, minutes: int) -> str:
    home, away = _names(game)
    parts = [f"⏰ Tip-off in {minutes} min: <b>{home}</b> vs <b>{away}</b>", f"{{{{hm:{iso_z(game)}}}}}"]
    if game.venue:
        parts.append(escape(game.venue.title()))
    return " · ".join(parts)


def _digest_line(game: Game, time_format: str) -> str:
    home, away = _names(game)
    if game.played and game.home_score is not None and game.away_score is not None:
        return f"✅ {_score_line(game, game.home_score, game.away_score, bold_winner=True)}"
    return f"🕒 {{{{{time_format}:{iso_z(game)}}}}} {home} vs {away}"


def schedule_digest(games: list[Game], title: str, time_format: str = "time") -> str:
    """Games sorted by tip-off and grouped by round.

    ``time_format`` is the PiButler placeholder: "time" (day + time) or "hm" (time only, for a
    single day's games).
    """
    lines = [f"📅 <b>{escape(title, quote=False)}</b>"]
    if not games:
        return "\n".join([*lines, "", "No games scheduled."])
    ordered = sorted(games, key=lambda g: (g.round or 0, g.tipoff, g.code))
    for round_number, group in groupby(ordered, key=lambda g: g.round):
        lines.append("")
        if round_number is not None:
            lines.append(f"<b>Round {round_number}</b>")
        lines += [_digest_line(g, time_format) for g in group]
    return "\n".join(lines)


def title(game: Game) -> str:
    """Plain-text game name for button labels and topic titles (not HTML)."""
    return f"{game.home.short_name} vs {game.away.short_name}"


def tipoff(game: Game) -> str:
    home, away = _names(game)
    return f"🏀 <b>Tip-off!</b> {home} vs {away}"


def final_score(game: Game, box: BoxScore) -> str:
    """One-line result for people who didn't follow the game."""
    score = _score_line(game, box.home.points, box.away.points, bold_winner=True)
    return f"🏁 <b>Final</b>{_ot_suffix(len(box.quarter_scores))} · {score}"


def daily_schedule(games: list[Game]) -> str:
    """Header of the daily schedule. The games themselves are its Follow buttons."""
    rounds = sorted({g.round for g in games if g.round is not None})
    round_text = f" · Round {rounds[0]}" if len(rounds) == 1 else ""
    return (
        f"📅 <b>Today's EuroLeague games</b>{round_text}\n\n"
        "Tap a game to follow it: a reminder before tip-off, stats after every quarter and the "
        "full box score at the end. For the rest you'll just get the start and the final score."
    )


def schedule_label(game: Game, following: bool) -> str:
    star = "⭐" if following else "☆"
    return f"{star} {{{{hm:{iso_z(game)}}}}} {game.home.short_name} {DASH} {game.away.short_name}"
