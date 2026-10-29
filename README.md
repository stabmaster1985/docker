# Docker-Dashboard

Ein schlankes, selbst gehostetes Dashboard für einen einzelnen Docker-Host:
Übersicht aller Container mit Status, Health, Ressourcen und Logs sowie Start, Stopp und Neustart per Klick.

- Backend: Python 3.12, FastAPI, offizielles `docker`-SDK
- Frontend: serverseitig gerendertes HTML mit [htmx](https://htmx.org) (lokal ausgeliefert), kein Build-Schritt
- Keine Datenbank. Das Aktionslog ist eine JSON-Datei im Volume.
- Deutsch, mobilfreundlich, Hell- und Dunkelmodus

> **Gedacht für LAN oder Tailscale, nicht fürs offene Internet.**
> Wer Container steuern kann, hat faktisch weitgehende Kontrolle über den Host.

---

## Funktionen

| Bereich | Was es tut |
|---|---|
| Übersicht | Alle Container, gruppiert nach Compose-Projekt (`com.docker.compose.project`) |
| Pro Container | Name, Status, Health, Laufzeit, Restart-Anzahl, Image und Tag, Image-Alter, CPU, RAM |
| Auffällig | Block ganz oben für **beendete**, **ungesunde** und Container in einer **Restart-Schleife** |
| Aktionen | Start, Stopp und Neustart mit Bestätigungsdialog; das Ergebnis erscheint als Hinweis |
| Logs | Detailansicht mit den letzten 200 Zeilen, Filterfeld und Zeilenumbruch-Schalter |
| Aktualisierung | Automatisch alle 15 s, abschaltbar (die Einstellung merkt sich der Browser) |
| Labels | `dashboard.hide=true` blendet aus, `dashboard.protect=true` sperrt Aktionen |
| Aktionslog | Wer hat wann was gestartet oder gestoppt, in `/data/aktionen.json`, abrufbar unter „Aktionslog“ |

**Restart-Schleife** heißt: Der Container steht gerade auf „startet neu“, oder er hat innerhalb von
`RESTART_LOOP_WINDOW` Sekunden (Standard 600) mindestens `RESTART_LOOP_THRESHOLD` Restarts (Standard 3) gesammelt.

---

## Sicherheitskonzept

```
Browser ──LAN/Tailscale──▶ dashboard :8080  (Login, Session-Cookie)
                               │  internes Netz „socket“ (internal: true)
                               ▼
                         socket-proxy :2375 ──▶ /var/run/docker.sock (ro)
```

- **Kein Docker-Socket in der App.** Nur [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)
  sieht den Socket. Erlaubt sind dort ausschließlich `CONTAINERS`, `IMAGES`, `INFO`, `POST`, `ALLOW_START`, `ALLOW_STOP`
  und `ALLOW_RESTARTS`; alle anderen Bereiche stehen ausdrücklich auf `0`.
- Der Proxy hängt nur im Compose-Netz `socket` mit `internal: true`. Er hat keine Ports und keinen Internetzugang.
  Erreichen kann ihn nur das Dashboard.
- **Selbstschutz:** Das Dashboard verweigert Aktionen auf sich selbst, auf den Socket-Proxy und auf alle Container im
  eigenen Compose-Projekt. Zusätzlich tragen beide Container `dashboard.protect=true`. Geprüft wird immer serverseitig.
- **Login** mit Benutzername und Passwort aus der `.env`. Der Vergleich läuft in konstanter Zeit. Nach 5 Fehlversuchen
  wird die IP 5 Minuten gesperrt.
- **Sitzung** als signiertes Cookie (`HttpOnly`, `SameSite=Strict`, optional `Secure`). Jede Aktion braucht zusätzlich
  ein CSRF-Token.
- Strenge **Content-Security-Policy** (nur eigene Skripte und Styles, kein `eval`) sowie `X-Frame-Options: DENY`.
- Der Dashboard-Container läuft **ohne Root**, mit schreibgeschütztem Dateisystem, `cap_drop: ALL` und `no-new-privileges`.

### Hinweis zu `POST=1`

Mit `POST=1` lässt der Socket-Proxy schreibende Anfragen auf **alle** freigegebenen Bereiche durch, also auf
`/containers` und `/images`. Theoretisch könnte damit auch ein Container angelegt oder gelöscht werden. Das Dashboard
nutzt nur Start, Stopp und Neustart, und außer dem Dashboard kommt niemand an den Proxy heran. Trotzdem ist das der
Punkt mit dem größten Restrisiko.

Laut Dokumentation des Proxys sind `ALLOW_START`, `ALLOW_STOP` und `ALLOW_RESTARTS` genau dafür gedacht, diese drei
Aktionen gezielt freizugeben. Teste mit deiner Proxy-Version, ob Start, Stopp und Neustart auch mit `POST: 0`
funktionieren. Wenn ja, ist das die sicherere Einstellung.

---

## Installation

Voraussetzungen: Docker Engine mit Compose-Plugin (`docker compose version`).

```bash
# 1. Repository holen
git clone https://github.com/stabmaster1985/docker.git docker-dashboard
cd docker-dashboard

# 2. Konfiguration anlegen
cp .env.example .env
nano .env
```

In der `.env` mindestens anpassen:

| Variable | Bedeutung |
|---|---|
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Zugangsdaten. Mit dem Platzhalter-Passwort startet die App bewusst nicht. |
| `SESSION_SECRET` | Zufälliger Schlüssel, z. B. aus `openssl rand -hex 32` |
| `DASHBOARD_PORT` | Port auf dem Host (Standard 8080) |
| `BIND_ADDRESS` | Am besten die LAN- oder Tailscale-IP statt `0.0.0.0` |
| `DOCKER_API_VERSION` | `1.44` für Docker 25 und neuer; ältere Engines z. B. `1.41` (`docker version` zeigt die API-Version) |
| `SOCKET_PROXY_TAG` | Am besten eine feste Version des Proxy-Images statt `latest` |

```bash
# 3. Bauen und starten
docker compose up -d --build

# 4. Prüfen
docker compose ps          # dashboard sollte nach ~20 s "healthy" sein
docker compose logs -f dashboard
```

Danach im Browser `http://<host-ip>:<DASHBOARD_PORT>` öffnen.

### Nur über Tailscale erreichbar machen

`BIND_ADDRESS` auf die Tailscale-IP des Hosts setzen (`tailscale ip -4`, z. B. `100.64.0.12`). Dann ist der Port im
normalen LAN gar nicht offen. Wer HTTPS über `tailscale serve` nutzt, setzt zusätzlich `COOKIE_SECURE=true`.

---

## Update

```bash
cd docker-dashboard
git pull
docker compose pull socket-proxy     # neues Proxy-Image (falls Tag geändert oder latest)
docker compose up -d --build         # Dashboard neu bauen und beide Container neu erstellen
docker image prune -f                # optional: alte Images aufräumen
```

Das Aktionslog liegt im Volume `dashboard-data` und bleibt bei Updates erhalten.
Neue Einstellungen stehen jeweils in `.env.example`. Ein Vergleich zeigt, was fehlt: `diff .env.example .env`.

---

## Labels für andere Container

In der `docker-compose.yml` des jeweiligen Projekts:

```yaml
services:
  datenbank:
    image: postgres:16
    labels:
      dashboard.protect: "true"   # sichtbar, aber Start/Stopp/Neustart gesperrt
  intern:
    image: irgendwas
    labels:
      dashboard.hide: "true"      # taucht im Dashboard nicht auf, auch nicht per Direkt-URL
```

Labels werden erst wirksam, wenn der Container neu erstellt wird (`docker compose up -d`).
Als „wahr“ gelten `true`, `1`, `yes`, `ja` und `on`.

---

## Alle Einstellungen

| Variable | Standard | Bedeutung |
|---|---|---|
| `DASHBOARD_USER` | – | Benutzername (Pflicht) |
| `DASHBOARD_PASSWORD` | – | Passwort (Pflicht) |
| `SESSION_SECRET` | zufällig | Schlüssel für das Session-Cookie |
| `SESSION_MAX_AGE` | `43200` | Sitzungsdauer in Sekunden |
| `COOKIE_SECURE` | `false` | Cookie nur über HTTPS senden |
| `DASHBOARD_PORT` | `8080` | Port auf dem Host |
| `BIND_ADDRESS` | `0.0.0.0` | Host-Adresse für den Port |
| `TZ` | `Europe/Berlin` | Zeitzone für Anzeige und Logs |
| `DOCKER_API_VERSION` | `1.44` | Docker-API-Version (siehe oben) |
| `SOCKET_PROXY_TAG` | `latest` | Tag des Proxy-Images |
| `REFRESH_SECONDS` | `15` | Intervall der automatischen Aktualisierung |
| `LOG_TAIL` | `200` | Anzahl Log-Zeilen in der Detailansicht |
| `RESTART_LOOP_THRESHOLD` | `3` | Restarts, ab denen eine Schleife gemeldet wird |
| `RESTART_LOOP_WINDOW` | `600` | Zeitfenster dafür in Sekunden |
| `IGNORE_EXIT_ZERO` | `false` | `true` = Exit-Code 0 nicht als auffällig werten |
| `ACTION_LOG_MAX` | `1000` | Maximale Einträge im Aktionslog |
| `LOGIN_MAX_ATTEMPTS` | `5` | Fehlversuche bis zur Sperre |
| `LOGIN_LOCK_SECONDS` | `300` | Dauer der Sperre |

---

## Fehlersuche

| Symptom | Ursache und Lösung |
|---|---|
| „Docker nicht erreichbar … client version 1.44 is too new“ | Ältere Docker Engine. `DOCKER_API_VERSION` auf die Version aus `docker version` (Zeile „API version“) setzen. |
| „… client version is too old“ | Sehr neue Engine. `DOCKER_API_VERSION` erhöhen. |
| Proxy startet nicht (SELinux, z. B. Fedora/RHEL) | Beim `socket-proxy` `privileged: true` ergänzen oder den Socket-Mount mit `:z` versehen; siehe Proxy-Doku. |
| Aktionen liefern „403 Forbidden“ | Proxy-Version unterstützt `ALLOW_START`, `ALLOW_STOP` oder `ALLOW_RESTARTS` nicht. Aktuelleren `SOCKET_PROXY_TAG` verwenden. |
| CPU zeigt „–“ | Container läuft nicht, oder der Daemon hat noch keinen Vergleichswert geliefert. |
| Nach Neustart abgemeldet | `SESSION_SECRET` ist nicht gesetzt. |

---

## Entwicklung

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Die Tests verwenden einen nachgebauten Docker-Client und brauchen kein laufendes Docker.

Lokal starten (gegen einen eigenen Socket-Proxy auf `localhost:2375`):

```bash
DASHBOARD_USER=admin DASHBOARD_PASSWORD=test SESSION_SECRET=dev DATA_DIR=./data \
DOCKER_HOST=tcp://localhost:2375 uvicorn app.main:create_app --factory --reload --port 8080
```

### Struktur

```
app/
  main.py            Routen, Login, Aktionen, Healthcheck (/healthz)
  docker_service.py  Zugriff über den Proxy, Aufbereitung, Schutzlogik, Restart-Schleifen
  auth.py            Passwortprüfung, CSRF, Login-Sperre
  action_log.py      Aktionslog (JSON, atomar geschrieben)
  config.py          Einstellungen aus Umgebungsvariablen
  fmt.py             Deutsche Zeit- und Größenformate
  templates/         Jinja2-Templates (Seiten und htmx-Teilstücke)
  static/            htmx 2.0.4, CSS, JS
tests/               pytest
```

---

## Möglicher zweiter Schritt: Update-Prüfung (noch nicht eingebaut)

Idee: Für jedes Image den lokalen Digest mit dem aktuellen Digest in der Registry vergleichen und „Update
verfügbar“ anzeigen. Das Dashboard würde nur anzeigen und nichts selbst aktualisieren.

Varianten:
1. **Über den Socket-Proxy** mit zusätzlich `DISTRIBUTION=1` (`GET /distribution/{image}/json`). Das ist einfach und
   nutzt die Registry-Zugangsdaten des Hosts, erweitert aber die Freigaben des Proxys.
2. **Direkt aus dem Dashboard** gegen die Registry-API (Docker Hub, ghcr.io usw.). Dafür sind keine weiteren
   Proxy-Rechte nötig, aber Rate-Limits und private Registries müssen berücksichtigt werden. Das Dashboard braucht dann
   Internetzugang.

Für beide Varianten wäre ein Cache sinnvoll, z. B. eine Prüfung alle 6 Stunden, damit Registry-Limits nicht gerissen werden.
