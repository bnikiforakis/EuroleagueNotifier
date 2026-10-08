# /// script
# requires-python = ">=3.12"
# ///
"""Record live Euroleague feeds for upcoming games, to build replay test fixtures.

Each game is polled from 10 min before tip-off until 15 min after it goes final.
A snapshot is written only when the payload changes:
    data/recordings/<season>_<game>/<feed>/<UTC timestamp>.json.gz

Usage: uv run scripts/record_live.py [--until 2026-10-09T23:59:00Z] [--competition E]
"""

import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

POLL_SECONDS = 45
PRE_GAME = timedelta(minutes=10)
POST_FINAL = timedelta(minutes=15)
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "recordings"

FEEDS = {
    "header": "https://live.euroleague.net/api/Header?gamecode={code}&seasoncode={season}",
    "playbyplay": "https://live.euroleague.net/api/PlayByPlay?gamecode={code}&seasoncode={season}",
    "boxscore": "https://live.euroleague.net/api/Boxscore?gamecode={code}&seasoncode={season}",
    "v3stats": "https://api-live.euroleague.net/v3/competitions/{comp}/seasons/{season}/games/{code}/stats",
    "feed": "https://feeds.incrowdsports.com/provider/euroleague-feeds/v2/competitions/{comp}/seasons/{season}/games/{code}",
}

log = logging.getLogger("recorder")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def now() -> datetime:
    return datetime.now(UTC)


def schedule(comp: str, season: str) -> list[dict]:
    url = f"https://api-live.euroleague.net/v2/competitions/{comp}/seasons/{season}/games"
    data = json.loads(fetch(url))
    return data.get("data", data)


async def record_game(comp: str, season: str, game: dict) -> None:
    code = game["gameCode"]
    tipoff = datetime.fromisoformat(game["utcDate"].replace("Z", "+00:00"))
    label = f"{season}_{code} {game['local']['club']['code']}-{game['road']['club']['code']}"
    game_dir = OUT_DIR / f"{season}_{code}"
    last_hash: dict[str, str] = {}
    final_at: datetime | None = None
    seen_live = False

    wait = (tipoff - PRE_GAME - now()).total_seconds()
    if wait > 0:
        log.info("%s: waiting %.0f min for tip-off %s", label, wait / 60, tipoff.isoformat())
        await asyncio.sleep(wait)
    log.info("%s: recording", label)

    while final_at is None or now() < final_at + POST_FINAL:
        ts = now().strftime("%Y%m%dT%H%M%SZ")
        for feed, pattern in FEEDS.items():
            url = pattern.format(comp=comp, season=season, code=code)
            try:
                body = await asyncio.to_thread(fetch, url)
            except Exception as exc:  # keep recording through transient errors
                log.warning("%s %s: %s", label, feed, exc)
                continue
            digest = hashlib.sha256(body).hexdigest()
            if last_hash.get(feed) == digest:
                continue
            last_hash[feed] = digest
            path = game_dir / feed / f"{ts}.json.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(body))
            if feed == "header" and body.strip():
                try:
                    h = json.loads(body)
                except ValueError:
                    continue
                log.info(
                    "%s header: Live=%s Q=%s %s-%s time=%s",
                    label,
                    h.get("Live"),
                    h.get("Quarter"),
                    h.get("ScoreA"),
                    h.get("ScoreB"),
                    h.get("RemainingPartialTime"),
                )
                seen_live |= bool(h.get("Live"))
                if seen_live and not h.get("Live") and final_at is None:
                    final_at = now()
                    log.info("%s: FINAL detected", label)
        if final_at is None and now() > tipoff + timedelta(hours=4):
            log.warning("%s: no final after 4h, giving up", label)
            return
        await asyncio.sleep(POLL_SECONDS)
    log.info("%s: done", label)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--competition", default="E")
    parser.add_argument("--season", default="E2026")
    parser.add_argument("--until", default=None, help="only games tipping off before this UTC time")
    args = parser.parse_args()

    until = now() + timedelta(days=1)
    if args.until:
        until = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    games = [
        g
        for g in schedule(args.competition, args.season)
        if not g["played"]
        and now() - timedelta(hours=3) < datetime.fromisoformat(g["utcDate"].replace("Z", "+00:00")) < until
    ]
    log.info("recording %d games", len(games))
    await asyncio.gather(*(record_game(args.competition, args.season, g) for g in games))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
