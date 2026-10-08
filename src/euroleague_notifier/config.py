"""Settings loaded from environment variables (see .env.example)."""

import os
from dataclasses import dataclass
from datetime import time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    pibutler_url: str
    pibutler_api_key: str
    project_id: str = "euroleague-notifier"
    competitions: tuple[str, ...] = ("E",)
    season_year: int = 2026
    reminder_minutes: int = 30
    poll_seconds: int = 45
    digest_time: time = time(12, 0)
    digest_timezone: str = "Europe/Athens"
    db_path: str = "/data/euroleague_notifier.db"
    log_level: str = "INFO"

    def season(self, competition: str) -> str:
        return f"{competition}{self.season_year}"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    missing = [k for k in ("PIBUTLER_URL", "PIBUTLER_API_KEY") if not env.get(k)]
    if missing:
        raise SystemExit(f"Missing required settings: {', '.join(missing)}")
    settings = Settings(
        pibutler_url=env["PIBUTLER_URL"].rstrip("/"),
        pibutler_api_key=env["PIBUTLER_API_KEY"],
        project_id=env.get("PIBUTLER_PROJECT_ID", "euroleague-notifier"),
        competitions=tuple(c.strip().upper() for c in env.get("COMPETITIONS", "E").split(",") if c.strip()),
        season_year=int(env.get("SEASON_YEAR", "2026")),
        reminder_minutes=int(env.get("REMINDER_MINUTES", "30")),
        poll_seconds=int(env.get("POLL_SECONDS", "45")),
        digest_time=time.fromisoformat(env.get("DIGEST_TIME", "12:00")),
        digest_timezone=env.get("DIGEST_TIMEZONE", "Europe/Athens"),
        db_path=env.get("DB_PATH", "/data/euroleague_notifier.db"),
        log_level=env.get("LOG_LEVEL", "INFO"),
    )
    ZoneInfo(settings.digest_timezone)  # fail fast on a typo
    return settings
