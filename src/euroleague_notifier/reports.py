"""Telegram-HTML message builders for schedules, quarter and final reports.

Times are emitted as ``{{time:<ISO Z>}}`` / ``{{hm:<ISO Z>}}`` placeholders that PiButler renders in
each user's timezone. Tables use ``<pre>`` and stay within ~32 characters for phone screens.
"""

import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from html import escape

from euroleague_notifier.models import (
    REGULATION_PERIODS,
    BoxScore,
    Game,
    PlayerLine,
    TeamLine,
    pct,
    period_label,
)

SITE = "https://www.euroleaguebasketball.net/en"
COMPETITION_PATHS = {"E": "euroleague", "U": "eurocup"}
DASH = "\N{EN DASH}"


def iso_z(dt: datetime) -> str:
    """UTC ISO timestamp with a ``Z`` suffix, as PiButler placeholders and expiry fields expect."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def game_url(game: Game, competition: str) -> str | None:
    """Official game-center page, e.g. ``.../euroleague/game-center/2026-27/<home>-<away>/E2026/31/``.

    Slugs are the clubs' full names lowercased and hyphenated. EuroCup uses the same pattern under
    ``/eurocup/``; other competitions return ``None``.
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


def ot_suffix(periods: int) -> str:
    """``" (OT)"``, ``" (2OT)"``, ... for a game of ``periods`` periods; empty in regulation."""
    extra = periods - REGULATION_PERIODS
    if extra <= 0:
        return ""
    return " (OT)" if extra == 1 else f" ({extra}OT)"


def _box(label: str, rows: list[str]) -> str:
    """A monospace table. Telegram shows ``label`` in the block's header (instead of "Copy code")."""
    return f'<pre><code class="language-{label}">' + "\n".join(rows) + "</code></pre>"


def _team_header(game: Game) -> str:
    """Column headers with the teams' names (at most 13 characters, so columns stay apart)."""
    home, away = (escape(c.short_name[:13]) for c in (game.home, game.away))
    return f"{'':<3}{home:>14}{away:>14}"


def _period_table(game: Game, scores: list[tuple[int, int]]) -> str:
    # One row per period (overtimes just add rows), same 31-character layout as the team table.
    rows = [_team_header(game)]
    rows += [f"{period_label(i):<3}{h:>14}{a:>14}" for i, (h, a) in enumerate(scores, 1)]
    rows.append(f"{'T':<3}{sum(h for h, _ in scores):>14}{sum(a for _, a in scores):>14}")
    return _box("Score by quarter", rows)


def shooting(made: int, attempted: int) -> str:
    """``7/9 (77.8%)``: made/attempted and the percentage (``0/0 (-)`` when nothing was tried)."""
    value = pct(made, attempted)
    return f"{made}/{attempted} ({'-' if value is None else f'{value:.1f}%'})"


StatRow = tuple[str, Callable[[TeamLine], str]]

QUARTER_STATS: list[StatRow] = [
    ("FG", lambda t: shooting(t.fg2m + t.fg3m, t.fg2a + t.fg3a)),
    ("2P", lambda t: shooting(t.fg2m, t.fg2a)),
    ("3P", lambda t: shooting(t.fg3m, t.fg3a)),
    ("FT", lambda t: shooting(t.ftm, t.fta)),
    ("REB", lambda t: str(t.rebounds)),
    ("AST", lambda t: str(t.assists)),
    ("TO", lambda t: str(t.turnovers)),
]
FINAL_STATS: list[StatRow] = [
    *QUARTER_STATS[:6],
    ("STL", lambda t: str(t.steals)),
    QUARTER_STATS[6],
    ("PIR", lambda t: str(t.pir)),
]


def _team_table(game: Game, box: BoxScore, stats: list[StatRow]) -> str:
    # 3 + 14 + 14 = 31 characters: fits a phone screen with "35/70 (50.0%)" per team.
    rows = [_team_header(game)]
    rows += [f"{label:<3}{fmt(box.home):>14}{fmt(box.away):>14}" for label, fmt in stats]
    return _box("Team stats", rows)


def periods_line(scores: list[tuple[int, int]], name: Callable[[int], str] = period_label) -> str:
    """``Q1 22-18 · Q2 15-17`` (with en dashes)."""
    return " · ".join(f"{name(i)} {h}{DASH}{a}" for i, (h, a) in enumerate(scores, 1))


def _player(player: PlayerLine) -> str:
    return escape(player.short_name)


def _top_scorers(game: Game, box: BoxScore, limit: int = 3) -> list[str]:
    names = {c.code: c.short_name for c in (game.home, game.away)}
    ranked = sorted(box.players, key=lambda p: (-p.points, -p.pir))[:limit]
    return [
        f"{i}. {_player(p)} ({escape(names.get(p.team_code, p.team_code))}) <b>{p.points}</b>"
        for i, p in enumerate(ranked, start=1)
    ]


def _top_performers(players: list[PlayerLine], limit: int = 3) -> list[str]:
    ranked = sorted(players, key=lambda p: (-p.pir, -p.points))[:limit]
    return [f"• {_player(p)}: {p.points} PTS, {p.rebounds} REB, {p.assists} AST, PIR {p.pir}" for p in ranked]


def plain_score(game: Game, home_score: int, away_score: int) -> str:
    """Plain-text score with team names, e.g. ``Panathinaikos 45-41 Fenerbahce`` (en dash)."""
    return f"{game.home.short_name} {home_score}{DASH}{away_score} {game.away.short_name}"


def box_sections(game: Game, box: BoxScore, scores: list[tuple[int, int]], stats: list[StatRow]) -> list[str]:
    """Period table, top scorers and team stats, as message lines."""
    return [
        _period_table(game, scores),
        "🔥 <b>Top scorers</b>",
        *_top_scorers(game, box),
        "",
        _team_table(game, box, stats),  # labelled "Team stats" by its box
    ]


def score_line(
    game: Game, home_score: int, away_score: int, bold_winner: bool = False, bold_both: bool = False
) -> str:
    home, away = _names(game)
    if bold_both:
        home, away = f"<b>{home}</b>", f"<b>{away}</b>"
    elif bold_winner and home_score != away_score:
        if home_score > away_score:
            home = f"<b>{home}</b>"
        else:
            away = f"<b>{away}</b>"
    return f"{home} {home_score}{DASH}{away_score} {away}"


def _period_score(
    box: BoxScore, period: int, score: tuple[int, int] | None
) -> tuple[list[tuple[int, int]], int, int]:
    scores = box.quarter_scores[:period]
    home_score, away_score = box.home.points, box.away.points
    if score is not None:
        home_score, away_score = score
    elif len(scores) == period:
        home_score, away_score = sum(h for h, _ in scores), sum(a for _, a in scores)
    return scores, home_score, away_score


def _period_head(game: Game, period: int, home_score: int, away_score: int) -> str:
    return f"🏀 <b>End of {period_label(period)}</b> · {score_line(game, home_score, away_score)}"


def quarter_card(game: Game, box: BoxScore, period: int, score: tuple[int, int] | None = None) -> str:
    """Compact end-of-period message for followers; the tables sit behind a Stats button."""
    scores, home_score, away_score = _period_score(box, period, score)
    head = _period_head(game, period, home_score, away_score)
    return f"{head}\n{periods_line(scores)}" if scores else head


def final_card(game: Game, box: BoxScore) -> str:
    """Compact final for followers: result and score by period; the tables sit behind Stats."""
    head = final_score(game, box)
    return f"{head}\n{periods_line(box.quarter_scores)}" if box.quarter_scores else head


def quarter_report(game: Game, box: BoxScore, period: int, score: tuple[int, int] | None = None) -> str:
    """Report sent when ``period`` (1-based; 5+ are overtimes) has ended.

    The headline is ``score`` (the play-by-play score at the end of the period) when given, else the
    sum of the first ``period`` period scores, so a box score fetched a little after the buzzer still
    reports the end-of-period score; team stats are game-to-date.
    """
    scores, home_score, away_score = _period_score(box, period, score)
    lines = [
        _period_head(game, period, home_score, away_score),
        "",
        *box_sections(game, box, scores, QUARTER_STATS),
    ]
    return "\n".join(lines)


def final_report(game: Game, box: BoxScore) -> str:
    """Full-game summary: result, period scores, team stats and top performers by PIR."""
    score = score_line(game, box.home.points, box.away.points, bold_winner=True)
    home, away = _names(game)
    lines = [
        f"🏁 <b>FINAL</b>{ot_suffix(len(box.quarter_scores))} · {score}",
        "",
        _period_table(game, box.quarter_scores),
        _team_table(game, box, FINAL_STATS),
        f"⭐ <b>{home}</b>",
        *_top_performers(box.home_players),
        "",
        f"⭐ <b>{away}</b>",
        *_top_performers(box.away_players),
    ]
    return "\n".join(lines)


def matchup(game: Game) -> str:
    """``<b>Home</b> vs <b>Away</b>`` (escaped)."""
    home, away = _names(game)
    return f"<b>{home}</b> vs <b>{away}</b>"


def reminder(game: Game, minutes: int) -> str:
    parts = [
        f"⏰ Tip-off in {minutes} min: {matchup(game)}",
        f"{{{{hm:{iso_z(game.tipoff)}}}}}",
    ]
    if game.venue:
        parts.append(escape(game.venue.title()))
    return " · ".join(parts)


def title(game: Game) -> str:
    """Plain-text game name for button labels and topic titles (not HTML)."""
    return f"{game.home.short_name} vs {game.away.short_name}"


def tipoff(game: Game) -> str:
    home, away = _names(game)
    return f"🏀 <b>Tip-off!</b> {home} vs {away}"


def final_score(game: Game, box: BoxScore) -> str:
    """One-line result for people who didn't follow the game."""
    score = score_line(game, box.home.points, box.away.points, bold_winner=True)
    return f"🏁 <b>Final</b>{ot_suffix(len(box.quarter_scores))} · {score}"


def daily_schedule(games: list[Game], name: str, results_time: time | None = None) -> str:
    """Header of the daily schedule. The games themselves are its Follow buttons."""
    rounds = sorted({g.round for g in games if g.round is not None})
    round_text = f" · Round {rounds[0]}" if len(rounds) == 1 else ""
    results = f" All results arrive at {results_time:%H:%M}." if results_time else ""
    return (
        f"📅 <b>Today's {escape(name)} games</b>{round_text}\n\n"
        "Tap a game to follow it: a reminder before tip-off and a score card after every quarter, "
        f"with 📊 for the full stats.{results}"
    )


def schedule_label(game: Game, following: bool) -> str:
    star = "⭐" if following else "☆"
    return f"{star} {{{{hm:{iso_z(game.tipoff)}}}}} {game.home.short_name} {DASH} {game.away.short_name}"


def daily_results(games: list[Game], day: date, name: str, command: str, missing: int = 0) -> str:
    """Header of the nightly results for the games of ``day``. The games are its 📊 buttons."""
    rounds = sorted({g.round for g in games if g.round is not None})
    round_text = f" · Round {rounds[0]}" if len(rounds) == 1 else ""
    head = f"🏁 <b>{escape(name)} results</b> · {day:%a} {day.day} {day:%b}{round_text}"
    text = f"{head}\n\nTap a game for its box score."
    if missing:
        text += f"\n\n⚠️ {missing} result{'s' if missing > 1 else ''} not available yet: see /{command}."
    return text


def result_label(game: Game, box: BoxScore) -> str:
    """Button label such as ``✅ PAN 88-80 FEN (OT)`` (en dash)."""
    return f"✅ {plain_score(game, box.home.points, box.away.points)}{ot_suffix(len(box.quarter_scores))}"
