import json

from app import fmt
from app.docker_service import RestartTracker, compute_cpu, compute_mem, split_image
from tests.conftest import csrf_from, login


# ------------------------------------------------------------------ Login
def test_redirect_ohne_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_htmx_ohne_login_bekommt_hx_redirect(client):
    r = client.get("/partials/liste", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_login_falsch_und_richtig(client):
    assert "falsch" in login(client, password="nein").text
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200


def test_login_ohne_csrf_abgelehnt(client):
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": "geheim123", "csrf": "x"})
    assert "Sitzung abgelaufen" in r.text


def test_login_sperre_nach_fehlversuchen(client):
    for _ in range(3):
        login(client, password="nein")
    r = login(client)  # richtiges Passwort, aber gesperrt
    assert "Zu viele Fehlversuche" in r.text


def test_sicherheitsheader(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_healthz_ohne_login(client):
    assert client.get("/healthz").json() == {"status": "ok", "docker": True}


# -------------------------------------------------------------- Übersicht
def test_uebersicht_gruppen_und_ausblenden(client):
    login(client)
    html = client.get("/").text
    assert "immich" in html and "paperless" in html and "docker-dashboard" in html
    assert "geheim" not in html.replace("geheim123", "")  # dashboard.hide
    assert "Ohne Compose-Projekt" in html
    assert "ghcr.io/immich-app/immich-server" in html and "v2.1.0" in html
    assert "vor 3 Monaten" in html


def test_auffaellige_container(service):
    infos = {c.name: c for c in service.list_containers()}
    assert infos["immich-db"].problems == ["beendet (Code 1)"]
    assert infos["paperless"].problems == ["ungesund"]
    assert "Restart-Schleife" in infos["flapper"].problems
    assert not infos["immich-server"].auffaellig


def test_cpu_ram_berechnet(service):
    info = next(c for c in service.list_containers() if c.name == "immich-server")
    assert round(info.cpu_percent, 1) == 40.0  # 1000/10000 * 4 CPUs
    assert info.mem_usage == 256 * 2**20 and info.mem_limit == 1024 * 2**20


# ------------------------------------------------------------------ Schutz
def test_selbst_proxy_und_label_geschuetzt(service):
    infos = {c.name: c for c in service.list_containers(with_stats=False)}
    assert infos["docker-dashboard-dashboard-1"].protected_reason == "das Dashboard selbst"
    assert infos["docker-dashboard-socket-proxy-1"].protected
    assert infos["router"].protected_reason == "per Label geschützt"
    assert not infos["immich-server"].protected


def test_aktion_auf_geschuetzten_container_verweigert(client, containers):
    login(client)
    csrf = csrf_from(client.get("/").text)
    for cid in ("dash", "proxy", "prot"):
        r = client.post(f"/container/{cid}/stop", headers={"X-CSRF-Token": csrf})
        assert "geschützt" in r.text
    assert all(not c.calls for c in containers)


def test_aktion_auf_ausgeblendeten_container_verweigert(client, containers):
    login(client)
    csrf = csrf_from(client.get("/").text)
    r = client.post("/container/secret/stop", headers={"X-CSRF-Token": csrf})
    assert "nicht gefunden" in r.text
    assert client.get("/container/secret").status_code == 200  # Fehlerseite
    assert next(c for c in containers if c.name == "geheim").calls == []


def test_aktion_ohne_csrf_abgelehnt(client, containers):
    login(client)
    r = client.post("/container/web/restart")
    assert r.status_code == 403
    assert next(c for c in containers if c.name == "immich-server").calls == []


def test_aktion_wird_ausgefuehrt_und_geloggt(client, containers, settings, tmp_path):
    login(client)
    csrf = csrf_from(client.get("/").text)
    r = client.post("/container/web/restart", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and "neu gestartet" in r.text and 'id="toasts"' not in r.text
    assert next(c for c in containers if c.name == "immich-server").calls == ["restart"]

    log = json.loads((tmp_path / "aktionen.json").read_text(encoding="utf-8"))
    assert log[-1]["benutzer"] == "admin" and log[-1]["aktion"] == "restart" and log[-1]["ok"]
    assert "immich-server" in client.get("/aktionen").text


def test_unbekannte_aktion(client, containers):
    login(client)
    csrf = csrf_from(client.get("/").text)
    r = client.post("/container/web/kill", headers={"X-CSRF-Token": csrf})
    assert "Unbekannte Aktion" in r.text
    assert next(c for c in containers if c.name == "immich-server").calls == []


# ------------------------------------------------------------------- Logs
def test_detail_mit_logs(client):
    login(client)
    html = client.get("/container/web").text
    assert "Server gestartet" in html and "Datenbank weg" in html
    assert "\x1b" not in html


# ------------------------------------------------------------------ Hilfen
def test_restart_tracker():
    t = RestartTracker(threshold=3, window=600)
    assert t.observe("a", 1, now=0) == 0
    assert t.observe("a", 3, now=100) == 2
    assert t.observe("a", 4, now=200) == 3
    assert t.observe("a", 5, now=700) == 2  # Beobachtung bei t=0 ist aus dem Fenster gefallen
    assert t.observe("a", 0, now=950) == 0  # Zähler zurückgesetzt


def test_split_image():
    assert split_image("nginx") == ("nginx", "latest")
    assert split_image("nginx:1.27") == ("nginx", "1.27")
    assert split_image("registry:5000/team/app") == ("registry:5000/team/app", "latest")
    assert split_image("registry:5000/team/app:2") == ("registry:5000/team/app", "2")
    assert split_image("nginx:1.27@sha256:abc") == ("nginx", "1.27")


def test_stats_ohne_vorwert():
    assert compute_cpu({"cpu_stats": {"cpu_usage": {"total_usage": 5}}, "precpu_stats": {}}) is None
    assert compute_mem({}) == (None, None)


def test_formatierung():
    assert fmt.dauer(90) == "1 Min."
    assert fmt.dauer(3 * 86400 + 7200) == "3 Tg. 2 Std."
    assert fmt.bytes_text(1536 * 2**20) == "1,5 GiB"
    assert fmt.parse_docker_time("2026-10-29T12:00:00.123456789Z").microsecond == 123456
    assert fmt.parse_docker_time("0001-01-01T00:00:00Z") is None
