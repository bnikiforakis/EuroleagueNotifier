import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from euroleague_notifier.api import (
    PBP_SECTIONS,
    ApiError,
    EuroleagueClient,
    parse_boxscore,
    parse_clubs,
    parse_games,
    parse_header,
    parse_period_state,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    text = (FIXTURES / name).read_text()
    return json.loads(text) if text.strip() else None


def test_parse_games_strips_and_uses_utc():
    games = {g.code: g for g in parse_games(load("v2_games_E2026.json"))}
    game = games[37]
    assert game.identifier == "E2026_37"
    assert game.tipoff == datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    assert game.tipoff.utcoffset().total_seconds() == 0
    assert (game.home.code, game.away.code) == ("DUB", "RED")
    assert not game.played and game.home_score is None
    assert game.venue == "COCA-COLA ARENA"

    finished = games[31]
    assert finished.played
    assert (finished.home.code, finished.away.code, finished.home_score, finished.away_score) == (
        "PRS",
        "ASV",
        96,
        98,
    )
    assert finished.round == 4


def test_parse_clubs():
    clubs = {c.code: c for c in parse_clubs(load("v2_clubs_E2026.json"))}
    assert len(clubs) == 20
    assert all(code == code.strip() and code for code in clubs)
    assert clubs["IST"].name == "Anadolu Efes Istanbul"
    assert clubs["IST"].short_name == "Anadolu Efes"


def test_header_empty_body_is_none():
    assert parse_header(load("live_Header_empty.json")) is None


def test_header_regulation_per_period():
    header = parse_header(load("live_Header_E2026_31.json"))
    assert not header.live
    assert (header.home_score, header.away_score) == (96, 98)
    assert header.quarter_scores == [(21, 14), (22, 32), (34, 28), (19, 24)]
    assert sum(h for h, _ in header.quarter_scores) == header.home_score
    assert sum(a for _, a in header.quarter_scores) == header.away_score


def test_header_overtime_is_one_combined_entry():
    header = parse_header(load("live_Header_E2025_340.json"))
    assert (header.home_score, header.away_score) == (110, 104)
    assert header.quarter_scores == [(17, 27), (24, 14), (18, 25), (25, 18), (26, 20)]
    assert sum(h for h, _ in header.quarter_scores) == 110


def test_period_state_regulation_final():
    # Q4 closes with EG only (no EP), which must still count as ended.
    state = parse_period_state(load("live_PlayByPlay_E2026_31.json"))
    assert state.ended_periods == 4
    assert state.game_over
    assert state.current_period == 4


def test_period_state_double_overtime():
    state = parse_period_state(load("live_PlayByPlay_E2025_340.json"))
    assert state.ended_periods == 6
    assert state.game_over
    assert state.current_period == 6


def test_period_state_mid_game():
    pbp = copy.deepcopy(load("live_PlayByPlay_E2026_31.json"))
    for section in PBP_SECTIONS[2:]:
        pbp[section] = []
    assert pbp["SecondQuarter"][-1]["PLAYTYPE"] == "EP"
    pbp["Live"] = True
    state = parse_period_state(pbp)
    assert state.ended_periods == 2
    assert not state.game_over


def test_period_state_before_tipoff():
    state = parse_period_state(None)
    assert (state.ended_periods, state.game_over, state.current_period) == (0, False, 0)


@pytest.mark.parametrize(
    ("season", "code", "periods"),
    [("E2026", 31, 4), ("E2025", 340, 6)],
)
def test_boxscore_matches_header(season, code, periods):
    box = parse_boxscore(load(f"live_Boxscore_{season}_{code}.json"))
    header = parse_header(load(f"live_Header_{season}_{code}.json"))
    assert (box.home.points, box.away.points) == (header.home_score, header.away_score)
    assert len(box.quarter_scores) == periods
    assert sum(h for h, _ in box.quarter_scores) == box.home.points
    assert sum(a for _, a in box.quarter_scores) == box.away.points
    assert sum(p.points for p in box.home_players) == box.home.points
    assert sum(p.points for p in box.away_players) == box.away.points
    assert box.home.code and box.home.code == box.home.code.strip()


def test_boxscore_details():
    box = parse_boxscore(load("live_Boxscore_E2026_31.json"))
    assert (box.home.code, box.away.code) == ("PRS", "ASV")
    assert box.home.name == "PARIS BASKETBALL"
    assert len(box.home_players) == 12
    assert box.home.fg_pct is not None and 0 < box.home.fg_pct < 100
    warren = box.home_players[0]
    assert warren.name == "WARREN, TJ"
    assert warren.short_name == "T. Warren"
    assert warren.minutes == "21:39"
    assert box.quarter_scores == [(21, 14), (22, 32), (34, 28), (19, 24)]


def test_boxscore_empty_is_none():
    assert parse_boxscore(None) is None


def _transport(responses):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        result = responses[min(len(calls), len(responses)) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    return httpx.MockTransport(handler), calls


async def test_client_header_and_user_agent():
    body = (FIXTURES / "live_Header_E2026_31.json").read_bytes()
    transport, calls = _transport([httpx.Response(200, content=body)])
    async with EuroleagueClient(transport=transport) as client:
        header = await client.header("E2026", 31)
    assert header.home_score == 96
    assert calls[0].url.params["gamecode"] == "31"
    assert calls[0].url.params["seasoncode"] == "E2026"
    assert "euroleague-notifier" in calls[0].headers["User-Agent"]


async def test_client_empty_body_before_tipoff():
    transport, _ = _transport([httpx.Response(200, content=b"")])
    async with EuroleagueClient(transport=transport) as client:
        assert await client.header("E2026", 40) is None
        assert await client.boxscore("E2026", 40) is None
        assert (await client.period_state("E2026", 40)).ended_periods == 0


async def test_client_retries_then_succeeds():
    body = (FIXTURES / "v2_games_E2026.json").read_bytes()
    transport, calls = _transport(
        [httpx.ConnectError("boom"), httpx.Response(503), httpx.Response(200, content=body)]
    )
    async with EuroleagueClient(transport=transport, backoff=0) as client:
        games = await client.games("E", "E2026")
    assert len(calls) == 3
    assert games


async def test_client_raises_after_retries():
    transport, calls = _transport([httpx.Response(502)])
    async with EuroleagueClient(transport=transport, backoff=0) as client:
        with pytest.raises(ApiError):
            await client.clubs("E", "E2026")
    assert len(calls) == 3


async def test_client_does_not_retry_4xx():
    transport, calls = _transport([httpx.Response(404)])
    async with EuroleagueClient(transport=transport, backoff=0) as client:
        with pytest.raises(ApiError):
            await client.clubs("E", "E2026")
    assert len(calls) == 1


def test_period_state_before_first_begin_period_is_not_started():
    pbp = {"ActualQuarter": 1, **{section: [] for section in PBP_SECTIONS}}
    assert parse_period_state(pbp).current_period == 0
