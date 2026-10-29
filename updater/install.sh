#!/usr/bin/env bash
# shellcheck disable=SC1111  # deutsche Anführungszeichen in Meldungen sind gewollt
# Richtet den Update-Dienst des Docker-Dashboards auf dem Docker-Server ein (einmalig, als root).
#   sudo ./updater/install.sh            einrichten bzw. aktualisieren
#   sudo ./updater/install.sh --entfernen
#   ./updater/install.sh --units         nur die systemd-Units abgleichen (ruft der Dienst nach Updates selbst auf)
set -euo pipefail
if [ "$(id -u)" != "0" ]; then echo "Bitte als root ausführen: sudo $0" >&2; exit 1; fi
if ! command -v systemctl >/dev/null; then echo "systemd fehlt – Update-Dienst nicht möglich" >&2; exit 1; fi
DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="dashboard-updater.service dashboard-updater.path dashboard-updater.timer"
SYSTEMD="${DASH_SYSTEMD_DIR:-/etc/systemd/system}"
APP_UID="${DASH_UID:-10001}"

if [ "${1:-}" = "--entfernen" ]; then
  systemctl disable --now dashboard-updater.path dashboard-updater.timer 2>/dev/null || true
  for u in $UNITS; do rm -f "$SYSTEMD/$u"; done
  systemctl daemon-reload
  rm -f "$DIR/data/updater/status.json"
  echo "Update-Dienst entfernt."
  exit 0
fi

mkdir -p "$DIR/data/updater/auftraege" "$DIR/data/updater/jobs"
chown "$APP_UID:$APP_UID" "$DIR/data" "$DIR/data/updater" "$DIR/data/updater/auftraege" "$DIR/data/updater/jobs"
changed=0
for u in $UNITS; do
  new="$(sed "s#@DIR@#$DIR#g" "$DIR/updater/$u")"
  if [ ! -f "$SYSTEMD/$u" ] || [ "$new" != "$(cat "$SYSTEMD/$u")" ]; then
    printf '%s\n' "$new" >"$SYSTEMD/$u"
    changed=1
  fi
done
chmod +x "$DIR/updater/dashboard-updater.sh"
[ $changed = 1 ] && systemctl daemon-reload
systemctl enable --now dashboard-updater.path dashboard-updater.timer >/dev/null
if [ "${1:-}" = "--units" ]; then
  [ $changed = 1 ] && echo "Update-Dienst: systemd-Units aktualisiert."
  exit 0  # läuft im Dienst selbst – status würde auf dessen Sperre warten
fi
DASH_DIR="$DIR" bash "$DIR/updater/dashboard-updater.sh" status
echo "Update-Dienst eingerichtet ($DIR). Die App zeigt ihn unter „Updates“."
