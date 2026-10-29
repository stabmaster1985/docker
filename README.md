# Docker-Dashboard

Ein schlankes, selbst gehostetes Dashboard für einen einzelnen Docker-Host:
Übersicht aller Container mit Status, Health, Ressourcen und Logs, dazu Start, Stopp und Neustart per Klick.
Außerdem gibt es Updates für Container und für das Dashboard selbst per Knopfdruck, und fast alles lässt sich in der Oberfläche einstellen.

- Backend: Python 3.12, FastAPI, offizielles `docker`-SDK
- Frontend: serverseitig gerendertes HTML mit [htmx](https://htmx.org) (lokal ausgeliefert), kein Build-Schritt
- Keine Datenbank. Einstellungen und Aktionslog liegen als JSON-Dateien in `./data`.
- Deutsch, mobilfreundlich, Hell- und Dunkelmodus

> **Gedacht für LAN oder Tailscale, nicht fürs offene Internet.**
> Wer Container steuern kann, hat faktisch weitgehende Kontrolle über den Host.

---

## Funktionen

| Bereich | Was es tut |
|---|---|
| Übersicht | Alle Container, gruppiert nach Compose-Projekt (`com.docker.compose.project`) |
| Pro Container | Name, Status, Health, Laufzeit, Restart-Anzahl, Image und Tag, Image-Alter, CPU, RAM |
| **Ports und Links** | Veröffentlichte Ports als anklickbare Chips, dazu optional ein eigener Link („Öffnen ↗“); per Schalter „Ports“ ausblendbar |
| Auffällig | Block ganz oben für **beendete**, **ungesunde** und Container in einer **Restart-Schleife** |
| Aktionen | Start, Stopp und Neustart mit Bestätigungsdialog; das Ergebnis erscheint als Hinweis |
| Logs | Detailansicht mit den letzten 200 Zeilen, Filterfeld und Zeilenumbruch-Schalter |
| Aktualisierung | Automatisch alle 15 s, abschaltbar (die Einstellung merkt sich der Browser) |
| **Updates** | Prüft, ob es für die Images neuere Versionen gibt; **Update per Klick** mit automatischem Rücksprung |
| **Dashboard-Update** | Neue Versionen des Dashboards werden im Hintergrund vorbereitet; Umschalten per Klick |
| **Einstellungen** | Anzeige, Restart-Schleife, Update-Prüfung, Sitzung, Passwort, Ausblenden/Schützen je Container |
| Labels | `dashboard.hide=true` blendet aus, `dashboard.protect=true` sperrt Aktionen |
| Aktionslog | Wer hat wann was gestartet, gestoppt, aktualisiert oder eingestellt, abrufbar unter „Aktionslog“ |

**Restart-Schleife** heißt: Der Container steht gerade auf „startet neu“, oder er hat im eingestellten Zeitfenster
(Standard 600 s) mindestens die eingestellte Anzahl Restarts (Standard 3) gesammelt.

---

## Sicherheitskonzept

```
Browser ──LAN/Tailscale──▶ dashboard :8080  (Login, Session-Cookie)
                               │  internes Netz „socket“ (internal: true)
                               ▼
                         socket-proxy :2375 ──▶ /var/run/docker.sock (ro)

dashboard ──Dateien in ./data/updater──▶ Update-Dienst (systemd auf dem Host)
```

- **Kein Docker-Socket in der App.** Nur [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy)
  sieht den Socket. Erlaubt sind dort ausschließlich `CONTAINERS`, `IMAGES`, `INFO`, `POST`, `ALLOW_START`, `ALLOW_STOP`
  und `ALLOW_RESTARTS`; alle anderen Bereiche stehen ausdrücklich auf `0`.
- Der Proxy hängt nur im Compose-Netz `socket` mit `internal: true`. Er hat keine Ports und keinen Internetzugang.
  Erreichen kann ihn nur das Dashboard.
- **Updates laufen nicht über den Proxy.** Images laden und Container neu erstellen erledigt ein kleiner Dienst auf dem
  Host (siehe [Update-Dienst](#update-dienst)). Das Dashboard legt dafür nur einen Auftrag als Datei ab, den Rest
  entscheidet der Dienst. Der Container bekommt dadurch **keine zusätzlichen Rechte**.
- **Selbstschutz:** Das Dashboard verweigert Aktionen auf sich selbst, auf den Socket-Proxy und auf alle Container im
  eigenen Compose-Projekt. Zusätzlich tragen beide Container `dashboard.protect=true`. Geprüft wird in der App und noch
  einmal im Update-Dienst.
- **Login** mit Benutzername und Passwort. Ein in der Oberfläche geändertes Passwort wird nur als scrypt-Hash
  gespeichert. Nach 5 Fehlversuchen wird die IP 5 Minuten gesperrt.
- **Sitzung** als signiertes Cookie (`HttpOnly`, `SameSite=Strict`, optional `Secure`). Jede Änderung braucht
  zusätzlich ein CSRF-Token.
- Strenge **Content-Security-Policy** (nur eigene Skripte und Styles, kein `eval`) sowie `X-Frame-Options: DENY`.
- Der Dashboard-Container läuft **ohne Root** (UID 10001), mit schreibgeschütztem Dateisystem, `cap_drop: ALL` und
  `no-new-privileges`.

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

Voraussetzungen: Docker Engine mit Compose-Plugin (`docker compose version`), `git`. Für Updates per Klick außerdem
systemd, was auf Debian, Ubuntu und Raspberry Pi OS Standard ist.

```bash
git clone https://github.com/stabmaster1985/docker.git docker-dashboard
cd docker-dashboard
sudo ./installieren.sh        # legt beim ersten Lauf die .env an und bittet ums Ausfüllen
nano .env                     # mindestens DASHBOARD_PASSWORD, am besten auch BIND_ADDRESS
sudo ./installieren.sh        # baut, startet, wartet auf „healthy“ und richtet den Update-Dienst ein
```

Danach im Browser `http://<host-ip>:<DASHBOARD_PORT>` öffnen. Alles Weitere stellst du unter **Einstellungen** ein.
Erneut ausgeführt repariert `installieren.sh` eine Installation.

<details>
<summary>Von Hand statt mit <code>installieren.sh</code></summary>

```bash
cp .env.example .env && nano .env
mkdir -p data && sudo chown -R 10001:10001 data     # Datenordner gehört dem Container-Benutzer
GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --build
sudo ./updater/install.sh                            # optional: Updates per Klick
```
</details>

### Was in die `.env` muss

| Variable | Bedeutung |
|---|---|
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | Erste Zugangsdaten. Mit dem Platzhalter-Passwort startet die App bewusst nicht. Später in der Oberfläche änderbar. |
| `DASHBOARD_PORT` | Port auf dem Host (Standard 8080) |
| `BIND_ADDRESS` | Am besten die LAN- oder Tailscale-IP statt `0.0.0.0` |
| `DOCKER_API_VERSION` | `1.44` für Docker 25 und neuer; ältere Engines z. B. `1.41` (`docker version` zeigt die API-Version) |
| `SOCKET_PROXY_TAG` | Am besten eine feste Version des Proxy-Images statt `latest` |

Alle anderen Werte in `.env.example` sind nur Vorgaben. Sie lassen sich in der Oberfläche ändern (siehe unten).

### Nur über Tailscale erreichbar machen

`BIND_ADDRESS` auf die Tailscale-IP des Hosts setzen (`tailscale ip -4`, z. B. `100.64.0.12`). Dann ist der Port im
normalen LAN gar nicht offen. Wer HTTPS über `tailscale serve` nutzt, setzt zusätzlich `COOKIE_SECURE=true`.

---

## Einstellungen in der Oberfläche

Unter **Einstellungen** lässt sich ohne Neustart ändern:

- **Anzeige:** Aktualisierungsintervall, Log-Zeilen, Exit-Code 0 als unauffällig werten
- **Restart-Schleife:** Schwelle und Zeitfenster
- **Updates:** automatische Image-Prüfung an/aus, Intervall in Stunden
- **Zugang:** Benutzername und Passwort, Sitzungsdauer, Login-Sperre
- **Container:** einzelne Container **ausblenden** oder **schützen** (zusätzlich zu den Labels)
- **Aktionslog:** maximale Einträge, Log leeren

Reihenfolge: Vorgabe im Code, dann `.env`, dann Oberfläche. Was in der Oberfläche gespeichert ist, gilt; Werte, die
von der `.env` abweichen, sind mit „geändert“ markiert. „Auf .env zurücksetzen“ verwirft die Änderungen.

Was **nicht** in der Oberfläche geht, und warum:

| Wert | Grund |
|---|---|
| Port, Bind-Adresse | setzt Docker, bevor die App läuft |
| `COOKIE_SECURE`, Docker-API-Version | ohne passende Werte ist die App gar nicht erreichbar bzw. findet Docker nicht |
| Rechte des Socket-Proxys | absichtlich: die Oberfläche soll sich nie selbst mehr Rechte geben können |
| Schutz per Label, Dashboard und Proxy | lässt sich in der Oberfläche nicht abschalten, nur zusätzlich setzen |

**Passwort vergessen?** Auf dem Server `data/zugang.json` löschen. Dann gelten wieder die Zugangsdaten aus der `.env`.

---

## Updates

Die Seite **Updates** (roter Punkt in der Navigation, wenn etwas ansteht) hat zwei Teile.

### Container-Images

- Alle `UPDATE_CHECK_HOURS` Stunden (Standard 6) und auf Knopfdruck fragt das Dashboard bei der Registry nach, auf welchen
  Stand der Tag eines Images zeigt (`:latest`, `:16`, `:v2.1` usw.). Es fragt **nur das Manifest per HEAD-Anfrage** ab
  und lädt nichts herunter. Bei Docker Hub zählt das nicht gegen das Pull-Limit.
- Stimmt der Stand nicht mit dem lokalen Image überein, erscheint **„Update“** in Übersicht und Detailansicht.
  **„Neustart nötig“** heißt: Das neue Image ist schon geladen (z. B. von Hand per `docker compose pull`), der Container
  läuft aber noch mit dem alten.
- **„Update“ per Klick** läuft über den Update-Dienst auf dem Host:
  1. `docker compose pull <service>` im Ordner des Compose-Projekts
  2. `docker compose up -d --no-deps <service>`, das nur diesen Container neu erstellt
  3. Warten, bis er „healthy“ ist bzw. ohne Healthcheck stabil läuft
  4. Klappt das nicht: altes Image zurücktaggen und den Container wieder damit starten

  Das Ergebnis erscheint als Hinweis und im Aktionslog.

**Grenzen:**
- Nur für Container aus einem **Compose-Projekt**. Einzeln per `docker run` gestartete Container lassen sich nicht neu
  erstellen, ohne ihre Startparameter zu kennen.
- **Selbst gebaute Images** (`build:` in Compose) und Images ohne Tag sind „nicht prüfbar“.
- **Per Digest festgelegte** Images (`image: foo@sha256:…`) werden absichtlich nicht angefasst.
- **Private Registries** melden „Anmeldung nötig“. Die Prüfung braucht dafür Zugangsdaten, die das Dashboard
  bewusst nicht kennt.
- Geschützte Container sowie Dashboard und Socket-Proxy lassen sich hier nicht aktualisieren. Das Dashboard und der
  Proxy werden über „Dashboard“ aktualisiert.

### Dashboard

Funktioniert wie bei immich-pflege: Der Update-Dienst holt **stündlich** neue Versionen von GitHub (Branch `main`) und
baut das Image im Hintergrund fertig, ohne die laufende Version anzufassen. Unter „Updates“ siehst du die neuen Commits.
„Jetzt aktualisieren“ schaltet in Sekunden um und lädt die Seite danach neu. **Startet die neue Version nicht sauber,
geht es automatisch auf die alte zurück.** Das Image des Socket-Proxys wird dabei gleich mit aktualisiert.

### Update-Dienst

Ein kleiner systemd-Dienst auf dem Docker-Server im Ordner `updater/`. Ihn richtet `installieren.sh` mit ein, von Hand
geht es so:

```bash
cd docker-dashboard
sudo ./updater/install.sh              # einrichten bzw. aktualisieren
sudo ./updater/install.sh --entfernen  # wieder entfernen
```

- `dashboard-updater.path` reagiert sofort, wenn die App einen Auftrag in `data/updater/auftraege/` ablegt.
- `dashboard-updater.timer` läuft stündlich und bereitet neue Dashboard-Versionen vor.
- App und Dienst tauschen nur Dateien aus: Aufträge, `status.json`, `jobs/*.json` und `update.log` in
  `data/updater/`.
- Der Dienst prüft jeden Auftrag selbst noch einmal: gültige Container-ID, Compose-Projekt vorhanden, nicht geschützt,
  nicht das Dashboard oder der Proxy.
- Bringt ein Dashboard-Update eine neue Fassung des Dienstes mit, übernimmt er sie beim Umschalten selbst.

Status und Protokoll auf dem Server:

```bash
systemctl status dashboard-updater.timer dashboard-updater.path
tail -f data/updater/update.log
```

### Ohne Update-Dienst (z. B. ohne systemd)

```bash
cd docker-dashboard
git pull
GIT_COMMIT=$(git rev-parse HEAD) docker compose up -d --build
docker compose pull socket-proxy && docker compose up -d
```

---

## Ports und Links

Zu jedem Container zeigt die Übersicht die **veröffentlichten Ports** (`ports:` in Compose) als kleine Chips:

- Ein Klick öffnet `http://<Adresse>:<Port>` in einem neuen Tab. Als Adresse dient die, über die du das Dashboard gerade
  aufrufst (LAN-IP, Tailscale-Name …). Ist ein Port an eine bestimmte IP gebunden, wird diese verwendet.
- Ports `443` und `8443` werden als `https://` verlinkt.
- Grau und nicht anklickbar: UDP-Ports, nur lokal gebundene Ports (`127.0.0.1:…`) und Ports gestoppter Container.
- **Eigener Link** „Öffnen ↗“ (z. B. die Adresse hinter einem Reverse-Proxy): per Label `dashboard.url` oder unter
  **Einstellungen** je Container. Der Eintrag in den Einstellungen geht vor. Erlaubt sind nur `http://` und `https://`.
- Der Schalter **„Ports“** oben in der Übersicht blendet die Chips aus, etwa auf dem Handy. Die Einstellung merkt sich der
  Browser. In der Detailansicht eines Containers sind Ports und Link immer zu sehen.

---

## Labels für andere Container

In der `docker-compose.yml` des jeweiligen Projekts:

```yaml
services:
  datenbank:
    image: postgres:16
    labels:
      dashboard.protect: "true"   # sichtbar, aber Start/Stopp/Neustart/Update gesperrt
  intern:
    image: irgendwas
    labels:
      dashboard.hide: "true"      # taucht im Dashboard nicht auf, auch nicht per Direkt-URL
  fotos:
    image: ghcr.io/immich-app/immich-server:release
    labels:
      dashboard.url: "https://fotos.example.lan"   # Link „Öffnen ↗“, z. B. hinter einem Reverse-Proxy
```

Labels werden erst wirksam, wenn der Container neu erstellt wird (`docker compose up -d`).
Als „wahr“ gelten `true`, `1`, `yes`, `ja` und `on`. Ohne Neuerstellung geht dasselbe unter **Einstellungen**.

---

## Datenordner `./data`

| Datei | Inhalt |
|---|---|
| `einstellungen.json` | in der Oberfläche geänderte Werte, ausgeblendete und geschützte Container |
| `zugang.json` | Benutzername und Passwort-Hash, falls in der Oberfläche geändert (Rechte 600) |
| `session_secret` | Schlüssel fürs Sitzungs-Cookie, falls `SESSION_SECRET` leer ist (Rechte 600) |
| `aktionen.json` | Aktionslog |
| `image-updates.json` | Ergebnis der letzten Update-Prüfung |
| `updater/` | Austausch mit dem Update-Dienst |

Der Ordner gehört UID 10001 (der Benutzer im Container) und ist nicht im Repository. Er bleibt bei Updates erhalten.
Zum Sichern genügt es, ihn zusammen mit der `.env` zu kopieren.

---

## Fehlersuche

| Symptom | Ursache und Lösung |
|---|---|
| „Docker nicht erreichbar … client version 1.44 is too new“ | Ältere Docker Engine. `DOCKER_API_VERSION` auf die Version aus `docker version` (Zeile „API version“) setzen. |
| „… client version is too old“ | Sehr neue Engine. `DOCKER_API_VERSION` erhöhen. |
| `Permission denied` auf `/data` in den Logs | Datenordner gehört nicht UID 10001: `sudo chown -R 10001:10001 data` |
| Proxy startet nicht (SELinux, z. B. Fedora/RHEL) | Beim `socket-proxy` `privileged: true` ergänzen oder den Socket-Mount mit `:z` versehen; siehe Proxy-Doku. |
| Aktionen liefern „403 Forbidden“ | Proxy-Version unterstützt `ALLOW_START`, `ALLOW_STOP` oder `ALLOW_RESTARTS` nicht. Aktuelleren `SOCKET_PROXY_TAG` verwenden. |
| „Update-Dienst ist nicht eingerichtet“ | `sudo ./updater/install.sh` im Ordner des Dashboards ausführen |
| „Update-Dienst meldet sich nicht mehr“ | `systemctl status dashboard-updater.timer` prüfen; Protokoll in `data/updater/update.log` |
| „Dateien von Hand geändert: …“ beim Update | Im Ordner wurde eine Datei aus dem Repository geändert (z. B. `docker-compose.yml`). Mit `git checkout -- <datei>` zurücksetzen und erneut aktualisieren. Eigene Werte gehören in die `.env` oder in die Oberfläche. |
| „Lokaler Stand weicht von GitHub ab“ | Auf dem Server wurde im Ordner etwas geändert: `git status`, Änderungen sichern, dann `git reset --hard origin/main` |
| Update-Prüfung: „Abruf-Limit erreicht“ | Docker Hub begrenzt Abfragen. Intervall unter Einstellungen erhöhen. |
| CPU zeigt „–“ | Container läuft nicht, oder der Daemon hat noch keinen Vergleichswert geliefert. |

---

## Entwicklung

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Die Tests verwenden einen nachgebauten Docker-Client und eine nachgebaute Registry. Sie brauchen weder Docker noch
Internet.

Lokal starten (gegen einen eigenen Socket-Proxy auf `localhost:2375`):

```bash
DASHBOARD_USER=admin DASHBOARD_PASSWORD=test-passwort DATA_DIR=./data \
DOCKER_HOST=tcp://localhost:2375 uvicorn app.main:create_app --factory --reload --port 8080
```

### Struktur

```
app/
  main.py            Routen, Login, Aktionen, Updates, Einstellungen, Healthcheck (/healthz)
  docker_service.py  Zugriff über den Proxy, Aufbereitung, Schutzlogik, Restart-Schleifen
  store.py           Einstellungen aus der Oberfläche, Zugangsdaten, Sitzungsschlüssel
  image_updates.py   Update-Prüfung im Hintergrund
  registry.py        Digest eines Tags bei der Registry abfragen (HEAD, Token-Anmeldung)
  updater.py         Austausch mit dem Update-Dienst (Aufträge, Status)
  auth.py            CSRF, Sitzungsdauer, Login-Sperre
  action_log.py      Aktionslog
  config.py          Startwerte aus Umgebungsvariablen
  fmt.py             Deutsche Zeit- und Größenformate
  templates/         Jinja2-Templates (Seiten und htmx-Teilstücke)
  static/            htmx 2.0.4, CSS, JS
updater/             Update-Dienst für den Host (Skript, systemd-Units, install.sh)
installieren.sh      Installation in einem Schritt
tests/               pytest
```
