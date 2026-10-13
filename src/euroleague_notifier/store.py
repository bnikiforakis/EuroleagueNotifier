"""Which notifications have already been sent, and each game's highlights video (SQLite).

Keys look like ``E2026_32:reminder``, ``E2026_32:p2``, ``E2026_32:final``, ``schedule:2026-10-08``.
A key is recorded *after* PiButler accepted the notification; PiButler dedupes on the same key,
so a crash in between can't produce a duplicate (ADR-006).
"""

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_events (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS highlights (
    game_id  TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    found_at TEXT NOT NULL
);
"""


class EventStore:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: str) -> "EventStore":
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def has(self, key: str) -> bool:
        async with self.conn.execute("SELECT 1 FROM sent_events WHERE key = ?", (key,)) as cur:
            return await cur.fetchone() is not None

    async def sent_at(self, key: str) -> datetime | None:
        async with self.conn.execute("SELECT sent_at FROM sent_events WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    async def highlight(self, game_id: str) -> tuple[str, datetime] | None:
        """The game's highlights video id and when it was found."""
        async with self.conn.execute(
            "SELECT video_id, found_at FROM highlights WHERE game_id = ?", (game_id,)
        ) as cur:
            row = await cur.fetchone()
        return (row[0], datetime.fromisoformat(row[1])) if row else None

    async def add_highlight(self, game_id: str, video_id: str, at: datetime | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO highlights (game_id, video_id, found_at) VALUES (?, ?, ?)"
            " ON CONFLICT (game_id) DO NOTHING",
            (game_id, video_id, (at or datetime.now(UTC)).isoformat(timespec="seconds")),
        )
        await self.conn.commit()

    async def add(self, key: str, at: datetime | None = None) -> None:
        await self.conn.execute(
            "INSERT INTO sent_events (key, sent_at) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
            (key, (at or datetime.now(UTC)).isoformat(timespec="seconds")),
        )
        await self.conn.commit()

    async def last_period(self, game_id: str) -> int:
        """Highest period whose report was sent for this game (0 if none)."""
        prefix = f"{game_id}:p"
        async with self.conn.execute(
            "SELECT key FROM sent_events WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        ) as cur:
            suffixes = [row[0][len(prefix) :] async for row in cur]
        return max((int(s) for s in suffixes if s.isascii() and s.isdigit()), default=0)
