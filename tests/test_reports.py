import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from euroleague_notifier.api import parse_boxscore, parse_games
from euroleague_notifier.models import Club, Game
from euroleague_notifier.reports import (
    DASH,
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
    return [len(line) for block in re.findall(r"<pre>(.*?)</pre>", html, re.S) for line in block.splitlines()]


def test_quarter_report_uses_end_of_period_score():
    game = games()[31]
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    text = quarter_report(game, box, 2)
    assert text.startswith(f"🏀 <b>End of Q2</b> · Paris 43{DASH}46 LDLC ASVEL")
    assert "Top scorers" in text
    assert "N. Hifi (PRS) <b>30</b>" in text
    table = text.split("Team stats")[1].split("<pre>")[1].split("</pre>")[0].splitlines()
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
    assert game_url(games()[31], "U") is None


def test_follow_model_messages():
    from euroleague_notifier.reports import daily_schedule, final_score, schedule_label, tipoff, title

    game, box = games()[31], parse_boxscore(load("live_Boxscore_E2026_31.json"))
    assert title(game) == "Paris vs LDLC ASVEL"
    assert "Tip-off" in tipoff(game)
    result = final_score(game, box)
    assert "96" in result and "98" in result and "<b>LDLC ASVEL</b>" in result and "Team stats" not in result
    assert schedule_label(game, False).startswith("☆ {{hm:") and schedule_label(game, True).startswith("⭐")
    assert "Round" in daily_schedule([game]) and "{{" not in daily_schedule([game])


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
