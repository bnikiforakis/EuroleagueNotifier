"""Which notifications have already been sent (SQLite).

Keys look like ``E2026_32:reminder``, ``E2026_32:p2``, ``E2026_32:final``, ``digest:2026-10-08``.
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
)
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
        await conn.execute(SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self.conn.close()

    async def has(self, key: str) -> bool:
        async with self.conn.execute("SELECT 1 FROM sent_events WHERE key = ?", (key,)) as cur:
            return await cur.fetchone() is not None

    async def add(self, key: str) -> None:
        await self.conn.execute(
            "INSERT INTO sent_events (key, sent_at) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
            (key, datetime.now(UTC).isoformat(timespec="seconds")),
        )
        await self.conn.commit()

    async def last_period(self, game_id: str) -> int:
        """Highest period whose report was sent for this game (0 if none)."""
        async with self.conn.execute(
            "SELECT key FROM sent_events WHERE key LIKE ?", (f"{game_id}:p%",)
        ) as cur:
            periods = [int(row[0].rsplit(":p", 1)[1]) async for row in cur]
        return max(periods, default=0)
