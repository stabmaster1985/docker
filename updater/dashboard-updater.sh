#!/usr/bin/env bash
# shellcheck disable=SC1111  # deutsche Anführungszeichen in Meldungen sind gewollt
# Docker-Dashboard – Update-Dienst auf dem Docker-Server (nicht im Container).
#
# Die App legt Aufträge als Dateien in data/updater/auftraege/ ab; dieses Skript arbeitet sie ab
# und schreibt den Stand nach data/updater/status.json (Dashboard) bzw. data/updater/jobs/<id>.json
# (Container-Updates). Ausgelöst per systemd (siehe install.sh): sofort bei einem Auftrag und
# stündlich, um neue Dashboard-Versionen im Hintergrund vorzubereiten.
#
#   dashboard-updater.sh handle    Aufträge abarbeiten – ohne Auftrag: vorbereiten
#   dashboard-updater.sh prepare   neue Dashboard-Version holen und fertig bauen (ohne umzuschalten)
#   dashboard-updater.sh apply     vorbereitete Version starten (bei Fehler zurück auf die alte)
#   dashboard-updater.sh status    nur den Status schreiben
set -euo pipefail

# Das Skript ersetzt sich beim Umschalten selbst (git) – deshalb aus einer Kopie laufen.
if [ -z "${DASH_UPDATER_COPY:-}" ]; then
  DASH_DIR="${DASH_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
  tmp="$(mktemp)"
  cp "$0" "$tmp"
  DASH_UPDATER_COPY=1 DASH_DIR="$DASH_DIR" exec bash "$tmp" "$@"
fi
trap 'rm -f "$0"' EXIT

DIENST_VERSION=1
REPO="$DASH_DIR"
BRANCH="${DASH_BRANCH:-main}"
IMAGE="${DASH_IMAGE:-docker-dashboard}"
SERVICE="dashboard"
APP_UID="${DASH_UID:-10001}"
HEALTH_WAIT="${DASH_HEALTH_WAIT:-90}"
DIR="$REPO/data/updater"
STATUS="$DIR/status.json"
AUFTRAEGE="$DIR/auftraege"
JOBS="$DIR/jobs"
READY="$DIR/bereit"
LOG="$DIR/update.log"

mkdir -p "$DIR" "$AUFTRAEGE" "$JOBS"
if [ "$(id -u)" = "0" ]; then
  # Die App (UID 10001) legt Aufträge ab und liest den Stand.
  chown "$APP_UID:$APP_UID" "$REPO/data" "$DIR" "$AUFTRAEGE" "$JOBS" 2>/dev/null || true
fi
cd "$REPO"

exec 9>"$DIR/.lock"
if command -v flock >/dev/null; then flock -w 900 9 || { echo "Update-Dienst ist beschäftigt"; exit 1; }; fi

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

# Protokoll klein halten
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 1048576 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG" >&2; }
esc() { printf '%s' "$1" | tr -d '\000-\010\013-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\t' '  '; }
json_get() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" <<<"$2" | head -n 1; }

commits_json() {  # Commits zwischen $1 und $2 als JSON-Liste (neueste zuerst)
  local out="" first=1 sha msg
  while IFS=$'\t' read -r sha msg; do
    [ -n "$sha" ] || continue
    [ $first = 1 ] || out="$out,"
    out="$out{\"commit\":\"$sha\",\"message\":\"$(esc "$msg")\"}"
    first=0
  done < <(git log --format='%H%x09%s' -n 30 "$1..$2" 2>/dev/null || true)
  printf '[%s]' "$out"
}

write_status() {  # write_status phase meldung [bereit-sha]
  local phase="$1" msg="$2" ready="${3:-}" cur commits="[]" tail=""
  cur="$(git rev-parse HEAD 2>/dev/null || echo "")"
  [ -n "$ready" ] && commits="$(commits_json "$cur" "$ready")"
  [ "$phase" = "fehler" ] && tail="$(tail -n 15 "$LOG" 2>/dev/null || true)"
  cat >"$STATUS.tmp" <<EOF
{"phase":"$phase","meldung":"$(esc "$msg")","aktuell":"$cur","bereit":"$ready","branch":"$BRANCH","commits":$commits,"zeit":"$(date -u +%FT%TZ)","dienst_version":$DIENST_VERSION,"log":"$(esc "$tail")"}
EOF
  chmod 644 "$STATUS.tmp"
  mv "$STATUS.tmp" "$STATUS"
}

write_job() {  # write_job id phase meldung container name ref benutzer
  local f="$JOBS/$1.json"
  cat >"$f.tmp" <<EOF
{"id":"$1","phase":"$2","meldung":"$(esc "$3")","container":"$4","name":"$(esc "$5")","ref":"$(esc "$6")","benutzer":"$(esc "$7")","zeit":"$(date -u +%FT%TZ)"}
EOF
  chmod 644 "$f.tmp"
  mv "$f.tmp" "$f"
}

prepared() { [ -f "$READY" ] && cat "$READY" || true; }

# Läuft der Container (mit Healthcheck: „healthy“, ohne: 10 s stabil „running“)?
container_ok() {
  local id="$1" s="" vorher=""
  for _ in $(seq 1 "$((HEALTH_WAIT / 2))"); do
    s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id" 2>/dev/null || echo fehlt)"
    case "$s" in
      healthy) return 0 ;;
      running)
        if [ "$vorher" = "running" ]; then return 0; fi
        vorher="running"; sleep 8 ;;
      *) vorher="" ;;
    esac
    sleep 2
  done
  log "Container-Status: $s"
  return 1
}

dashboard_id() { $DC ps -q "$SERVICE" 2>/dev/null | head -n 1; }

# ------------------------------------------------------------ Dashboard selbst
prepare() {
  local cur new
  write_status "vorbereiten" "Hole neue Version von GitHub …"
  log "== Vorbereiten"
  if ! git fetch -q origin "$BRANCH" >>"$LOG" 2>&1; then
    write_status "fehler" "GitHub nicht erreichbar (git fetch fehlgeschlagen)"; return 1
  fi
  cur="$(git rev-parse HEAD)"
  new="$(git rev-parse "origin/$BRANCH")"
  if [ "$cur" = "$new" ]; then
    rm -f "$READY"
    write_status "aktuell" "Die neueste Version läuft bereits."; return 0
  fi
  if ! git merge-base --is-ancestor "$cur" "$new"; then
    write_status "fehler" "Lokaler Stand weicht von GitHub ab – bitte auf dem Server prüfen (git status)."; return 1
  fi
  if [ "$(prepared)" = "$new" ] && docker image inspect "$IMAGE:bereit" >/dev/null 2>&1; then
    write_status "bereit" "Version ${new:0:7} ist vorbereitet." "$new"; return 0
  fi
  write_status "vorbereiten" "Baue Version ${new:0:7} …"
  log "Baue $new"
  if ! git archive --format=tar "$new" | docker build -q \
      --build-arg GIT_COMMIT="$new" --build-arg GIT_BRANCH="$BRANCH" \
      --build-arg GIT_DATE="$(git show -s --format=%cI "$new")" \
      -t "$IMAGE:bereit" - >>"$LOG" 2>&1; then
    rm -f "$READY"
    write_status "fehler" "Bauen der neuen Version fehlgeschlagen – die laufende Version bleibt."; return 1
  fi
  # Neues Image des Socket-Proxys gleich mit holen (nur herunterladen, nichts neu starten).
  $DC pull -q socket-proxy >>"$LOG" 2>&1 || log "Hinweis: Socket-Proxy-Image nicht geladen"
  echo "$new" >"$READY"
  log "Bereit: $new"
  write_status "bereit" "Version ${new:0:7} ist vorbereitet." "$new"
}

apply() {
  local new cur id
  new="$(prepared)"
  if [ -z "$new" ] || ! docker image inspect "$IMAGE:bereit" >/dev/null 2>&1; then
    write_status "fehler" "Es ist keine Version vorbereitet."; return 1
  fi
  cur="$(git rev-parse HEAD)"
  write_status "umschalten" "Schalte auf ${new:0:7} um …" "$new"
  log "== Umschalten $cur -> $new"
  docker tag "$IMAGE:latest" "$IMAGE:vorher" 2>/dev/null || true
  if ! git merge -q --ff-only "$new" >>"$LOG" 2>&1; then
    write_status "fehler" "git merge fehlgeschlagen – nichts umgeschaltet."; return 1
  fi
  docker tag "$IMAGE:bereit" "$IMAGE:latest"
  $DC up -d --no-build --remove-orphans >>"$LOG" 2>&1 || true
  id="$(dashboard_id)"
  if [ -n "$id" ] && container_ok "$id"; then
    rm -f "$READY"
    docker image rm "$IMAGE:bereit" >/dev/null 2>&1 || true
    docker image prune -f >/dev/null 2>&1 || true
    log "Fertig: $new läuft"
    # Neue Fassung der systemd-Units übernehmen
    if [ "$(id -u)" = "0" ] && [ -f "$REPO/updater/install.sh" ]; then
      bash "$REPO/updater/install.sh" --units >>"$LOG" 2>&1 || log "Hinweis: systemd-Units nicht aktualisiert"
    fi
    write_status "fertig" "Version ${new:0:7} läuft."
    return 0
  fi
  log "Neue Version startet nicht – zurück auf $cur"
  $DC logs --tail 30 "$SERVICE" >>"$LOG" 2>&1 || true
  git reset -q --hard "$cur" >>"$LOG" 2>&1
  docker tag "$IMAGE:vorher" "$IMAGE:latest" 2>/dev/null || true
  $DC up -d --no-build --remove-orphans >>"$LOG" 2>&1 || true
  write_status "fehler" "Die neue Version startete nicht – die alte Version läuft wieder."
  return 1
}

# ------------------------------------------------------------ andere Container
label() { docker inspect -f "{{ index .Config.Labels \"$2\" }}" "$1" 2>/dev/null | sed 's/^<no value>$//'; }

container_update() {  # container_update job-id container-id benutzer
  local job="$1" cid="$2" user="$3" name="" project wd files service ref old self_project id
  local -a args
  # Meldet den Fehler; der Aufrufer beendet danach mit „return 1“.
  fail() { log "Container-Update $cid: $1"; write_job "$job" "fehler" "$1" "$cid" "$name" "${ref:-}" "$user"; }

  if ! [[ "$cid" =~ ^[0-9a-f]{12,64}$ ]]; then fail "Ungültige Container-ID."; return 1; fi
  if ! docker inspect "$cid" >/dev/null 2>&1; then fail "Container nicht gefunden."; return 1; fi
  name="$(docker inspect -f '{{.Name}}' "$cid" | sed 's#^/##')"
  ref="$(docker inspect -f '{{.Config.Image}}' "$cid")"
  old="$(docker inspect -f '{{.Image}}' "$cid")"
  project="$(label "$cid" com.docker.compose.project)"
  wd="$(label "$cid" com.docker.compose.project.working_dir)"
  files="$(label "$cid" com.docker.compose.project.config_files)"
  service="$(label "$cid" com.docker.compose.service)"
  write_job "$job" "laeuft" "Lade neues Image für „$name“ …" "$cid" "$name" "$ref" "$user"
  log "== Container-Update $name ($ref)"

  # Schutz noch einmal auf dem Server prüfen – unabhängig von der App.
  self_project="$(label "$(dashboard_id)" com.docker.compose.project 2>/dev/null || true)"
  case "$(label "$cid" dashboard.protect | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|ja|on) fail "„$name“ ist per Label geschützt."; return 1 ;;
  esac
  if [ -n "$self_project" ] && [ "$project" = "$self_project" ]; then fail "Das Dashboard selbst wird über „Dashboard“ aktualisiert."; return 1; fi
  case "$ref" in *docker-socket-proxy*) fail "Der Socket-Proxy wird mit dem Dashboard aktualisiert."; return 1 ;; esac
  case "$ref" in *@sha256:*) fail "Das Image ist per Digest festgelegt."; return 1 ;; esac
  if [ -z "$project" ] || [ -z "$service" ] || [ -z "$wd" ]; then fail "„$name“ gehört zu keinem Compose-Projekt."; return 1; fi
  if [ ! -d "$wd" ]; then fail "Projektordner $wd nicht gefunden."; return 1; fi

  args=(-p "$project" --project-directory "$wd")
  if [ -n "$files" ]; then
    IFS=',' read -ra liste <<<"$files"
    for f in "${liste[@]}"; do
      if [ ! -f "$f" ]; then fail "Compose-Datei $f nicht gefunden."; return 1; fi
      args+=(-f "$f")
    done
  fi

  if ! $DC "${args[@]}" pull "$service" >>"$LOG" 2>&1; then
    fail "Neues Image konnte nicht geladen werden (Details im update.log)."; return 1
  fi
  write_job "$job" "laeuft" "Erstelle „$name“ neu …" "$cid" "$name" "$ref" "$user"
  if $DC "${args[@]}" up -d --no-deps "$service" >>"$LOG" 2>&1; then
    id="$($DC "${args[@]}" ps -q "$service" 2>/dev/null | head -n 1)"
    if [ -n "$id" ] && container_ok "$id"; then
      log "Fertig: $name"
      write_job "$job" "fertig" "„$name“ wurde aktualisiert." "$id" "$name" "$ref" "$user"
      return 0
    fi
  fi
  log "„$name“ startet nicht – zurück auf das alte Image ${old:7:12}"
  $DC "${args[@]}" logs --tail 30 "$service" >>"$LOG" 2>&1 || true
  docker tag "$old" "$ref" >>"$LOG" 2>&1 || true
  $DC "${args[@]}" up -d --no-deps "$service" >>"$LOG" 2>&1 || true
  write_job "$job" "fehler" "Neue Version von „$name“ startete nicht – das alte Image läuft wieder." "$cid" "$name" "$ref" "$user"
  return 1
}

# ------------------------------------------------------------ Aufträge
handle() {
  local f inhalt id aktion cid user found=0
  for f in "$AUFTRAEGE"/*.json; do  # Glob ist sortiert, IDs beginnen mit dem Zeitstempel
    [ -e "$f" ] || continue
    found=1
    inhalt="$(head -c 4096 "$f")"
    rm -f "$f"  # immer zuerst entfernen, damit ein kaputter Auftrag nicht endlos neu startet
    id="$(json_get id "$inhalt")"
    aktion="$(json_get aktion "$inhalt")"
    cid="$(json_get container "$inhalt")"
    user="$(json_get benutzer "$inhalt")"
    if ! [[ "$id" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then log "Auftrag ohne gültige ID verworfen"; continue; fi
    case "$aktion" in
      vorbereiten) prepare || true ;;
      umschalten) apply || true ;;
      container) container_update "$id" "$cid" "$user" || true ;;
      *) log "Unbekannter Auftrag „$aktion“ verworfen" ;;
    esac
  done
  if [ $found = 0 ]; then prepare || true; fi
  # Alte Auftragsergebnisse aufräumen
  find "$JOBS" -name '*.json' -mtime +30 -delete 2>/dev/null || true
}

case "${1:-handle}" in
  handle) handle ;;
  prepare) prepare ;;
  apply) apply ;;
  status)
    r="$(prepared)"
    if [ -n "$r" ]; then write_status "bereit" "Version ${r:0:7} ist vorbereitet." "$r"
    else write_status "eingerichtet" "Update-Dienst eingerichtet."; fi ;;
  *) echo "Aufruf: $0 handle|prepare|apply|status" >&2; exit 2 ;;
esac
