"""YouTube highlights: matching real channel titles to games, and the hunter's timing (ADR-018)."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_tracker import GAME, FakeEuroleague, events, make  # noqa: F401 (fixture)

from euroleague_notifier.api import ApiError, EuroleagueClient, parse_games
from euroleague_notifier.highlights import CHANNELS, Video, match, parse_feed
from euroleague_notifier.models import Club
from euroleague_notifier.tracker import HIGHLIGHTS_GIVE_UP, HIGHLIGHTS_RETRY, HIGHLIGHTS_TTL, MAX_GAME_LENGTH

FIXTURES = Path(__file__).parent / "fixtures"


def games(competition: str):
    return parse_games(json.loads((FIXTURES / f"v2_games_{competition}2026_highlights.json").read_text()))


def feed(name: str) -> list[Video]:
    return parse_feed((FIXTURES / name).read_bytes())


# --- matching --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("competition", "feed_name", "expected"),
    [
        (
            "E",
            "youtube_E_round4.xml",  # EuroLeague Round 4 plus everything else the channel posted that week
            {
                31: "L-K73HNYGtw",  # "ALL-OUT IN THE FRENCH DERBY! | Paris Basketball - LDLC ASVEL | R4 …"
                32: "Sms2JqO1FH0",
                33: "6uL3DTk-Mrk",
                34: "0Jf1UfVWYUQ",
                35: "n6ugkuGRXD0",
                36: "PtdYeWFvq7o",  # not "THE INTENSITY NEVER STOPS! | Panathinaikos AKTOR - Fenerbahce …"
                37: "2kMCaWkCCK8",
                38: "T2HMK28eMC8",  # "Kosner Baskonia - Besiktas | R4 Basketball Highlights"
                39: "-3F35ggXzHI",
                40: "JyRGLVO_EXY",  # "Olympiacos - Anadolu Efes": shortened names
            },
        ),
        (
            "U",
            "youtube_U_2026-10-13.xml",  # EuroCup: full names, "Round N", the odd typo
            {
                17: "M8pPl81bpEM",
                18: "SrOUfeg_g7c",
                19: "34bKTiagBSM",
                21: "SyLCQFm5muI",
                23: "Ua8TTcSy09w",
                25: "U5TUQ5NI0HI",
                26: "0W72LXcb0Pc",
                29: "JtolGGDrRpU",
                30: "87wPdHXZYI8",
                31: "DRXKitE2R9I",  # "Kids&Us Manresa - radiopharm ulm"
                43: "gegXga57jsk",
                45: "DaghJT80AWY",
                47: "npQbZe6pUao",
            },
        ),
    ],
)
def test_every_game_matches_its_own_video_and_nothing_else(competition, feed_name, expected):
    videos = feed(feed_name)
    schedule = games(competition)
    clubs = {c.code: c for g in schedule for c in (g.home, g.away)}.values()
    found = {g.code: v.id for g in schedule if (v := match(g, videos, clubs))}
    assert found == expected  # no clip, preview, top-10 or other round matches anything


def test_a_video_published_before_tip_off_is_not_the_highlights():
    (paris,) = [g for g in games("E") if g.code == 31]
    videos = feed("youtube_E_round4.xml")
    assert match(replace(paris, tipoff=datetime(2026, 10, 8, tzinfo=UTC)), videos) is None


def club(code: str, name: str) -> Club:
    return Club(code, name, name)


SHARED = [  # one EuroCup round where two pairs of clubs share their city
    club("PAO", "PAOK Thessaloniki"),
    club("ROM", "Roma Basketball"),
    club("ARI", "Aris Thessaloniki"),
    club("MAX", "Maxima Roma"),
]


def test_a_shared_city_does_not_match_another_game():
    paok, roma, aris, maxima = SHARED
    (paris,) = [g for g in games("E") if g.code == 31]
    aris_maxima = replace(paris, home=aris, away=maxima)
    paok_roma = replace(paris, home=paok, away=roma)
    video = Video(
        "abcdefghijk", "PAOK Thessaloniki - Roma Basketball | Round 4", paris.tipoff + timedelta(hours=3)
    )
    assert match(paok_roma, [video], SHARED) == video
    assert match(aris_maxima, [video], SHARED) is None
    short = Video("abcdefghij2", "Aris - Maxima Roma | R4 Highlights", video.published)
    assert match(aris_maxima, [short], SHARED) == short and match(paok_roma, [short], SHARED) is None


def test_playoff_titles_say_highlights_instead_of_a_round():
    (paris,) = [g for g in games("E") if g.code == 31]
    after = paris.tipoff + timedelta(hours=3)
    video = Video("abcdefghijk", "Paris Basketball - LDLC ASVEL | Playoffs Game 2 Highlights 2026-27", after)
    assert match(replace(paris, round=None), [video]) == video
    clip = Video("abcdefghij2", "Paris Basketball - LDLC ASVEL | LAST 5 MINUTES", after)
    assert match(replace(paris, round=None), [clip]) is None


def test_parse_feed_skips_broken_entries():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
      <entry><yt:videoId>abcdefghijk</yt:videoId><title>A - B | R1</title>
        <published>2026-10-08T18:00:00+00:00</published></entry>
      <entry><yt:videoId>bad id</yt:videoId><title>x</title>
        <published>2026-10-08T18:00:00+00:00</published></entry>
      <entry><yt:videoId>abcdefghij2</yt:videoId><title>x</title><published>yesterday</published></entry>
      <entry><yt:videoId>abcdefghij3</yt:videoId><published>2026-10-08T18:00:00+00:00</published></entry>
    </feed>"""
    assert [v.id for v in parse_feed(xml)] == ["abcdefghijk"]


async def test_client_reads_a_channel_feed():
    body = (FIXTURES / "youtube_U_2026-10-13.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["channel_id"] == CHANNELS["U"]
        return httpx.Response(200, content=body)

    async with EuroleagueClient(transport=httpx.MockTransport(handler), backoff=0) as client:
        assert len(await client.videos(CHANNELS["U"])) == 15


async def test_client_rejects_a_broken_feed():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>consent</html"))
    async with EuroleagueClient(transport=transport, backoff=0) as client:
        with pytest.raises(ApiError):
            await client.videos(CHANNELS["E"])


# --- hunting ---------------------------------------------------------------------------------

ENDED = GAME.tipoff + timedelta(hours=2)
HIGHLIGHTS = Video(
    "abcdefghijk",
    f"{GAME.home.name} - {GAME.away.name} | R{GAME.round} Highlights 2026-27",
    ENDED + timedelta(minutes=70),
)
CLIP = Video("zyxwvutsrqp", f"BIG RUN! | {GAME.home.name} - {GAME.away.name} | LAST 5 MINUTES", ENDED)


class Feed(FakeEuroleague):
    def __init__(self):
        super().__init__([])
        self.videos_list: list[Video] = []
        self.feed_calls = 0
        self.fail_feed = False

    async def videos(self, channel_id):
        assert channel_id == CHANNELS["E"]
        self.feed_calls += 1
        if self.fail_feed:
            raise ApiError("down")
        return self.videos_list


async def hunter(events, at: datetime):  # noqa: F811 (fixture)
    notifier, butler, clock = make(events, [], start=at)
    notifier.el = feed_api = Feed()
    await notifier.refresh_schedule()
    await events.add(f"{GAME.identifier}:final", ENDED)
    return notifier, butler, clock, feed_api


async def test_first_look_an_hour_after_the_final_then_every_15_minutes(events):  # noqa: F811
    notifier, butler, clock, api = await hunter(events, ENDED + timedelta(minutes=59))
    await notifier.hunt_once()
    assert api.feed_calls == 0  # too early
    clock.t = ENDED + timedelta(hours=1)
    api.videos_list = [CLIP]
    await notifier.hunt_once()
    assert api.feed_calls == 1 and await events.highlight(GAME.identifier) is None
    clock.t += HIGHLIGHTS_RETRY - timedelta(minutes=1)
    await notifier.hunt_once()
    assert api.feed_calls == 1  # not yet time to retry
    clock.t += timedelta(minutes=1)
    api.videos_list = [CLIP, HIGHLIGHTS]
    await notifier.hunt_once()
    assert api.feed_calls == 2 and (await events.highlight(GAME.identifier))[0] == HIGHLIGHTS.id
    (msg,) = butler.sent
    assert msg["key"] == f"{GAME.identifier}:highlights" and msg["silent"]
    assert msg["audience"] == {"topic": GAME.identifier, "following": True}  # followers only
    assert msg["buttons"] == [[{"label": "▶️ Watch on YouTube", "url": HIGHLIGHTS.url}]]
    assert "Highlights" in msg["text"]
    clock.t += timedelta(hours=1)
    await notifier.hunt_once()
    assert api.feed_calls == 2 and len(butler.sent) == 1  # done


async def test_gives_up_after_twelve_hours(events):  # noqa: F811
    notifier, butler, _, api = await hunter(events, ENDED + HIGHLIGHTS_GIVE_UP)
    api.videos_list = [HIGHLIGHTS]
    await notifier.hunt_once()
    assert api.feed_calls == 0 and butler.sent == []


async def test_feed_errors_are_retried_later(events):  # noqa: F811
    notifier, butler, clock, api = await hunter(events, ENDED + timedelta(hours=1))
    api.fail_feed = True
    await notifier.hunt_once()
    api.fail_feed, api.videos_list = False, [HIGHLIGHTS]
    await notifier.hunt_once()
    assert api.feed_calls == 1  # waits for the retry interval
    clock.t += HIGHLIGHTS_RETRY
    await notifier.hunt_once()
    assert (await events.highlight(GAME.identifier))[0] == HIGHLIGHTS.id and len(butler.sent) == 1


async def test_found_video_is_announced_after_pibutler_recovers_or_a_restart(events):  # noqa: F811
    notifier, butler, clock, api = await hunter(events, ENDED + timedelta(hours=1))
    api.videos_list = [HIGHLIGHTS]
    butler.fail_next = 1
    await notifier.hunt_once()
    assert (await events.highlight(GAME.identifier))[0] == HIGHLIGHTS.id and butler.sent == []
    # a fresh notifier (restart) on the same database announces it without searching again
    restarted, butler2, _, api2 = await hunter(events, clock.t + timedelta(minutes=1))
    await restarted.hunt_once()
    assert api2.feed_calls == 0 and butler2.keys == [f"{GAME.identifier}:highlights"]
    await restarted.hunt_once()
    assert len(butler2.sent) == 1


async def test_unfinished_games_and_other_competitions_are_not_searched(events):  # noqa: F811
    notifier, _, _, api = await hunter(events, ENDED + timedelta(hours=1))
    await events.conn.execute("DELETE FROM sent_events")
    await notifier.hunt_once()
    assert api.feed_calls == 0  # no final yet (and the schedule doesn't say played)
    await events.add(f"{GAME.identifier}:final", ENDED)
    notifier._games = {GAME.identifier: ("X", GAME)}  # a competition without a channel
    await notifier.hunt_once()
    assert api.feed_calls == 0


async def test_a_late_announcement_goes_stale(events):  # noqa: F811
    notifier, butler, clock, api = await hunter(events, ENDED + timedelta(hours=1))
    api.videos_list = [HIGHLIGHTS]
    butler.fail_next = 1
    await notifier.hunt_once()  # found, but PiButler is down
    clock.t += HIGHLIGHTS_TTL
    await notifier.hunt_once()
    assert butler.sent == [] and (await events.highlight(GAME.identifier)) is not None  # card still has it


async def test_games_whose_final_was_missed_are_searched_from_the_schedule(events):  # noqa: F811
    notifier, _, _, api = await hunter(events, GAME.tipoff + MAX_GAME_LENGTH + timedelta(hours=1))
    await events.conn.execute("DELETE FROM sent_events")  # down during the game
    notifier._games = {GAME.identifier: ("E", replace(GAME, played=True))}
    api.videos_list = [replace(HIGHLIGHTS, published=GAME.tipoff + timedelta(hours=3))]
    await notifier.hunt_once()
    assert api.feed_calls == 1 and await events.highlight(GAME.identifier) is not None
