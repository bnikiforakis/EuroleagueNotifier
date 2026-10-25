import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from euroleague_notifier import gbl, highlights, reports
from euroleague_notifier.api import CLUB_LINKS
from euroleague_notifier.config import load_settings
from euroleague_notifier.gbl import (
    GblClient,
    parse_boxscore,
    parse_header,
    parse_other_matches,
    parse_period_state,
    parse_round,
    parse_rounds,
    parse_ticker,
)
from euroleague_notifier.tracker import build_manifest

FIXTURES = Path(__file__).parent / "fixtures"
ROUND4 = (FIXTURES / "esake_round04.html").read_text()
ROUND5 = (FIXTURES / "esake_round05.html").read_text()
TICKER = (FIXTURES / "gbl_ticker.js").read_text()


def feed(match: int) -> dict:
    return json.loads((FIXTURES / f"fiba_{match}.json").read_text())


def until(payload: dict, period: int, kind: str) -> dict:
    """The feed as it was right after a period marker: later plays dropped (they're newest first)."""
    live = copy.deepcopy(payload)
    pbp = live["pbp"]
    index = next(
        i
        for i, p in enumerate(pbp)
        if p["actionType"] == "period"
        and p["subType"] == kind
        and p["period"] + (4 if p["periodType"] == "OVERTIME" else 0) == period
    )
    live["pbp"] = pbp[index:]
    home, away = int(pbp[index]["s1"]), int(pbp[index]["s2"])
    live["tm"]["1"]["score"], live["tm"]["2"]["score"] = home, away
    live["clock"] = "10:00" if kind == "start" else "00:00"
    return live


# --- schedule ----------------------------------------------------------------------------------


def test_parse_round_played():
    games = parse_round(ROUND4, 2026)
    assert len(games) == 7
    game = games[0]
    assert game.identifier == "G2026_705268768"  # esake id 2A098C20
    assert game.round == 4
    assert game.tipoff == datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # 15:00 Athens (EEST)
    assert (game.home.code, game.away.code) == ("MYK", "ARI")
    assert (game.home.short_name, game.away.short_name) == ("Mykonos", "Aris")
    assert game.played and (game.home_score, game.away_score) == (72, 88)
    assert game.home.aliases == ("ΜΥΚΟΝΟΣ Betsson BC",) and game.away.link == "gr-aris"
    assert game.venue.startswith("Κ.Γ.")


def test_parse_round_upcoming_across_the_clock_change():
    games = parse_round(ROUND5, 2026)
    assert len(games) == 7 and not any(g.played for g in games)
    assert games[0].tipoff == datetime(2026, 10, 31, 13, 0, tzinfo=UTC)  # 15:00 Athens (EET)
    last = games[-1]
    assert last.tipoff == datetime(2026, 11, 1, 15, 15, tzinfo=UTC)
    assert (last.home.code, last.away.code, last.home_score) == ("ARI", "PAO", None)


def test_parse_rounds():
    assert parse_rounds(ROUND4) == (4, list(range(1, 27)))
    assert parse_rounds(ROUND5)[0] == 5


def test_year_follows_the_season():
    assert gbl._tipoff("Σαβ 9 Ιαν - 18:00", 2026) == datetime(2027, 1, 9, 18, 0, tzinfo=gbl.ATHENS)
    assert gbl._tipoff("Κυρ 3 Μαΐ - 19:15", 2026).month == 5
    assert gbl._tipoff("Αναβολή", 2026) is None


def test_unknown_team_is_kept():
    club = gbl.club("DEADBEEF", "ΝΕΑ ΟΜΑΔΑ")
    assert club.code == "DEADBEEF" and club.aliases == ("ΝΕΑ ΟΜΑΔΑ",) and club.link is None


def test_every_team_has_a_unique_code_and_link():
    teams = list(gbl.TEAMS.values())
    assert len({t.code for t in teams}) == len({t.link for t in teams}) == len(teams) == 14
    assert all(len(t.short_name) <= 13 for t in teams)  # fits the report tables


def test_links_match_the_european_clubs():
    gbl_links = {t.link for t in gbl.TEAMS.values()}
    assert {link for links in CLUB_LINKS.values() for link in links.values()} <= gbl_links


def test_manifest_carries_links():
    options = build_manifest(gbl.clubs(), "GBL", "gbl")["settings"][0]["options"]
    assert {"value": "PAO", "label": "Panathinaikos", "link": "gr-panathinaikos"} in options


def test_game_url():
    game = parse_round(ROUND4, 2026)[0]
    assert (
        reports.game_url(game, "G") == "https://www.esake.gr/el/action/EsakegameView?idgame=2A098C20&mode=3"
    )


def test_gbl_runs_on_its_own():
    env = {"PIBUTLER_URL": "http://x", "PIBUTLER_API_KEY": "k"}
    assert load_settings(env | {"COMPETITIONS": "G"}).competitions == ("G",)
    with pytest.raises(SystemExit):
        load_settings(env | {"COMPETITIONS": "E,G"})


# --- match ids ---------------------------------------------------------------------------------


def test_parse_ticker():
    listed = parse_ticker(TICKER)
    assert len(listed) == 7
    assert listed[0] == gbl.Listed(2902699, "MYK", "ARI", datetime(2026, 10, 24).date())


def test_parse_other_matches_includes_unstarted_games():
    others = parse_other_matches(feed(2902699))
    assert (others[0].match, others[0].home, others[0].away, others[0].day) == (2902701, "LEF", "IRA", None)


# --- live --------------------------------------------------------------------------------------


def test_regulation_final():
    payload = feed(2902699)
    state = parse_period_state(payload)
    assert (state.ended_periods, state.game_over, state.current_period, state.score) == (4, True, 4, (72, 88))
    header = parse_header(payload)
    assert not header.live and header.quarter is None
    assert header.quarter_scores == [(21, 28), (15, 21), (20, 15), (16, 24)]
    box = parse_boxscore(payload, "MYK", "ARI")
    assert not box.live and (box.home.points, box.away.points) == (72, 88)
    assert box.quarter_scores == header.quarter_scores
    funderburk = box.home_players[0]
    assert funderburk.short_name == "D. Funderburk" and funderburk.team_code == "MYK"
    assert (funderburk.pir, funderburk.plus_minus, funderburk.starter) == (17, -7, True)
    assert box.home.fg_pct == pytest.approx(25 / 58 * 100)


def test_overtime_final():
    payload = feed(2902701)
    state = parse_period_state(payload)
    assert (state.ended_periods, state.game_over, state.score) == (5, True, (94, 86))
    box = parse_boxscore(payload)
    assert box.quarter_scores[-2:] == [(19, 11), (16, 8)]  # Q4 tied at 78, OT1 16-8
    assert len(parse_header(payload).quarter_scores) == 5


def test_during_a_period():
    payload = until(feed(2902699), 2, "start")
    state = parse_period_state(payload)
    assert (state.ended_periods, state.game_over, state.current_period) == (1, False, 2)
    assert state.score == (21, 28)
    header = parse_header(payload)
    assert header.live and header.quarter == 2 and header.remaining == "10:00"
    assert header.quarter_scores == [(21, 28), (0, 0)]


def test_at_a_break():
    payload = until(feed(2902699), 2, "end")
    state = parse_period_state(payload)
    assert (state.ended_periods, state.game_over, state.score) == (2, False, (36, 49))
    header = parse_header(payload)
    assert header.live and header.quarter is None and header.remaining is None


def test_last_period_ended_before_game_end():
    """Q4 gets its own marker; with a decided score no card goes out before the final."""
    from euroleague_notifier.tracker import _another_period_follows

    state = parse_period_state(until(feed(2902699), 4, "end"))
    assert (state.ended_periods, state.game_over) == (4, False)
    assert not _another_period_follows(state)
    tied = parse_period_state(until(feed(2902701), 4, "end"))
    assert _another_period_follows(tied)  # 78-78: overtime follows


def test_not_started():
    payload = until(feed(2902699), 1, "start")
    payload["pbp"] = []
    assert parse_header(payload) is None
    assert parse_period_state(payload).current_period == 0
    assert parse_header(None) is None and parse_boxscore(None) is None


# --- client ------------------------------------------------------------------------------------


def transport(routes: dict[str, httpx.Response | object]):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = str(request.url)
        for prefix, response in routes.items():
            if url.startswith(prefix):
                return response(request) if callable(response) else response
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def round_page(request: httpx.Request) -> httpx.Response:
    series = request.url.params.get("series")
    page = {None: ROUND4, "04": ROUND4, "05": ROUND5}.get(series, "<html>no games</html>")
    return httpx.Response(200, text=page)


def fiba(request: httpx.Request) -> httpx.Response:
    match = int(request.url.path.split("/")[2])
    if request.headers.get("If-None-Match") == f'"{match}"':
        return httpx.Response(304)
    try:
        return httpx.Response(200, json=feed(match), headers={"ETag": f'"{match}"'})
    except FileNotFoundError:
        return httpx.Response(403)


def make_client(now: datetime, ticker: str = TICKER) -> tuple[GblClient, list[httpx.Request], list[float]]:
    mock, calls = transport(
        {
            gbl.RESULTS_URL: round_page,
            gbl.TICKER_URL: httpx.Response(200, text=ticker),
            "https://fibalivestats": fiba,
        }
    )
    ticks = [1000.0]
    client = GblClient(2026, transport=mock, backoff=0, monotonic=lambda: ticks[0], now=lambda: now)
    return client, calls, ticks


async def test_client_schedule_and_live_by_ticker():
    client, calls, _ = make_client(datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    async with client:
        games = {g.code: g for g in await client.games("G", "G2026")}
        assert len(games) == 14  # rounds 4 and 5 (round 3 page is empty here)
        state = await client.period_state("G2026", 705268768)
        assert state.game_over and state.score == (72, 88)
        box = await client.boxscore("G2026", 705268768)
        assert box.home.code == "MYK" and box.home_players[0].team_code == "MYK"
        assert (await client.header("G2026", 705268768)).home_score == 72
    fiba_calls = [c for c in calls if "fibalivestats" in str(c.url)]
    assert len(fiba_calls) == 1  # one download serves header, state and box score


async def test_client_feed_cache_and_etag():
    client, calls, ticks = make_client(datetime(2026, 10, 24, 12, 30, tzinfo=UTC))
    async with client:
        await client.games("G", "G2026")
        await client.boxscore("G2026", 705268768)
        ticks[0] += gbl.FEED_TTL + 1
        assert (await client.boxscore("G2026", 705268768)).home.points == 72
    fiba_calls = [c for c in calls if "fibalivestats" in str(c.url)]
    assert len(fiba_calls) == 2 and fiba_calls[1].headers["If-None-Match"] == '"2902699"'


async def test_client_unknown_match_reads_as_not_started():
    # Hours before round 5, nothing listed: no feed, and the lookup is rate-limited.
    client, calls, _ = make_client(datetime(2026, 10, 31, 10, 0, tzinfo=UTC), ticker="")
    async with client:
        games = await client.games("G", "G2026")
        upcoming = next(g for g in games if g.round == 5)
        assert await client.header("G2026", upcoming.code) is None
        assert await client.boxscore("G2026", upcoming.code) is None
        assert (await client.period_state("G2026", upcoming.code)).current_period == 0
    assert sum("geniussports.com/widget" in str(c.url) for c in calls) == 1


async def test_client_finds_the_rest_of_a_round_in_othermatches():
    # The ticker only lists the first game; the others come from its feed.
    first = TICKER.split("<\\/li>")[0] + "<\\/li>"
    client, _, _ = make_client(datetime(2026, 10, 24, 14, 0, tzinfo=UTC), ticker=first)
    async with client:
        games = await client.games("G", "G2026")
        lef_ira = next(g for g in games if (g.home.code, g.away.code) == ("LEF", "IRA"))
        state = await client.period_state("G2026", lef_ira.code)
    assert state.game_over and state.ended_periods == 5


# --- highlights --------------------------------------------------------------------------------


def test_highlights_from_sport_fm_tv():
    videos = highlights.parse_feed((FIXTURES / "youtube_G_2026-10-25.xml").read_bytes())
    games = parse_round(ROUND4, 2026)
    clubs = [c for g in games for c in (g.home, g.away)]
    found = {(g.home.code, g.away.code): highlights.match(g, videos, clubs) for g in games}
    assert found[("PBC", "AEK")].title.startswith("ΠΑΟΚ – ΑΕΚ 65-62")
    assert found[("LEF", "IRA")].title.startswith("Δόξα Λευκάδας Elabet – Ηρακλής 94 - 86")
    assert found[("PAO", "KAR")].title.startswith("Παναθηναϊκός AKTOR")
    assert found[("PER", "MAR")] is not None and found[("MYK", "ARI")] is not None
    # Sunday's games weren't up yet; their clips and the football highlights never match.
    assert found[("KOL", "FAL")] is None and found[("PRO", "OLY")] is None


def test_highlights_need_the_games_score():
    """Sport FM TV also posts football between clubs of the same names."""
    from dataclasses import replace

    videos = highlights.parse_feed((FIXTURES / "youtube_G_2026-10-25.xml").read_bytes())
    games = parse_round(ROUND4, 2026)
    clubs = [c for g in games for c in (g.home, g.away)]
    paok_aek = next(g for g in games if g.home.code == "PBC")
    video = highlights.match(paok_aek, videos, clubs)
    football = replace(video, title="ΠΑΟΚ – ΑΕΚ 2-1 | HIGHLIGHTS | Stoiximan Super League")
    assert not highlights.is_highlights(paok_aek, football, clubs)
    unknown = replace(paok_aek, home_score=None, away_score=None)  # schedule not updated yet
    assert not highlights.is_highlights(unknown, video, clubs)


def test_assign_same_pairing_in_order():
    client = GblClient(2026)
    base = parse_round(ROUND5, 2026)[0]
    from dataclasses import replace
    from datetime import timedelta

    game1 = replace(base, code=1)
    game2 = replace(base, code=2, tipoff=base.tipoff + timedelta(days=2))
    client._games = {1: game1, 2: game2}
    listed = [gbl.Listed(20, base.home.code, base.away.code), gbl.Listed(10, base.home.code, base.away.code)]
    client._assign(listed, game2.tipoff)  # "now" is nearer the second game
    assert client._matches == {1: 10, 2: 20}


async def test_client_match_not_set_up_reads_as_not_started():
    client, _, _ = make_client(datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    async with client:
        games = await client.games("G", "G2026")
        upcoming = next(g for g in games if g.round == 5)
        client._matches[upcoming.code] = 2999999  # listed, but answers 403
        assert await client.header("G2026", upcoming.code) is None
        assert (await client.period_state("G2026", upcoming.code)).current_period == 0


async def test_client_lookup_skips_a_match_not_set_up():
    # The newest known match answers 403; an older one still lists the round.
    first = TICKER.split("<\\/li>")[0] + "<\\/li>"
    client, _, _ = make_client(datetime(2026, 10, 24, 14, 0, tzinfo=UTC), ticker=first)
    async with client:
        games = await client.games("G", "G2026")
        kol_fal = next(g for g in games if g.home.code == "KOL")
        client._matches[kol_fal.code] = 2999999  # newer than MYK-ARI's 2902699, answers 403
        lef_ira = next(g for g in games if (g.home.code, g.away.code) == ("LEF", "IRA"))
        client._matches.pop(lef_ira.code, None)
        client._discovered_at = float("-inf")
        assert (await client.period_state("G2026", lef_ira.code)).game_over


async def test_client_finds_matches_after_a_restart_for_the_results():
    # Restarted long after the games: the nightly results still need their box scores.
    client, _, _ = make_client(datetime(2026, 10, 24, 21, 30, tzinfo=UTC))
    async with client:
        await client.games("G", "G2026")
        box = await client.boxscore("G2026", 705268768)  # tipped off 9.5 h earlier
    assert box is not None and box.home.points == 72


async def test_client_schedule_survives_a_failing_lookup(monkeypatch):
    client, _, _ = make_client(datetime(2026, 10, 24, 12, 30, tzinfo=UTC))

    async def broken(force: bool = False) -> None:
        raise AttributeError("unexpected feed shape")

    monkeypatch.setattr(client, "_discover", broken)
    async with client:
        assert len(await client.games("G", "G2026")) == 14
