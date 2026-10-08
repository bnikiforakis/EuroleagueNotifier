# Deploying updates to the Pi

You work on the Mac, push to GitHub, and the Pi runs whatever is on `main`.

## The short version

```sh
git push                     # from the Mac, in the repo you changed
scripts/deploy.sh            # from the Mac, in EuroleagueNotifier: deploys both, PiButler first
```

The script checks that CI passed, pulls on the Pi, rebuilds and restarts only what changed, and
waits until the containers report healthy. If something goes wrong it prints the logs and stops.
Use `scripts/deploy.sh pibutler` or `scripts/deploy.sh notifier` to deploy just one.

## By hand (what the script does)

```sh
ssh pi
cd ~/apps/PiButler            # or ~/apps/EuroleagueNotifier
git pull
docker compose up -d --build
docker compose ps             # wait until it says (healthy), about 20 s
docker compose logs --tail 20
exit
```

`up -d --build` rebuilds the image from the new code and replaces the running container. If
nothing changed, nothing restarts. Data lives in Docker volumes, so it survives every deploy.

## Rules of thumb

- **Wait for CI to pass** before deploying (`gh run watch` on the Mac). The script enforces this.
- **PiButler first** when both repos changed. EuroleagueNotifier registers with it on start-up and
  retries until PiButler is back, so the order matters little, but this is the cleanest.
- **Database changes** need no extra step: PiButler applies new migrations on start-up.
- **New settings in `.env.example`** are the one manual step. Add them to the Pi's
  `~/apps/<repo>/.env` (`nano ~/apps/<repo>/.env`) before deploying, or the defaults apply.
- **A deploy restarts the service for a few seconds.** Messages aren't lost: Telegram keeps users'
  messages, and EuroleagueNotifier resumes live games from where it stopped without duplicates.
  Still, prefer deploying between games.
- **Never start the Mac's containers while the Pi is running.** One bot token allows only one
  running PiButler.

## Rolling back

If an update misbehaves, go back to the previous commit on the Pi:

```sh
ssh pi
cd ~/apps/EuroleagueNotifier             # or PiButler
git log --oneline -5                     # find the last good commit
git checkout <good-commit>               # e.g. git checkout 07976b9
docker compose up -d --build
```

Fix the problem on the Mac and push. Then on the Pi run `git checkout main && git pull` followed
by `docker compose up -d --build` to get back on track.

## Useful checks

```sh
ssh pi 'docker ps'                                          # both (healthy)?
ssh pi 'docker stats --no-stream'                           # memory / CPU
ssh pi 'docker logs --since 10m euroleague-notifier'        # what it did recently
ssh pi 'docker logs --since 10m pibutler | grep -v aiogram'
```
