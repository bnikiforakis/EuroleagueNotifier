import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from euroleague_notifier.api import parse_boxscore, parse_games
from euroleague_notifier.models import Club, Game
from euroleague_notifier.reports import (
    DASH,
    daily_results,
    daily_schedule,
    final_report,
    game_url,
    iso_z,
    quarter_report,
    reminder,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def games() -> dict[int, Game]:
    return {g.code: g for g in parse_games(load("v2_games_E2026.json"))}


def double_ot_game() -> Game:
    return Game(
        season="E2025",
        code=340,
        round=34,
        tipoff=datetime(2026, 3, 27, 19, 30, tzinfo=UTC),
        home=Club("PAR", "Partizan Mozzart Bet Belgrade", "Partizan"),
        away=Club("PAM", "Valencia Basket", "Valencia"),
        played=True,
        home_score=110,
        away_score=104,
        venue="BELGRADE ARENA",
    )


def pre_widths(html: str) -> list[int]:
    blocks = re.findall(r"<pre><code[^>]*>(.*?)</code></pre>", html, re.S)
    return [len(line) for block in blocks for line in block.splitlines()]


def test_quarter_report_uses_end_of_period_score():
    game = games()[31]
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    text = quarter_report(game, box, 2)
    assert text.startswith(f"🏀 <b>End of Q2</b> · Paris 43{DASH}46 LDLC ASVEL")
    assert "Top scorers" in text
    assert "N. Hifi (Paris) <b>30</b>" in text
    table = text.split('<code class="language-Team stats">')[1].split("</code></pre>")[0].splitlines()
    assert [row.split()[0] for row in table[1:]] == ["FG", "2P", "3P", "FT", "REB", "AST", "TO"]
    assert "Q3" not in text
    assert max(pre_widths(text)) <= 32


def test_quarter_report_headline_prefers_play_by_play_score():
    # A box score fetched a little after the buzzer can already be ahead of the end-of-period score.
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    text = quarter_report(games()[31], box, 2, score=(44, 45))
    assert text.startswith(f"🏀 <b>End of Q2</b> · Paris 44{DASH}45 LDLC ASVEL")


def test_iso_z_converts_to_utc():
    athens = datetime(2026, 10, 8, 19, 0, tzinfo=timezone(timedelta(hours=3)))
    assert iso_z(athens) == "2026-10-08T16:00:00Z"


def test_quarter_report_overtime_label():
    box = parse_boxscore(load("live_Boxscore_E2025_340.json"))
    text = quarter_report(double_ot_game(), box, 6)
    assert "End of OT2" in text
    assert "OT1" in text
    assert f"Partizan 110{DASH}104 Valencia" in text
    assert max(pre_widths(text)) <= 32


def test_final_report_regulation():
    game = games()[31]
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    text = final_report(game, box)
    assert text.startswith(f"🏁 <b>FINAL</b> · Paris 96{DASH}98 <b>LDLC ASVEL</b>")
    assert "(OT)" not in text
    for label in ("2P", "3P", "FT", "REB", "AST", "STL", "TO", "PIR"):
        assert label in text
    assert "T. Waters: 19 PTS, 2 REB, 3 AST, PIR 25" in text


def test_final_report_double_overtime():
    box = parse_boxscore(load("live_Boxscore_E2025_340.json"))
    text = final_report(double_ot_game(), box)
    assert text.startswith(f"🏁 <b>FINAL</b> (2OT) · <b>Partizan</b> 110{DASH}104 Valencia")
    assert "OT2" in text
    assert "<a href" not in text


def test_names_are_escaped():
    game = games()[31]
    evil = replace(game, home=Club("PRS", "A<b>", "Paris <script>"))
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    for text in (quarter_report(evil, box, 1), final_report(evil, box), reminder(evil, 30)):
        assert "<script>" not in text
        assert "Paris &lt;script&gt;" in text


def test_reminder():
    text = reminder(games()[37], 30)
    assert text.startswith("⏰ Tip-off in 30 min: <b>Dubai</b> vs <b>Crvena Zvezda</b>")
    assert "{{hm:2026-10-08T16:00:00Z}}" in text
    assert "Coca-Cola Arena" in text


def test_game_url():
    assert game_url(games()[31], "E") == (
        "https://www.euroleaguebasketball.net/en/euroleague/game-center/2026-27/"
        "paris-basketball-ldlc-asvel-villeurbanne/E2026/31/"
    )
    assert game_url(double_ot_game(), "E") == (
        "https://www.euroleaguebasketball.net/en/euroleague/game-center/2025-26/"
        "partizan-mozzart-bet-belgrade-valencia-basket/E2025/340/"
    )
    eurocup = replace(games()[31], season="U2026")
    assert game_url(eurocup, "U") == (
        "https://www.euroleaguebasketball.net/en/eurocup/game-center/2026-27/"
        "paris-basketball-ldlc-asvel-villeurbanne/U2026/31/"
    )
    assert game_url(games()[31], "X") is None


def test_follow_model_messages():
    from euroleague_notifier.reports import daily_schedule, final_score, schedule_label, tipoff, title

    game, box = games()[31], parse_boxscore(load("live_Boxscore_E2026_31.json"))
    assert title(game) == "Paris vs LDLC ASVEL"
    assert "Tip-off" in tipoff(game)
    result = final_score(game, box)
    assert "96" in result and "98" in result and "<b>LDLC ASVEL</b>" in result and "Team stats" not in result
    assert schedule_label(game, False).startswith("☆ {{hm:") and schedule_label(game, True).startswith("⭐")
    text = daily_schedule([game], "EuroLeague")
    assert "Round" in text and "{{" not in text
    from datetime import time

    assert "All results arrive at 01:00." in daily_schedule([game], "EuroLeague", time(1, 0))


def test_shooting_shows_made_attempted_and_percentage():
    from euroleague_notifier.reports import shooting

    assert shooting(7, 9) == "7/9 (77.8%)"
    assert shooting(0, 0) == "0/0 (-)"
    assert shooting(35, 70) == "35/70 (50.0%)"
    assert shooting(1, 8) == "1/8 (12.5%)" and shooting(1, 200) == "1/200 (0.5%)"


def test_team_table_shows_made_attempted_for_every_shot_type():
    game = games()[31]
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    text = final_report(game, box)
    home, away = box.home, box.away
    for label, (hm, ha), (am, aa) in (
        (
            "FG",
            (home.fg2m + home.fg3m, home.fg2a + home.fg3a),
            (away.fg2m + away.fg3m, away.fg2a + away.fg3a),
        ),
        ("2P", (home.fg2m, home.fg2a), (away.fg2m, away.fg2a)),
        ("3P", (home.fg3m, home.fg3a), (away.fg3m, away.fg3a)),
        ("FT", (home.ftm, home.fta), (away.ftm, away.fta)),
    ):
        row = next(line for line in text.splitlines() if line.startswith(label + " "))
        assert f"{hm}/{ha}" in row and f"{am}/{aa}" in row
    assert max(pre_widths(text)) <= 32


def test_compact_cards_hold_score_and_periods_but_no_tables():
    from euroleague_notifier.reports import final_card, quarter_card

    game = games()[31]
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    card = quarter_card(game, box, 2)
    assert card == f"🏀 <b>End of Q2</b> · Paris 43{DASH}46 LDLC ASVEL\nQ1 21{DASH}14 · Q2 22{DASH}32"
    final = final_card(game, box)
    assert final.startswith("🏁 <b>Final</b> · Paris 96") and "Q4 19" in final
    assert "<pre>" not in card + final


def test_tables_are_labelled_boxes():
    game = games()[31]
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    for text in (quarter_report(game, box, 2), final_report(game, box)):
        assert '<pre><code class="language-Score by quarter">' in text
        assert '<pre><code class="language-Team stats">' in text
        assert "<b>Team stats</b>" not in text  # the box label replaces the old heading
        assert text.count("<pre>") == text.count("</code></pre>") == 2


def test_reports_use_team_names_not_club_codes():
    game, box = games()[31], parse_boxscore(load("live_Boxscore_E2026_31.json"))
    two_ot_game, two_ot = double_ot_game(), parse_boxscore(load("live_Boxscore_E2025_340.json"))
    for g, b in ((game, box), (two_ot_game, two_ot)):
        text = final_report(g, b) + quarter_report(g, b, 2)
        for code in (g.home.code, g.away.code):
            assert not re.search(rf"\b{code}\b", text), code
        assert g.home.short_name in text and g.away.short_name in text
        assert max(pre_widths(text)) <= 32


def test_headers_use_the_competition_name():
    from datetime import date

    game = games()[31]
    assert "Today's EuroCup games" in daily_schedule([game], "EuroCup")
    assert "EuroCup results</b>" in daily_results([game], date(2026, 10, 8), "EuroCup", "eurocup")
    missing = daily_results([game], date(2026, 10, 8), "EuroCup", "eurocup", missing=2)
    assert "see /eurocup" in missing and "/score" not in missing
