# Raspberry Pi setup

From a fresh Raspberry Pi to running bots in about 15 minutes. Tested target: Raspberry Pi 5
(2 GB) with Raspberry Pi OS Lite 64-bit (Debian 13 "trixie").

## 1. Flash and boot

1. In **Raspberry Pi Imager**, choose *Raspberry Pi OS Lite (64-bit)*. In the settings (⚙️), set a
   hostname (e.g. `pi`), a username and password, Wi-Fi if needed, and **enable SSH**.
2. Boot the Pi and connect: `ssh <user>@pi.local`.
3. Update it: `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.

## 2. Protect the SD card (recommended)

Most SD-card writes on a quiet Pi come from the OS, not from the apps.

```sh
# Keep the system journal in RAM, capped at 32 MB (it is lost on reboot)
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' | sudo tee /etc/systemd/journald.conf.d/ram.conf
sudo systemctl restart systemd-journald

# Don't write a timestamp on every file read: add noatime to the root mount
sudo sed -i 's/\(\s\/\s\+ext4\s\+\)defaults/\1defaults,noatime/' /etc/fstab
grep ' / ' /etc/fstab   # check that it now says defaults,noatime
```

Use the official 27 W USB-C power supply. Brown-outs corrupt SD cards far more often than wear does.

## 3. Install Docker

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker            # or log out and back in
docker run --rm hello-world
```

## 4. Get the code

PiButler is a private repository, so the Pi needs access to GitHub. The simplest way is the GitHub CLI:

```sh
sudo apt install -y gh git
gh auth login            # GitHub.com → HTTPS → log in with a browser code
mkdir -p ~/apps && cd ~/apps
gh repo clone bnikiforakis/PiButler
gh repo clone bnikiforakis/EuroleagueNotifier
```

## 5. Configure

```sh
cd ~/apps/PiButler && cp .env.example .env && nano .env
cd ~/apps/EuroleagueNotifier && cp .env.example .env && nano .env
```

- **PiButler** needs `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID` and `PROJECT_KEYS`
  (`euroleague-notifier:<a long random key>`, e.g. from `openssl rand -base64 32`).
- **EuroleagueNotifier** needs `PIBUTLER_API_KEY` set to the same key, and `PIBUTLER_URL=http://pibutler:8080`.
- Make both files private: `chmod 600 .env`.

A bot token can only be used by **one** running PiButler at a time. Stop any other copy first
(for example on your laptop), or Telegram rejects the second one.

## 6. Start

PiButler goes first, because it creates the shared `pibutler-net` network:

```sh
cd ~/apps/PiButler && docker compose up -d --build
cd ~/apps/EuroleagueNotifier && docker compose up -d --build
docker ps                       # both should become "healthy" within ~2 minutes
docker compose logs -f          # in either folder
```

Containers restart on their own after a crash or a reboot (`restart: unless-stopped`).

## 7. Updating

```sh
cd ~/apps/PiButler && git pull && docker compose up -d --build
cd ~/apps/EuroleagueNotifier && git pull && docker compose up -d --build
docker image prune -f
```

## 8. Backups

Each service keeps a small SQLite file in a Docker volume. To copy consistent snapshots into `~/backups`:

```sh
mkdir -p ~/backups
for svc in pibutler:pibutler euroleague-notifier:euroleague_notifier; do
  name=${svc%%:*}; db=${svc##*:}
  docker exec "$name" python -c "import sqlite3; sqlite3.connect('/data/$db.db').backup(sqlite3.connect('/tmp/$db.db'))"
  docker exec "$name" sh -c "cat /tmp/$db.db && rm /tmp/$db.db" > ~/backups/"$db-$(date +%F).db"
done
```

Copy `~/backups` off the Pi every so often, or run it nightly from `crontab -e`.

## Resource use

Measured in arm64 containers with nothing happening:

| Service | RAM | CPU |
|---|---|---|
| PiButler | ~130 MB (limit 256 MB) | ~0% |
| EuroleagueNotifier | ~30–45 MB (limit 128 MB) | ~0%, short bursts every ~45 s during live games |

## Memory limits (Raspberry Pi 5)

Raspberry Pi OS boots with `cgroup_disable=memory`, so Docker can't enforce the containers'
`mem_limit` (`docker stats` shows 0 B). To enable it:

```sh
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
sudo sed -i '1 s/$/ cgroup_enable=memory/' /boot/firmware/cmdline.txt   # must stay ONE line
cat /boot/firmware/cmdline.txt
sudo reboot
```

Then recreate the containers once so the limits are applied:
`docker compose up -d --force-recreate` in each project folder (PiButler first).
