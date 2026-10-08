"""Tracker decisions with a fake clock, scripted live states and a recording PiButler."""

import asyncio
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
        self.sent: list[dict] = []
        self.manifest = None
        self.fail_next = 0

    async def notify(self, key, text, tags=None, expires_at=None, **extra):
        if self.fail_next:
            self.fail_next -= 1
            raise ButlerError("down")
        self.sent.append({"key": key, "text": text, "tags": tags or {}, "expires_at": expires_at, **extra})
        return {"recipients": 1, "duplicate": False}

    async def register(self, manifest):
        self.manifest = manifest

    @property
    def keys(self) -> list[str]:
        return [n["key"] for n in self.sent]

    def get(self, suffix: str) -> dict:
        return next(n for n in self.sent if n["key"].endswith(suffix))


def state(ended: int, over: bool = False) -> PeriodState:
    return PeriodState(ended_periods=ended, game_over=over, current_period=ended + (0 if over else 1))


PRE = PeriodState(ended_periods=0, game_over=False, current_period=0)  # before tip-off


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
    # Regulation games end with EG only (no Q4 EP), so Q4 goes straight to the final messages.
    states = [PRE, PRE, state(0), state(0), state(1), state(1), state(2), state(3), state(4, over=True)]
    notifier, butler, clock = make(events, states)
    await notifier.track("E", GAME)
    gid = GAME.identifier
    reminder = f"reminder:{TIPOFF:%Y-%m-%dT%H:%M:%SZ}"
    assert butler.keys == [f"{gid}:{k}" for k in (reminder, "start", "p1", "p2", "p3", "final", "result")]
    assert clock() < TIPOFF + timedelta(minutes=30)  # stopped polling once final


async def test_who_gets_what(events):
    states = [PRE, state(0), state(1), state(4, over=True)]
    notifier, butler, _ = make(events, states)
    await notifier.track("E", GAME)
    gid, followers, others = GAME.identifier, {"topic": GAME.identifier, "following": True}, None
    # followers only: reminder, quarter reports, full final
    for suffix in (":00Z", ":p1", ":final"):  # reminder key ends with its tip-off time
        assert butler.get(suffix)["audience"] == followers, suffix
    # everyone: tip-off with a follow button
    start = butler.get(":start")
    assert start.get("audience") is others and start["tags"] == {"kind": ["start"]}
    assert start["buttons"][0][0]["follow"] == gid
    # non-followers: one-line result with a Stats button revealing the full report
    result = butler.get(":result")
    assert result["audience"] == {"topic": gid, "following": False}
    assert result["tags"] == {"kind": ["final"]}
    stats = result["buttons"][0][0]
    assert stats["label"] == "📊 Stats" and stats["reveal"] == butler.get(":final")["text"]
    assert "Team stats" not in result["text"] and "Team stats" in stats["reveal"]
    # every game message registers the topic, with auto-follow by team
    assert all(
        n["topics"][0]["auto_follow"] == {"teams": [GAME.home.code, GAME.away.code]} for n in butler.sent
    )
    # quarter reports go stale, finals don't
    assert butler.get(":p1")["expires_at"] and butler.get(":final")["expires_at"] is None


async def test_overtime_reports_q4_and_each_ot(events):
    states = [state(3), state(4), state(5), state(6, over=True)]
    notifier, butler, _ = make(events, states, start=TIPOFF)
    await notifier.track("E", GAME)
    gid = GAME.identifier
    assert butler.keys == [f"{gid}:{k}" for k in ("p3", "p4", "p5", "final", "result")]


async def test_restart_mid_game_skips_already_sent_and_old_quarters(events):
    gid = GAME.identifier
    for key in (f"reminder:{TIPOFF:%Y-%m-%dT%H:%M:%SZ}", "start", "p1"):
        await events.add(f"{gid}:{key}")
    notifier, butler, _ = make(events, [state(3), state(4, over=True)], start=TIPOFF + timedelta(minutes=70))
    await notifier.track("E", GAME)
    assert butler.keys == [f"{gid}:p3", f"{gid}:final", f"{gid}:result"]


async def test_restart_between_the_two_final_messages(events):
    gid = GAME.identifier
    await events.add(f"{gid}:final")
    notifier, butler, _ = make(events, [state(4, over=True)], start=TIPOFF + timedelta(hours=2))
    await notifier.track("E", GAME)
    assert butler.keys == [f"{gid}:result"]


async def test_late_start_skips_reminder_and_tipoff(events):
    notifier, butler, _ = make(events, [state(4, over=True)], start=TIPOFF + timedelta(minutes=5))
    await notifier.track("E", GAME)
    assert butler.keys == [f"{GAME.identifier}:final", f"{GAME.identifier}:result"]


async def test_pibutler_outage_retries_without_losing_or_duplicating(events):
    notifier, butler, _ = make(events, [state(1), state(1), state(1), state(4, over=True)], start=TIPOFF)
    butler.fail_next = 1
    await notifier.track("E", GAME)
    gid = GAME.identifier
    assert butler.keys == [f"{gid}:p1", f"{gid}:final", f"{gid}:result"]


async def test_gives_up_after_max_game_length(events):
    notifier, butler, clock = make(events, [state(2)], start=TIPOFF)
    await notifier.track("E", GAME)
    assert butler.keys == [f"{GAME.identifier}:p2"]
    assert clock() >= TIPOFF + timedelta(hours=4)


async def test_daily_schedule_at_noon_with_follow_buttons(events):
    notifier, butler, clock = make(events, [PRE], start=datetime(2026, 10, 8, 8, 0, tzinfo=UTC))
    await notifier.refresh_schedule()
    await notifier.maybe_send_schedule()  # 11:00 Athens: before noon
    assert butler.keys == []
    clock.t = datetime(2026, 10, 8, 9, 30, tzinfo=UTC)  # 12:30 Athens
    await notifier.maybe_send_schedule()
    await notifier.maybe_send_schedule()
    assert butler.keys == ["schedule:2026-10-08"]
    digest = butler.sent[0]
    assert digest["tags"] == {"kind": ["schedule"]}
    (button,) = digest["buttons"][0]
    assert button["follow"] == GAME.identifier and "{{hm:2026-10-08T16:00:00Z}}" in button["label"]
    assert "{{time:" not in digest["text"] + button["label"]  # today's schedule: times only
    assert [t["id"] for t in digest["topics"]] == [GAME.identifier]


async def test_no_schedule_after_all_games_started(events):
    notifier, butler, _ = make(events, [PRE], start=TIPOFF + timedelta(minutes=1))
    await notifier.refresh_schedule()
    await notifier.maybe_send_schedule()
    assert butler.keys == []


async def test_register_builds_manifest_from_clubs(events):
    notifier, butler, _ = make(events, [state(0)])
    await notifier.register()
    teams, kind = butler.manifest["settings"]
    assert len(teams["options"]) == 20 and teams["default"] == [] and "all_label" not in teams
    assert kind["default"] == ["schedule", "start", "final"]


def test_manifest_dedupes_clubs_across_competitions():
    clubs = parse_clubs(load("v2_clubs_E2026.json"))
    assert len(build_manifest(clubs + clubs)["settings"][0]["options"]) == 20


async def test_event_store_last_period(events):
    assert await events.last_period("E2026_1") == 0
    for key in ("E2026_1:p1", "E2026_1:p3", "E2026_12:p5", "E2026_1:final"):
        await events.add(key)
    assert await events.last_period("E2026_1") == 3


class Blocking(FakeClock):
    """A clock whose sleeps never finish, so tracker tasks stay parked in their first wait."""

    async def sleep(self, seconds: float) -> None:
        await asyncio.Event().wait()


async def test_rescheduled_game_restarts_its_waiting_tracker(events):
    clock = Blocking(TIPOFF - timedelta(hours=5))
    el = FakeEuroleague([PRE])
    settings = Settings(pibutler_url="http://x", pibutler_api_key="k")
    notifier = Notifier(settings, el, FakeButler(), events, clock=clock, sleep=clock.sleep)
    await notifier.refresh_schedule()
    await notifier.tick()
    first = notifier._tasks[GAME.identifier]

    moved = replace(GAME, tipoff=TIPOFF + timedelta(hours=1))
    el.games_list = [moved]
    await notifier.refresh_schedule()
    await notifier.tick()
    await asyncio.sleep(0)
    assert first.cancelled()
    assert notifier._tracked[GAME.identifier].tipoff == moved.tipoff
    notifier._tasks[GAME.identifier].cancel()


async def test_schedule_change_after_start_does_not_restart(events):
    clock = Blocking(TIPOFF + timedelta(minutes=5))
    el = FakeEuroleague([state(0)])
    notifier = Notifier(
        Settings(pibutler_url="http://x", pibutler_api_key="k"),
        el,
        FakeButler(),
        events,
        clock=clock,
        sleep=clock.sleep,
    )
    await notifier.refresh_schedule()
    await notifier.tick()
    first = notifier._tasks[GAME.identifier]
    await events.add(f"{GAME.identifier}:start")
    el.games_list = [replace(GAME, tipoff=TIPOFF + timedelta(minutes=10))]
    await notifier.refresh_schedule()
    await notifier.tick()
    await asyncio.sleep(0)
    assert not first.cancelled()
    first.cancel()
