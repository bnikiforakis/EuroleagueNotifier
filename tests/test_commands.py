"""/score callback views through aiohttp's test client, with scripted live feeds and a fake clock."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from euroleague_notifier.api import ApiError, parse_boxscore, parse_clubs, parse_games, parse_header
from euroleague_notifier.commands import ACTION_RE, Scoreboard, TtlCache, callback_path, create_app
from euroleague_notifier.config import Settings
from euroleague_notifier.models import Club
from euroleague_notifier.reports import DASH
from euroleague_notifier.store import EventStore
from euroleague_notifier.tracker import Notifier

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "secret"
AUTH = {"Authorization": f"Bearer {KEY}"}
NOW = datetime(2026, 10, 8, 18, 20, tzinfo=UTC)  # 21:20 in Athens
ATHENS = {"id": 1, "timezone": "Europe/Athens"}


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


GAMES = parse_games(load("v2_games_E2026.json"))
BY_CODE = {g.code: g for g in GAMES}
FINAL_HEADER = parse_header(load("live_Header_E2026_31.json"))  # PRS 96-98 ASV, Live false
BOX = parse_boxscore(load("live_Boxscore_E2026_31.json"))


def live_header(quarter: str = "3", remaining: str = "05:12", cum=((22, 37, 45), (18, 35, 41))):
    raw = load("live_Header_E2026_31.json") | {
        "Live": True,
        "Quarter": quarter,
        "RemainingPartialTime": remaining,
    }
    for i in range(4):
        raw[f"ScoreQuarter{i + 1}A"] = cum[0][i] if i < len(cum[0]) else 0
        raw[f"ScoreQuarter{i + 1}B"] = cum[1][i] if i < len(cum[1]) else 0
    raw["ScoreA"], raw["ScoreB"] = str(cum[0][-1]), str(cum[1][-1])
    return parse_header(raw)


class FakeEuroleague:
    def __init__(self, headers=None, boxes=None, games=None):
        self.headers = {32: FINAL_HEADER, 37: FINAL_HEADER, 33: live_header("4", "01:00"), 36: live_header()}
        self.headers |= headers or {}
        self.boxes = boxes or {}
        self.games_list = GAMES if games is None else games
        self.calls: list[tuple[str, int]] = []

    async def header(self, season, code):
        self.calls.append(("header", code))
        value = self.headers.get(code)
        if isinstance(value, Exception):
            raise value
        return value

    async def boxscore(self, season, code):
        self.calls.append(("box", code))
        value = self.boxes.get(code, BOX)
        if isinstance(value, Exception):
            raise value
        return value

    async def games(self, competition, season):
        return self.games_list if competition == "E" else []

    async def clubs(self, competition, season):
        return parse_clubs(load("v2_clubs_E2026.json"))


class FakeButler:
    manifest = None

    async def register(self, manifest):
        self.manifest = manifest


class Setup:
    def __init__(self, notifier: Notifier, el: FakeEuroleague, client: TestClient, events: EventStore):
        self.notifier, self.el, self.client, self.events = notifier, el, client, events
        self.now = NOW

    async def post(self, body, headers=AUTH) -> dict:
        resp = await self.client.post("/pibutler", json=body, headers=headers)
        assert resp.status == 200
        reply = await resp.json()
        check_limits(reply)
        return reply

    async def command(self, user=ATHENS) -> dict:
        return await self.post({"type": "command", "command": "score", "args": "", "user": user})

    async def action(self, data: str, user=ATHENS) -> dict:
        return await self.post({"type": "action", "data": data, "user": user})


def check_limits(reply: dict) -> None:
    assert len(reply["text"]) <= 4000
    assert len(reply["buttons"]) <= 12
    for row in reply["buttons"]:
        assert 1 <= len(row) <= 3
        for button in row:
            assert len(button["label"]) <= 64
            assert "url" in button or ACTION_RE.fullmatch(button["action"])
            assert ":" not in button.get("action", "")


def labels(reply: dict) -> list[str]:
    return [b["label"] for row in reply["buttons"] for b in row]


def actions(reply: dict) -> list[list[str]]:
    return [[b["action"] for b in row] for row in reply["buttons"]]


@pytest.fixture
async def make():
    stores, clients = [], []

    async def _make(el=None, competitions=("E",), now=NOW) -> Setup:
        el = el or FakeEuroleague()
        events = await EventStore.open(":memory:")
        settings = Settings(pibutler_url="http://x", pibutler_api_key=KEY, competitions=competitions)
        setup = None
        notifier = Notifier(settings, el, FakeButler(), events, clock=lambda: setup.now)
        client = TestClient(TestServer(create_app(Scoreboard(notifier), KEY)))
        await client.start_server()
        setup = Setup(notifier, el, client, events)
        setup.now = now
        await notifier.refresh_schedule()
        stores.append(events)
        clients.append(client)
        return setup

    yield _make
    for client in clients:
        await client.close()
    for store in stores:
        await store.close()


# --- transport -------------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": KEY}])
async def test_rejects_wrong_or_missing_key(make, headers):
    s = await make()
    resp = await s.client.post("/pibutler", json={"type": "command", "command": "score"}, headers=headers)
    assert resp.status == 401


@pytest.mark.parametrize("data", [b"{not json", b"[1, 2]"])
async def test_bad_json_is_400(make, data):
    s = await make()
    resp = await s.client.post("/pibutler", data=data, headers=AUTH | {"Content-Type": "application/json"})
    assert resp.status == 400


async def test_unknown_command_and_action_are_friendly(make):
    s = await make()
    reply = await s.post({"type": "command", "command": "standings", "user": ATHENS})
    assert "Unknown command" in reply["text"]
    assert "expired" in (await s.action("x|1"))["text"]
    assert "isn't on the schedule" in (await s.action("g|999"))["text"]
    assert actions(await s.action("g|999")) == [["l"]]


# --- list ------------------------------------------------------------------------------------


async def test_score_lists_todays_games_by_tipoff(make):
    s = await make()
    reply = await s.command()
    assert reply["text"] == "🏀 <b>Today's EuroLeague games</b>\n\nTap a game for the live score."
    assert labels(reply) == [
        f"✅ TEL 96{DASH}98 MIL",
        f"✅ DUB 96{DASH}98 RED",
        f"🔴 MUN 45{DASH}41 VIR · Q4 01:00",
        f"🔴 PAN 45{DASH}41 ULK · Q3 05:12",
        f"⏳ {{{{hm:2026-10-08T18:30:00Z}}}} Valencia {DASH} Hapoel TLV",
        f"⏳ {{{{hm:2026-10-08T18:45:00Z}}}} Real Madrid {DASH} Partizan",
    ]
    assert actions(reply) == [["g|32"], ["g|37"], ["g|33"], ["g|36"], ["g|34"], ["g|35"]]
    # Only tipped-off games hit the live feed (yesterday's game 31 is long over and not today).
    assert sorted(code for _, code in s.el.calls) == [32, 33, 36, 37]


async def test_break_shows_break_instead_of_clock(make):
    s = await make(FakeEuroleague({36: live_header(quarter="", remaining="10:00")}))
    assert f"🔴 PAN 45{DASH}41 ULK · Break" in labels(await s.command())


async def test_finished_from_event_store_even_if_header_still_live(make):
    s = await make()
    await s.events.add(f"{BY_CODE[36].identifier}:final")
    assert f"✅ PAN 45{DASH}41 ULK" in labels(await s.command())


async def test_today_is_the_users_date_and_live_games_from_yesterday_stay(make):
    # 01:10 in Athens on Oct 9: no games today, but game 35 (21:45 Athens, Oct 8) is still live;
    # the other recent games are over and belong to yesterday.
    headers = {33: FINAL_HEADER, 36: FINAL_HEADER, 35: live_header("4", "00:30")}
    s = await make(FakeEuroleague(headers), now=datetime(2026, 10, 8, 22, 10, tzinfo=UTC))
    assert labels(await s.command()) == [f"🔴 MAD 45{DASH}41 PAR · Q4 00:30"]


async def test_no_games_today_shows_next_game(make):
    s = await make(now=datetime(2026, 10, 6, 12, 0, tzinfo=UTC))
    reply = await s.command()
    assert "No EuroLeague games today." in reply["text"]
    assert "Next: {{time:2026-10-07T18:45:00Z}} <b>Paris</b> vs <b>LDLC ASVEL</b>" in reply["text"]
    assert reply["buttons"] == []
    s.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert "Next" not in (await s.command())["text"]


async def test_schedule_not_loaded_yet(make):
    s = await make(FakeEuroleague(games=[]))
    assert "still loading" in (await s.command())["text"]


async def test_api_error_marks_the_game_unavailable(make):
    s = await make(FakeEuroleague({36: ApiError("down")}))
    reply = await s.command()
    assert "Live data unavailable right now" in reply["text"]
    assert f"🏀 {{{{hm:2026-10-08T18:15:00Z}}}} Panathinaikos {DASH} Fenerbahce" in labels(reply)


async def test_headers_are_cached_between_requests(make):
    s = await make()
    await s.command()
    await s.action("g|36")
    await s.action("l")
    assert s.el.calls.count(("header", 36)) == 1


async def test_long_names_are_cut_to_the_label_limit(make):
    long = Club("LNG", "X" * 80, "Very Long Club Name " * 3)
    game = replace(BY_CODE[34], home=long, away=long)
    s = await make(FakeEuroleague(games=[game]))
    (label,) = labels(await s.command())
    assert (
        len(label) == 64 and label.startswith("⏳ {{hm:2026-10-08T18:30:00Z}} Very") and label.endswith("…")
    )


# --- game, stats, back -----------------------------------------------------------------------


async def test_live_game_view(make):
    s = await make()
    reply = await s.action("g|36")
    assert reply["text"] == (
        f"🔴 <b>Panathinaikos</b> 45{DASH}41 <b>Fenerbahce</b>\nQ3 · 05:12 left\n\n"
        # periods come from the box score (exact overtimes), trimmed to those under way
        f"Q1 21{DASH}14 · Q2 22{DASH}32 · Q3 34{DASH}28"
    )
    assert actions(reply) == [["s|36", "g|36"], ["l"]]
    assert labels(reply) == ["📊 Stats", "🔄 Refresh", "⬅️ Back"]


async def test_finished_game_view(make):
    s = await make()
    reply = await s.action("g|37")
    assert reply["text"] == (
        f"✅ <b>Final</b> · Dubai 96{DASH}98 <b>Crvena Zvezda</b>\n\n"
        f"Q1 21{DASH}14 · Q2 22{DASH}32 · Q3 34{DASH}28 · Q4 19{DASH}24"
    )
    assert actions(reply) == [["s|37", "g|37"], ["l"]]


async def test_upcoming_game_view_has_no_stats(make):
    s = await make()
    reply = await s.action("g|34")
    assert reply["text"].startswith(
        "⏳ <b>Valencia</b> vs <b>Hapoel TLV</b>\nTip-off {{time:2026-10-08T18:30:00Z}}"
    )
    assert actions(reply) == [["g|34"], ["l"]]


async def test_live_stats_view(make):
    s = await make(FakeEuroleague(boxes={36: replace(BOX, live=True)}))
    reply = await s.action("s|36")
    assert reply["text"].startswith(f"📊 <b>Live stats</b> · Q3 05:12 · PAN 45{DASH}41 ULK\n\n<pre>")
    assert "🔥 <b>Top scorers</b>" in reply["text"] and "📊 <b>Team stats</b>" in reply["text"]
    assert "Q4" not in reply["text"]  # periods sliced to the ones under way
    assert actions(reply) == [["s|36", "g|36"]]
    assert labels(reply) == ["🔄 Refresh", "⬅️ Game"]


async def test_final_stats_view(make):
    s = await make()
    text = (await s.action("s|37"))["text"]
    assert text.startswith(f"📊 <b>Final</b> · DUB 96{DASH}98 RED")
    ft = next(line for line in text.splitlines() if line.startswith("FT "))
    assert "/" in ft and "%)" in ft and "PIR" in text


async def test_stats_unavailable(make):
    s = await make(FakeEuroleague(boxes={36: ApiError("down")}))
    reply = await s.action("s|36")
    assert "Live data unavailable right now" in reply["text"]
    assert actions(reply) == [["s|36", "g|36"]]


async def test_back_returns_the_list(make):
    s = await make()
    assert await s.action("l") == await s.command()


async def test_multiple_competitions_encode_the_competition(make):
    s = await make(competitions=("E", "U"))
    reply = await s.command()
    assert actions(reply)[0] == ["g|E|32"]
    assert actions(await s.action("g|E|36")) == [["s|E|36", "g|E|36"], ["l"]]


async def test_invalid_timezone_falls_back(make):
    s = await make()
    assert len((await s.command(user={"id": 1, "timezone": "Mars/Base"}))["buttons"]) == 6


# --- manifest and helpers --------------------------------------------------------------------


async def test_manifest_includes_command_and_callback(make):
    s = await make()
    await s.notifier.register()
    manifest = s.notifier.butler.manifest
    assert manifest["commands"] == [{"command": "score", "description": "Live scores of today's games"}]
    assert manifest["callback_url"] == "http://euroleague-notifier:8081/pibutler"


def test_callback_path():
    assert callback_path("http://euroleague-notifier:8081/pibutler") == "/pibutler"
    assert callback_path("http://host:1") == "/"


async def test_cache_expires_and_does_not_keep_failures():
    t = [0.0]
    cache = TtlCache(15, clock=lambda: t[0])
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) == 2:
            raise ApiError("down")
        return len(calls)

    assert await cache.get("k", fetch) == 1
    assert await cache.get("k", fetch) == 1
    t[0] = 16
    with pytest.raises(ApiError):
        await cache.get("k", fetch)
    assert await cache.get("k", fetch) == 3


# --- regression tests for the code review ----------------------------------------------------


async def test_game_view_falls_back_to_header_periods_without_a_box_score(make):
    s = await make(FakeEuroleague(boxes={36: ApiError("down")}))
    text = (await s.action("g|36"))["text"]
    assert f"Q1 22{DASH}18 · Q2 15{DASH}17 · Q3 8{DASH}6" in text


async def test_final_overtime_count_comes_from_the_box_score(make):
    two_ot = parse_boxscore(load("live_Boxscore_E2025_340.json"))
    s = await make(FakeEuroleague(boxes={37: two_ot}))
    assert "(2OT)" in (await s.action("g|37"))["text"]


async def test_stats_view_fetches_header_and_box_score_together(make):
    started: list[str] = []
    release = asyncio.Event()

    class Slow(FakeEuroleague):
        async def header(self, season, code):
            started.append("header")
            await release.wait()
            return await super().header(season, code)

        async def boxscore(self, season, code):
            started.append("box")
            await release.wait()
            return await super().boxscore(season, code)

    s = await make(Slow())
    task = asyncio.create_task(s.action("s|36"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert sorted(started) == ["box", "header"]  # both in flight before either finished
    release.set()
    await task


def test_too_long_text_is_replaced_not_cut_mid_tag():
    from euroleague_notifier.commands import MAX_TEXT, TOO_LONG, Reply

    assert Reply("<pre>" + "x" * MAX_TEXT + "</pre>").as_dict()["text"] == TOO_LONG


async def test_more_games_than_button_rows(make):
    from euroleague_notifier.commands import MAX_GAMES

    many = [replace(BY_CODE[34], code=100 + i) for i in range(MAX_GAMES + 3)]
    reply = await (await make(FakeEuroleague(games=many))).command()
    assert len(reply["buttons"]) == MAX_GAMES and "…and 3 more" in reply["text"]


def test_callback_port_comes_from_the_url():
    assert (
        Settings(pibutler_url="x", pibutler_api_key="k", callback_url="http://n:9000/p").callback_port == 9000
    )
