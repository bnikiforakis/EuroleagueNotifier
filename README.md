# EuroleagueNotifier

EuroLeague notifications in Telegram: upcoming games, quarter reports, and full-game reports
with stats, for the teams you follow.

It runs 24/7 on a Raspberry Pi and delivers messages through
[PiButler](https://github.com/bnikiforakis/PiButler), an invite-only Telegram bot gateway.

> Work in progress. Setup instructions will follow.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

### Recording live games for test fixtures

```sh
uv run --no-project scripts/record_live.py --until 2026-10-09T23:59:00Z
```

Snapshots go to `data/recordings/` (gitignored).

## Run with Docker

```sh
cp .env.example .env   # then fill it in
docker compose up -d --build
docker compose logs -f
```

PiButler must be running first: its compose file creates the shared `pibutler-net` network.
