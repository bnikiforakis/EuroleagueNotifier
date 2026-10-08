"""Tracker decisions with a fake clock, scripted live states and a recording PiButler."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from euroleague_notifier.api import parse_boxscore, parse_clubs, parse_games
from euroleague_notifier.butler import ButlerError
from euroleague_notifier.config import Settings
from euroleague_notifier.models import PeriodState
from euroleague_notifier.store import EventStore
from euroleague_notifier.tracker import Notifier, build_manifest

FIXTURES = Path(__file__).parent / "fixtures"
TIPOFF = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


GAME = replace(parse_games(load("v2_games_E2026.json"))[0], tipoff=TIPOFF, code=99)
BOX = parse_boxscore(load("live_Boxscore_E2026_31.json"))


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeEuroleague:
    def __init__(self, states: list[PeriodState], games=None):
        self.states = list(states)
        self.games_list = games or [GAME]

    async def period_state(self, season, code):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    async def boxscore(self, season, code):
        return BOX

    async def games(self, competition, season):
        return self.games_list

    async def clubs(self, competition, season):
        return parse_clubs(load("v2_clubs_E2026.json"))


class FakeButler:
    def __init__(self):
        self.sent: list[tuple[str, dict, str | None]] = []
        self.manifest = None
        self.fail_next = 0

    async def notify(self, key, text, tags, expires_at=None):
        if self.fail_next:
            self.fail_next -= 1
            raise ButlerError("down")
        self.sent.append((key, tags, expires_at))
        return {"recipients": 1, "duplicate": False}

    async def register(self, manifest):
        self.manifest = manifest

    @property
    def keys(self) -> list[str]:
        return [k for k, _, _ in self.sent]


def state(ended: int, over: bool = False) -> PeriodState:
    return PeriodState(ended_periods=ended, game_over=over, current_period=ended + (0 if over else 1))


@pytest.fixture
async def events():
    store = await EventStore.open(":memory:")
    yield store
    await store.close()


def make(events, states, start=TIPOFF - timedelta(hours=1), games=None):
    clock = FakeClock(start)
    butler = FakeButler()
    settings = Settings(pibutler_url="http://x", pibutler_api_key="k", poll_seconds=45)
    notifier = Notifier(
        settings, FakeEuroleague(states, games), butler, events, clock=clock, sleep=clock.sleep
    )
    return notifier, butler, clock


async def test_full_regulation_game(events):
    # Regulation games end with EG only (no Q4 EP), so Q4 goes straight to the final report.
    states = [state(0), state(0), state(1), state(1), state(2), state(3), state(3), state(4, over=True)]
    notifier, butler, clock = make(events, states)
    await notifier.track("E", GAME)
    gid = GAME.identifier
    assert butler.keys == [f"{gid}:reminder", f"{gid}:p1", f"{gid}:p2", f"{gid}:p3", f"{gid}:final"]
    assert butler.sent[0][1] == {"teams": [GAME.home.code, GAME.away.code], "kind": ["reminder"]}
    assert butler.sent[1][1]["kind"] == ["quarter"] and butler.sent[1][2] is not None  # quarter expires
    assert butler.sent[-1][2] is None  # final never expires
    assert clock() < TIPOFF + timedelta(minutes=30)  # stopped polling once final


async def test_overtime_reports_q4_and_each_ot(events):
    states = [state(3), state(4), state(5), state(6, over=True)]
    notifier, butler, _ = make(events, states, start=TIPOFF)
    await notifier.track("E", GAME)
    gid = GAME.identifier
    assert butler.keys == [f"{gid}:p3", f"{gid}:p4", f"{gid}:p5", f"{gid}:final"]


async def test_restart_mid_game_skips_already_sent_and_old_quarters(events):
    gid = GAME.identifier
    await events.add(f"{gid}:reminder")
    await events.add(f"{gid}:p1")
    notifier, butler, _ = make(events, [state(3), state(4, over=True)], start=TIPOFF + timedelta(minutes=70))
    await notifier.track("E", GAME)
    assert butler.keys == [f"{gid}:p3", f"{gid}:final"]  # no p2 replay, no second p1/reminder


async def test_late_start_skips_reminder(events):
    notifier, butler, _ = make(events, [state(4, over=True)], start=TIPOFF + timedelta(minutes=5))
    await notifier.track("E", GAME)
    assert butler.keys == [f"{GAME.identifier}:final"]


async def test_pibutler_outage_retries_without_losing_or_duplicating(events):
    notifier, butler, _ = make(events, [state(1), state(1), state(1), state(4, over=True)], start=TIPOFF)
    butler.fail_next = 1
    await notifier.track("E", GAME)
    gid = GAME.identifier
    assert butler.keys == [f"{gid}:p1", f"{gid}:final"]


async def test_gives_up_after_max_game_length(events):
    notifier, butler, clock = make(events, [state(2)], start=TIPOFF)
    await notifier.track("E", GAME)
    assert butler.keys == [f"{GAME.identifier}:p2"]
    assert clock() >= TIPOFF + timedelta(hours=4)


async def test_digest_once_per_day_with_todays_teams(events):
    notifier, butler, clock = make(events, [state(0)], start=datetime(2026, 10, 8, 5, 0, tzinfo=UTC))
    await notifier.refresh_schedule()
    await notifier.maybe_send_digest()  # 08:00 Athens: before DIGEST_TIME
    assert butler.keys == []
    clock.t = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)  # 09:30 Athens
    await notifier.maybe_send_digest()
    await notifier.maybe_send_digest()
    assert butler.keys == ["digest:2026-10-08"]
    assert butler.sent[0][1] == {"teams": sorted([GAME.home.code, GAME.away.code]), "kind": ["schedule"]}


async def test_no_digest_after_all_games_started(events):
    notifier, butler, _ = make(events, [state(0)], start=TIPOFF + timedelta(minutes=1))
    await notifier.refresh_schedule()
    await notifier.maybe_send_digest()
    assert butler.keys == []


async def test_register_builds_manifest_from_clubs(events):
    notifier, butler, _ = make(events, [state(0)])
    await notifier.register()
    teams, kind = butler.manifest["settings"]
    assert len(teams["options"]) == 20 and teams["all_label"] == "All teams"
    assert kind["default"] == ["schedule", "reminder", "quarter", "final"]


def test_manifest_dedupes_clubs_across_competitions():
    clubs = parse_clubs(load("v2_clubs_E2026.json"))
    assert len(build_manifest(clubs + clubs)["settings"][0]["options"]) == 20


async def test_event_store_last_period(events):
    assert await events.last_period("E2026_1") == 0
    for key in ("E2026_1:p1", "E2026_1:p3", "E2026_12:p5", "E2026_1:final"):
        await events.add(key)
    assert await events.last_period("E2026_1") == 3
