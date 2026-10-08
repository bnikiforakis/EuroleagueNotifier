import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from euroleague_notifier.api import parse_boxscore, parse_games
from euroleague_notifier.models import Club, Game
from euroleague_notifier.reports import (
    DASH,
    final_report,
    game_url,
    quarter_report,
    reminder,
    schedule_digest,
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
    assert "FG%" in text and "3P%" in text and "TO" in text
    assert "Q3" not in text
    assert max(pre_widths(text)) <= 32


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
    url = game_url(game, "E")
    text = final_report(game, box, url)
    assert text.startswith(f"🏁 <b>FINAL</b> · Paris 96{DASH}98 <b>LDLC ASVEL</b>")
    assert "(OT)" not in text
    for label in ("FG%", "3P%", "FT%", "REB", "AST", "STL", "TO", "PIR"):
        assert label in text
    assert "T. Waters: 19 PTS, 2 REB, 3 AST, PIR 25" in text
    assert f'<a href="{url}">' in text


def test_final_report_double_overtime():
    box = parse_boxscore(load("live_Boxscore_E2025_340.json"))
    text = final_report(double_ot_game(), box, None)
    assert text.startswith(f"🏁 <b>FINAL</b> (2OT) · <b>Partizan</b> 110{DASH}104 Valencia")
    assert "OT2" in text
    assert "<a href" not in text


def test_names_are_escaped():
    game = games()[31]
    evil = replace(game, home=Club("PRS", "A<b>", "Paris <script>"))
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    for text in (quarter_report(evil, box, 1), final_report(evil, box, None), reminder(evil, 30)):
        assert "<script>" not in text
        assert "Paris &lt;script&gt;" in text


def test_reminder():
    text = reminder(games()[37], 30)
    assert text.startswith("⏰ Tip-off in 30 min: <b>Dubai</b> vs <b>Crvena Zvezda</b>")
    assert "{{hm:2026-10-08T16:00:00Z}}" in text
    assert "Coca-Cola Arena" in text


def test_schedule_digest():
    text = schedule_digest(list(games().values()), "Thursday")
    assert text.startswith("📅 <b>Thursday</b>")
    assert "<b>Round 4</b>" in text
    assert "{{time:2026-10-08T16:00:00Z}} Dubai vs Crvena Zvezda" in text
    assert f"Paris 96{DASH}98 <b>LDLC ASVEL</b>" in text
    times = re.findall(r"\{\{time:([^}]+)\}\}", text)
    assert times == sorted(times)
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", t) for t in times)


def test_schedule_digest_single_day_uses_time_only():
    text = schedule_digest(list(games().values()), "Today's games", time_format="hm")
    assert "{{hm:" in text and "{{time:" not in text
    assert "Today's games" in text  # apostrophe not entity-escaped


def test_schedule_digest_empty():
    assert "No games" in schedule_digest([], "Today")


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
