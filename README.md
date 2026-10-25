# EuroleagueNotifier

Basketball notifications in Telegram for EuroLeague, EuroCup and the Greek Basket League (GBL):
upcoming games, quarter reports, and full-game reports with stats, for the teams you follow. A
while after each game, its YouTube highlights are linked too.

Each competition runs as its own service with its own `/settings` page and live-scores command
(`/score`, `/eurocup`, `/gbl`). A club that plays in several of them (say Panathinaikos in the
EuroLeague and the GBL) is auto-followed in all of them once you pick it in one.

GBL data comes from the league's site ([esake.gr](https://www.esake.gr)) for the schedule and
FIBA LiveStats for live scores and box scores; its highlights are the broadcaster's (Sport FM TV).

It runs 24/7 on a Raspberry Pi and delivers messages through
[PiButler](https://github.com/bnikiforakis/PiButler), an invite-only Telegram bot gateway.

> Work in progress. To run it on a Raspberry Pi, see [docs/PI_SETUP.md](docs/PI_SETUP.md);
> to ship updates to it, see [docs/DEPLOY.md](docs/DEPLOY.md).

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

## License

[MIT](LICENSE)
