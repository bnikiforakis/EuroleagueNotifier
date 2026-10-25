from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_tracker import FakeButler, FakeClock, FakeEuroleague, load

from euroleague_notifier import gbl, weekly
from euroleague_notifier.api import parse_games
from euroleague_notifier.config import Settings
from euroleague_notifier.gbl import parse_round
from euroleague_notifier.models import Club
from euroleague_notifier.store import EventStore
from euroleague_notifier.tracker import Notifier

FIXTURES = Path(__file__).parent / "fixtures"
ATHENS = ZoneInfo("Europe/Athens")
ROUND5 = parse_round((FIXTURES / "esake_round05.html").read_text(), 2026)  # Sat 31 Oct - Sun 1 Nov
START, END = weekly.week_of(date(2026, 10, 28), ATHENS)  # Mon 26 Oct - Mon 2 Nov
PAN = Club("PAN", "Panathinaikos AKTOR Athens", "Panathinaikos", link="gr-panathinaikos")
MIL = Club("MIL", "EA7 Emporio Armani Milan", "Milan")
TEMPLATE = parse_games(load("v2_games_E2026.json"))[0]


def el_game(code: int, tipoff: datetime, home: Club, away: Club):
    return replace(TEMPLATE, code=code, tipoff=tipoff, home=home, away=away, played=False)


EL_WEEK = [
    ("E", el_game(901, datetime(2026, 10, 27, 19, 0, tzinfo=UTC), PAN, MIL)),  # Tue 21:00 Athens
    ("E", el_game(902, datetime(2026, 10, 30, 19, 15, tzinfo=UTC), MIL, PAN)),  # Fri
    ("E", el_game(903, datetime(2026, 11, 3, 19, 0, tzinfo=UTC), PAN, MIL)),  # next week
]


def test_week_of():
    assert datetime(2026, 10, 26, 0, 0, tzinfo=ATHENS) == START
    assert timedelta(days=7) == END - START
    assert weekly.week_of(date(2026, 10, 26), ATHENS)[0] == START  # a Monday is its own week


def test_gbl_service_merges_european_games_of_linked_clubs():
    weeks = weekly.team_weeks([("G", g) for g in ROUND5], EL_WEEK, gbl.clubs(), ("G",), START, END)
    assert len(weeks) == 14  # every GBL club plays once in round 5
    club, fixtures = weeks["PAO"]
    assert club.short_name == "Panathinaikos"
    assert [(f.competition, f.opponent.short_name, f.home) for f in fixtures] == [
        ("E", "Milan", True),
        ("E", "Milan", False),
        ("G", "Aris", False),  # Sun 1 Nov at Aris
    ]
    assert "MIL" not in weeks  # Milan isn't a GBL club: the EuroLeague service sends it


def test_euroleague_service_leaves_linked_clubs_to_the_gbl():
    weeks = weekly.team_weeks(EL_WEEK, [], [], ("E",), START, END)
    assert set(weeks) == {"MIL"}
    assert [f.home for f in weeks["MIL"][1]] == [False, True]


def test_message():
    club, fixtures = weekly.team_weeks([("G", g) for g in ROUND5], EL_WEEK, gbl.clubs(), ("G",), START, END)[
        "PAO"
    ]
    text = weekly.message(club, fixtures)
    assert text.splitlines() == [
        "📅 <b>Panathinaikos weekly schedule</b>",
        "",
        "{{time:2026-10-27T19:00:00Z}} · vs <b>Milan</b> (H) · EuroLeague",
        "{{time:2026-10-30T19:15:00Z}} · vs <b>Milan</b> (A) · EuroLeague",
        "{{time:2026-11-01T15:15:00Z}} · vs <b>Aris</b> (A) · GBL",
    ]


@pytest.fixture
async def events():
    store = await EventStore.open(":memory:")
    yield store
    await store.close()


class FakePartner:
    def __init__(self):
        self.calls: list[str] = []

    async def games(self, competition, season):
        self.calls.append(season)
        return [g for c, g in EL_WEEK if c == competition]


def make_gbl(events, start: datetime):
    clock = FakeClock(start)
    butler = FakeButler()
    settings = Settings(pibutler_url="http://x", pibutler_api_key="k", competitions=("G",))
    partner = FakePartner()
    source = FakeEuroleague([], games=ROUND5)

    async def gbl_clubs(competition, season):
        return gbl.clubs()

    source.clubs = gbl_clubs
    notifier = Notifier(
        settings,
        source,
        butler,
        events,
        clock=clock,
        sleep=clock.sleep,
        partner=partner,
        partner_competitions=("E", "U"),
    )
    return notifier, butler, clock, partner


async def test_sent_on_monday_at_ten(events):
    notifier, butler, clock, partner = make_gbl(events, datetime(2026, 10, 26, 6, 50, tzinfo=UTC))  # 08:50
    await notifier.tick()
    assert not [k for k in butler.keys if k.startswith("weekly")]
    clock.t = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # 10:00 Athens
    await notifier.tick()
    sent = {n["key"]: n for n in butler.sent if n["key"].startswith("weekly")}
    assert len(sent) == 14 and partner.calls == ["E2026", "U2026"]
    pao = sent["weekly:2026-10-26:PAO"]
    assert pao["silent"] and pao["tags"] == {"daily": ["weekly"]}
    assert pao["audience"] == {"topic": "week:2026-10-26:PAO", "following": True}
    assert pao["topics"][0]["auto_follow"] == {"teams": ["PAO"]}
    assert pao["expires_at"] == "2026-10-26T22:00:00Z"  # end of Monday, Athens
    assert "EuroLeague" in pao["text"] and "GBL" in pao["text"]
    await notifier.tick()  # once a week
    assert len([k for k in butler.keys if k.startswith("weekly")]) == 14


async def test_not_on_other_days(events):
    notifier, butler, _, _ = make_gbl(events, datetime(2026, 10, 27, 8, 0, tzinfo=UTC))  # Tuesday
    await notifier.tick()
    assert not [k for k in butler.keys if k.startswith("weekly")]


async def test_partner_failure_retries(events):
    notifier, butler, clock, partner = make_gbl(events, datetime(2026, 10, 26, 8, 0, tzinfo=UTC))

    async def down(competition, season):
        raise RuntimeError("down")

    partner.games = down
    await notifier.tick()
    assert not [k for k in butler.keys if k.startswith("weekly")]
    del partner.games  # back to the class method
    clock.t += timedelta(minutes=10)
    await notifier.tick()
    assert len([k for k in butler.keys if k.startswith("weekly")]) == 14


async def test_partner_down_past_patience_sends_without_it(events):
    notifier, butler, _, partner = make_gbl(events, datetime(2026, 10, 26, 10, 5, tzinfo=UTC))  # 12:05

    async def down(competition, season):
        raise RuntimeError("down")

    partner.games = down
    await notifier.tick()
    texts = {n["key"]: n["text"] for n in butler.sent if n["key"].startswith("weekly")}
    assert len(texts) == 14 and "EuroLeague" not in texts["weekly:2026-10-26:PAO"]


def test_linked_club_without_a_gbl_game_still_gets_its_week():
    weeks = weekly.team_weeks([], EL_WEEK, gbl.clubs(), ("G",), START, END)  # before the GBL season
    assert [f.competition for f in weeks["PAO"][1]] == ["E", "E"]
