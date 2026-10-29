#!/usr/bin/env bash
# Docker-Dashboard auf dem Docker-Server installieren. Einmalig als root:
#
#   git clone https://github.com/stabmaster1985/docker.git docker-dashboard && sudo ./docker-dashboard/installieren.sh
#
# Danach laufen Updates per Klick in der App (Update-Dienst, siehe updater/). Erneut ausführen
# repariert eine Installation: Container neu bauen und starten, Update-Dienst einrichten.
set -euo pipefail

if [ "$(id -u)" != "0" ]; then echo "Bitte als root ausführen: sudo $0" >&2; exit 1; fi
for cmd in git docker; do
  command -v "$cmd" >/dev/null || { echo "FEHLER: $cmd fehlt auf diesem Server." >&2; exit 1; }
done
if docker compose version >/dev/null 2>&1; then DC="docker compose"
elif command -v docker-compose >/dev/null; then DC="docker-compose"
else echo "FEHLER: docker compose fehlt." >&2; exit 1; fi

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
[ -d .git ] && [ -f docker-compose.yml ] || { echo "FEHLER: $DIR ist kein Klon des Dashboards." >&2; exit 1; }

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Es gab noch keine .env – sie wurde aus .env.example angelegt."
  echo "Bitte jetzt mindestens DASHBOARD_PASSWORD (und am besten BIND_ADDRESS) eintragen:"
  echo "  nano $DIR/.env"
  echo "und danach dieses Skript erneut starten."
  exit 1
fi
if grep -qE '^DASHBOARD_PASSWORD=(bitte-aendern)?$' .env; then
  echo "FEHLER: Bitte zuerst DASHBOARD_PASSWORD in $DIR/.env setzen." >&2
  exit 1
fi

echo "== Datenordner $DIR/data (Container läuft als Benutzer 10001)"
mkdir -p data/updater/auftraege data/updater/jobs
chown -R 10001:10001 data

export GIT_COMMIT GIT_BRANCH GIT_DATE
GIT_COMMIT="$(git rev-parse HEAD)"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
GIT_DATE="$(git show -s --format=%cI HEAD)"
echo "== Baue und starte $(git log -1 --format='%h %s')"
$DC up -d --build --remove-orphans

STATUS=""
for _ in $(seq 1 45); do
  ID="$($DC ps -q dashboard 2>/dev/null | head -n 1)"
  STATUS="$(docker inspect -f '{{.State.Health.Status}}' "$ID" 2>/dev/null || echo fehlt)"
  [ "$STATUS" = "healthy" ] && break
  sleep 2
done
if [ "$STATUS" != "healthy" ]; then
  echo "FEHLER: Dashboard startet nicht (Status: $STATUS)." >&2
  $DC logs --tail 50 >&2
  exit 1
fi

if command -v systemctl >/dev/null; then
  echo "== Update-Dienst einrichten (Updates per Klick in der App)"
  ./updater/install.sh
else
  echo "Hinweis: Ohne systemd kein Update-Dienst – Updates dann mit: git pull && $DC up -d --build"
fi

PORT="$(sed -n 's/^DASHBOARD_PORT=//p' .env | tail -n 1)"
BIND="$(sed -n 's/^BIND_ADDRESS=//p' .env | tail -n 1)"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$BIND" ] && [ "$BIND" != "0.0.0.0" ] && IP="$BIND"
echo
echo "Fertig. Oberfläche: http://${IP:-<server>}:${PORT:-8080}"
