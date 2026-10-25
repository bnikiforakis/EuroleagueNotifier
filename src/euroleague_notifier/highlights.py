"""Official YouTube highlights: feed parsing and matching videos to games (ADR-018).

Each competition's channel uploads one highlights video per game, typically 35-60 min after the
final buzzer. The channel's Atom feed lists its latest 15 uploads and needs no API key.

Titles differ per channel and use names that only roughly match the API's:
- EuroLeague: ``Real Madrid - Partizan Mozzart Bet | R4 Highlights 2026-27``, sometimes after a
  headline: ``ALL-OUT IN THE FRENCH DERBY! | Paris Basketball - LDLC ASVEL | R4 Highlights 2026-27``;
- EuroCup: ``Kids&Us Manresa - radiopharm ulm | Round 2 | BKT EuroCup Basketball 2026-27`` (sic);
- GBL: the league's channel has no per-game videos, the broadcaster Sport FM TV does, in Greek and
  with the final score: ``ΠΑΟΚ – ΑΕΚ 65-62 | HIGHLIGHTS | Stoiximan GBL`` (ADR-023).

So a video matches a game when one ``|`` segment reads ``<home> - <away>``, each side naming its
club more closely than any other club of the competition, the title says "Highlights" or names the
game's round, and it was published after tip-off; a final score after the teams must be the game's
(until the schedule has it, such a video waits). That excludes previews ("Last 5 Meetings"),
late-game clips ("LAST 5 MINUTES"), earlier meetings of the same teams and other sports.
"""

import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from euroleague_notifier.models import Club, Game

CHANNELS = {
    "E": "UCGr3nR_XH9r6E5b09ZJAT9w",  # EUROLEAGUE BASKETBALL
    "U": "UCpFqOv5z4WKps64GlQS4ytw",  # EuroCup
    "G": "UCPRkLPtgLZR1jYPxcBluZJQ",  # Sport FM TV (GBL broadcaster; also posts football)
}
FEED_URL = "https://www.youtube.com/feeds/videos.xml"
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SCORE = re.compile(r"\s+(\d+)\s*[-\u2013]\s*(\d+)\s*$")  # "78-64", "94 - 86"
# Words too common in club names to tell two clubs apart.
_GENERIC = {"basket", "basketball", "bc", "fc", "kk", "club", "the", "and", "de", "of", "sc", "bk", "cb"}


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    published: datetime

    @property
    def url(self) -> str:
        return video_url(self.id)


def parse_feed(xml: str | bytes) -> list[Video]:
    """Videos in a channel's Atom feed; entries without a valid id, title or date are skipped."""
    videos = []
    for entry in ET.fromstring(xml).iter(f"{_ATOM}entry"):
        vid = (entry.findtext(f"{_YT}videoId") or "").strip()
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        published = (entry.findtext(f"{_ATOM}published") or "").strip()
        if not (_VIDEO_ID.match(vid) and title and published):
            continue
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        videos.append(Video(vid, title, when))
    return videos


def fold(text: str) -> str:
    """Lowercase without accents, in any script: "Άρης" and "ΑΡΗΣ" compare equal."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _words(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[^\W_]+", fold(text.replace("&", " and ")))
        if w not in _GENERIC and len(w) > 1
    }


def _names(club: Club) -> set[str]:
    return set().union(*(_words(n) for n in (club.name, club.short_name, *club.aliases)))


def _similarity(side: set[str], club: Club) -> float:
    names = _names(club)
    return len(side & names) / len(side | names) if side & names else 0.0


def _names_club(side: str, club: Club, others: Iterable[Club]) -> bool:
    """``side`` of a title names ``club``, and fits it better than any other club. Cities are
    shared (Thessaloniki, Tel Aviv, Roma), so one common word isn't enough on its own."""
    words = _words(side)
    score = _similarity(words, club)
    return score > 0 and all(_similarity(words, o) < score for o in others if o.code != club.code)


def _labelled(title: str, round_: int | None) -> bool:
    """Says it's highlights, or names the game's round (EuroCup titles do only that)."""
    if re.search(r"\bhighlights\b", title, re.IGNORECASE):
        return True
    return round_ is not None and re.search(rf"\b(?:R|Round\s*){round_}\b", title, re.IGNORECASE) is not None


def is_highlights(game: Game, video: Video, clubs: Iterable[Club] = ()) -> bool:
    """``clubs``: the competition's other clubs, so a video names this game's teams and no others."""
    if video.published < game.tipoff or not _labelled(video.title, game.round):
        return False
    others = list(clubs)
    for segment in video.title.split("|"):
        segment = segment.strip()
        # A title with a score (GBL) must have this game's: the same channel posts football
        # highlights between clubs of the same names ("ΠΑΟΚ – ΑΕΚ 2-1").
        if (score := _SCORE.search(segment)) and (
            (int(score[1]), int(score[2])) != (game.home_score, game.away_score)
        ):
            continue
        sides = re.split(r"\s+[-\u2013]\s+", _SCORE.sub("", segment))  # hyphen or en dash
        if (
            len(sides) == 2
            and _names_club(sides[0], game.home, others)
            and _names_club(sides[1], game.away, others)
        ):
            return True
    return False


def match(game: Game, videos: list[Video], clubs: Iterable[Club] = ()) -> Video | None:
    """The game's highlights video, the earliest if the channel posted several."""
    found = [v for v in videos if is_highlights(game, v, clubs)]
    return min(found, key=lambda v: v.published) if found else None
