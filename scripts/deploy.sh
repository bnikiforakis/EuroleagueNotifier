#!/usr/bin/env bash
# Deploy the latest pushed code to the Raspberry Pi.
#
# Usage (from the Mac):   scripts/deploy.sh            # both, PiButler first
#                         scripts/deploy.sh pibutler   # or: notifier
#
# Needs `ssh pi` to work (see docs/DEPLOY.md). PI_HOST overrides the host alias.
set -euo pipefail

HOST="${PI_HOST:-pi}"
case "${1:-all}" in
  all) REPOS=(PiButler EuroleagueNotifier) ;;
  pibutler) REPOS=(PiButler) ;;
  notifier) REPOS=(EuroleagueNotifier) ;;
  *) echo "usage: $0 [all|pibutler|notifier]" >&2; exit 2 ;;
esac

for repo in "${REPOS[@]}"; do
  echo "== $repo"
  # Don't ship a red build: check the latest CI run on GitHub first.
  ci=$(gh run list -R "bnikiforakis/$repo" -L 1 --json conclusion,status --jq '.[0] | .status + "/" + .conclusion')
  if [[ "$ci" != "completed/success" ]]; then
    echo "   CI is '$ci' for $repo: wait for it to pass (gh run watch), or fix it first." >&2
    exit 1
  fi
  # shellcheck disable=SC2029  # $repo is meant to expand locally
  ssh "$HOST" "set -e
    cd ~/apps/$repo
    before=\$(git rev-parse --short HEAD)
    git pull --ff-only -q
    after=\$(git rev-parse --short HEAD)
    echo \"   code: \$before -> \$after\"
    docker compose up -d --build --quiet-pull 2>&1 | grep -E 'Started|Recreated|Running|Error' || true
    for service in \$(docker compose config --services); do
      for _ in \$(seq 1 30); do
        health=\$(docker inspect -f '{{.State.Health.Status}}' \"\$service\" 2>/dev/null || echo missing)
        [ \"\$health\" = healthy ] && break
        sleep 5
      done
      echo \"   \$service: \$health\"
      [ \"\$health\" = healthy ] || { docker compose logs --tail 30 \"\$service\"; exit 1; }
    done"
done
echo "Deployed."
