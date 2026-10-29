import json
import os
import stat
import urllib.error
from dataclasses import replace
from datetime import datetime, timezone
from email.message import Message

import pytest

from app import registry
from app.main import create_app
from app.store import Store, hash_password, verify_password
from tests.conftest import BASE_SETTINGS, csrf_from, hex_id, hx, login

WEB_ID = hex_id("web")


def jetzt() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def eingeloggt(client):
    login(client)
    return csrf_from(client.get("/").text)


def formular(**werte):
    basis = {
        "refresh_seconds": "15", "log_tail": "200", "restart_loop_threshold": "3",
        "restart_loop_window": "600", "update_check": "on", "update_check_hours": "6",
        "session_max_age": "60", "login_max_attempts": "3", "login_lock_seconds": "60",
        "action_log_max": "100",
    }
    basis.update(werte)
    return {k: v for k, v in basis.items() if v is not None}


def updater_bereit(tmp_path, **extra):
    """Simuliert einen eingerichteten Update-Dienst auf dem Host."""
    ordner = tmp_path / "updater"
    ordner.mkdir(exist_ok=True)
    status = {"phase": "aktuell", "meldung": "Die neueste Version läuft bereits.", "aktuell": "a" * 40,
              "bereit": "", "commits": [], "zeit": jetzt(), **extra}
    (ordner / "status.json").write_text(json.dumps(status), encoding="utf-8")


def auftraege(tmp_path):
    ordner = tmp_path / "updater" / "auftraege"
    return [json.loads(p.read_text()) for p in sorted(ordner.glob("*.json"))] if ordner.exists() else []


# -------------------------------------------------------------- Einstellungen
def test_einstellungen_speichern_und_wirken(client, app):
    csrf = eingeloggt(client)
    r = client.post("/einstellungen/allgemein", data=formular(refresh_seconds="30", log_tail="50"),
                    headers=hx(csrf))
    assert "Einstellungen gespeichert" in r.text
    assert app.state.store.s.refresh_seconds == 30
    assert 'data-intervall="30"' in client.get("/").text
    gespeichert = json.loads((app.state.store.path).read_text())
    # Nur Abweichungen von der .env werden gespeichert.
    assert gespeichert["werte"] == {"refresh_seconds": 30, "log_tail": 50}


def test_einstellungen_ungueltig_wird_abgelehnt(client, app):
    csrf = eingeloggt(client)
    r = client.post("/einstellungen/allgemein", data=formular(refresh_seconds="2", log_tail="abc"),
                    headers=hx(csrf))
    assert "Nicht gespeichert" in r.text and "erlaubt sind 5 bis 3600" in r.text
    assert app.state.store.s.refresh_seconds == 15


def test_checkbox_aus_wird_gespeichert(client, app):
    csrf = eingeloggt(client)
    client.post("/einstellungen/allgemein", data=formular(update_check=None), headers=hx(csrf))
    assert app.state.store.s.update_check is False


def test_einstellungen_zuruecksetzen(client, app):
    csrf = eingeloggt(client)
    client.post("/einstellungen/allgemein", data=formular(refresh_seconds="99"), headers=hx(csrf))
    client.post("/einstellungen/zuruecksetzen", headers=hx(csrf))
    assert app.state.store.s.refresh_seconds == 15


def test_einstellungsseite_rendert(client):
    eingeloggt(client)
    html = client.get("/einstellungen").text
    assert "Container: ausblenden, schützen, Link" in html and "Zugang" in html
    assert "geheim" in html  # auch ausgeblendete Container sind dort aufgeführt


def test_store_ueberlebt_neustart(settings):
    s1 = Store(settings)
    s1.update(formular(log_tail="77"))
    s1.set_container("immich-db", hidden=True, protected=False)
    s2 = Store(settings)
    assert s2.s.log_tail == 77 and "immich-db" in s2.hidden_names


# ---------------------------------------------------- Ausblenden / Schützen
def test_container_per_oberflaeche_ausblenden(client):
    csrf = eingeloggt(client)
    client.post("/einstellungen/container", data={"name": "immich-server", "ausgeblendet": "on"},
                headers=hx(csrf))
    assert "immich-server" not in client.get("/partials/liste").text
    assert "nicht gefunden" in client.get(f"/container/{WEB_ID}").text
    client.post("/einstellungen/container", data={"name": "immich-server"}, headers=hx(csrf))
    assert "immich-server" in client.get("/partials/liste").text


def test_container_per_oberflaeche_schuetzen(client, containers):
    csrf = eingeloggt(client)
    client.post("/einstellungen/container", data={"name": "immich-server", "geschuetzt": "on"},
                headers=hx(csrf))
    r = client.post("/container/web/stop", headers=hx(csrf))
    assert "in den Einstellungen geschützt" in r.text
    assert next(c for c in containers if c.name == "immich-server").calls == []


def test_fester_schutz_nicht_abschaltbar(client, containers):
    csrf = eingeloggt(client)
    for name in ("docker-dashboard-dashboard-1", "docker-dashboard-socket-proxy-1", "router"):
        client.post("/einstellungen/container", data={"name": name}, headers=hx(csrf))
    for cid in ("dash", "proxy", "prot"):
        assert "ist geschützt (" in client.post(f"/container/{cid}/restart", headers=hx(csrf)).text
    assert all(not c.calls for c in containers)


# ------------------------------------------------------------------- Zugang
def test_passwort_aendern(client, app, tmp_path):
    csrf = eingeloggt(client)
    falsch = client.post("/einstellungen/zugang", headers=hx(csrf),
                         data={"alt": "nein", "benutzer": "admin", "neu": "x" * 12, "neu2": "x" * 12})
    assert "aktuelle Passwort stimmt nicht" in falsch.text
    kurz = client.post("/einstellungen/zugang", headers=hx(csrf),
                       data={"alt": "geheim123", "benutzer": "admin", "neu": "kurz", "neu2": "kurz"})
    assert "mindestens 10 Zeichen" in kurz.text
    ok = client.post("/einstellungen/zugang", headers=hx(csrf),
                     data={"alt": "geheim123", "benutzer": "chef", "neu": "neues-passwort-1",
                           "neu2": "neues-passwort-1"})
    assert "Zugangsdaten geändert" in ok.text

    datei = tmp_path / "zugang.json"
    assert "neues-passwort-1" not in datei.read_text()
    assert stat.S_IMODE(os.stat(datei).st_mode) == 0o600

    client.cookies.clear()
    assert "falsch" in login(client, user="admin", password="geheim123").text
    assert login(client, user="chef", password="neues-passwort-1").status_code == 303


def test_passwort_hash():
    h = hash_password("geheim")
    assert h.startswith("scrypt$") and verify_password("geheim", h) and not verify_password("nein", h)
    assert not verify_password("geheim", "kaputt")


def test_sitzungsschluessel_wird_erzeugt_und_behalten(tmp_path):
    s = replace(BASE_SETTINGS, data_dir=str(tmp_path), session_secret="")
    eins, zwei = Store(s).session_secret(), Store(s).session_secret()
    assert eins == zwei and len(eins) > 40
    assert stat.S_IMODE(os.stat(tmp_path / "session_secret").st_mode) == 0o600


def test_sitzung_laeuft_ab(client, app, monkeypatch):
    eingeloggt(client)
    import app.auth as auth_modul
    echt = auth_modul.time.time
    monkeypatch.setattr(auth_modul.time, "time", lambda: echt() + 3601)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ----------------------------------------------------------------- Registry
def test_parse_ref():
    assert registry.parse_ref("nginx") == ("registry-1.docker.io", "library/nginx", "latest")
    assert registry.parse_ref("nginx:1.27") == ("registry-1.docker.io", "library/nginx", "1.27")
    assert registry.parse_ref("user/app:2") == ("registry-1.docker.io", "user/app", "2")
    assert registry.parse_ref("docker.io/user/app") == ("registry-1.docker.io", "user/app", "latest")
    assert registry.parse_ref("ghcr.io/immich-app/immich-server:v2") == ("ghcr.io", "immich-app/immich-server", "v2")
    assert registry.parse_ref("localhost:5000/x:1") == ("localhost:5000", "x", "1")
    assert registry.normalize_repo("nginx") == registry.normalize_repo("docker.io/library/nginx")
    assert registry.normalize_repo("ghcr.io/a/b:v2") == registry.normalize_repo("ghcr.io/a/b@sha256:x")
    with pytest.raises(registry.RegistryError):
        registry.parse_ref("nginx@sha256:abc")


class FakeResponse:
    def __init__(self, headers=None, body=b""):
        self.headers = headers or {}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_remote_digest_mit_token(monkeypatch):
    aufrufe = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        aufrufe.append(url)
        if url.startswith("https://auth.docker.io/token"):
            assert "scope=repository%3Alibrary%2Fnginx%3Apull" in url
            return FakeResponse(body=b'{"token": "T"}')
        if req.get_header("Authorization") != "Bearer T":
            h = Message()
            h["WWW-Authenticate"] = ('Bearer realm="https://auth.docker.io/token",'
                                     'service="registry.docker.io",scope="repository:library/nginx:pull"')
            raise urllib.error.HTTPError(url, 401, "Unauthorized", h, None)
        assert req.get_method() == "HEAD"
        return FakeResponse(headers={"Docker-Content-Digest": "sha256:neu"})

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)
    assert registry.remote_digest("nginx:1.27") == "sha256:neu"
    assert aufrufe[0] == "https://registry-1.docker.io/v2/library/nginx/manifests/1.27"


def test_remote_digest_privat(monkeypatch):
    def urlopen(req, timeout=None):
        h = Message()
        h["WWW-Authenticate"] = 'Basic realm="Registry"'
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", h, None)

    monkeypatch.setattr(registry.urllib.request, "urlopen", urlopen)
    with pytest.raises(registry.RegistryError, match="Anmeldung nötig"):
        registry.remote_digest("registry.example.com/privat:1")


# --------------------------------------------------------- Update-Prüfung
@pytest.fixture
def mit_digests(fake_client, monkeypatch):
    ref = "ghcr.io/immich-app/immich-server:v2.1.0"
    fake_client.images.digests[ref] = ["ghcr.io/immich-app/immich-server@sha256:alt"]
    fake_client.images.digests["postgres:16"] = ["postgres@sha256:pg"]
    remote = {ref: "sha256:neu", "postgres:16": "sha256:pg"}

    def remote_digest(r):
        if r in remote:
            return remote[r]
        raise registry.RegistryError("Anmeldung nötig (privates Image?)")

    monkeypatch.setattr("app.image_updates.remote_digest", remote_digest)
    return fake_client


def test_update_pruefung(app, mit_digests):
    checker = app.state.checker
    assert checker.check_all() > 0
    infos = {c.name: c for c in app.state.service.list_containers(with_stats=False)}
    status = lambda n: checker.status(infos[n].image_ref, infos[n].image_id)  # noqa: E731
    assert status("immich-server").code == "update"
    assert status("immich-db").code == "aktuell"
    assert status("paperless").code == "lokal"  # keine RepoDigests -> nicht prüfbar
    # Nach „docker compose pull“ zeigt der Tag auf ein neues Image, der Container läuft noch mit dem alten:
    mit_digests.images.ids["postgres:16"] = "sha256:neueres-image"
    assert status("immich-db").code == "aktuell"  # bis zum nächsten lokalen Abgleich
    checker.refresh_locals()
    assert status("immich-db").code == "neu"


def test_update_pruefung_nicht_faellig_wenn_aus(app):
    app.state.store.update(formular(update_check=None))
    assert app.state.checker.faellig() is False


def test_update_anzeige_in_uebersicht(client, app, mit_digests):
    eingeloggt(client)
    app.state.checker.check_all()
    html = client.get("/").text
    assert 'class="marke-update"' in html and "kennzahl-update" in html
    assert "/container/" + WEB_ID + "/update" not in html  # Update-Dienst fehlt -> kein Knopf
    assert "Update-Dienst ist nicht eingerichtet" in client.get("/updates").text


# --------------------------------------------------------- Update-Aufträge
def test_container_update_ohne_dienst_abgelehnt(client, tmp_path):
    csrf = eingeloggt(client)
    r = client.post(f"/container/{WEB_ID}/update", headers=hx(csrf))
    assert "nicht eingerichtet" in r.text and auftraege(tmp_path) == []


def test_container_update_anfordern(client, app, tmp_path, mit_digests):
    updater_bereit(tmp_path)
    csrf = eingeloggt(client)
    app.state.checker.check_all()
    assert f"/container/{WEB_ID}/update" in client.get("/").text

    r = client.post(f"/container/{WEB_ID}/update", headers=hx(csrf))
    assert "läuft im Hintergrund" in r.text
    [auftrag] = auftraege(tmp_path)
    assert auftrag["aktion"] == "container" and auftrag["container"] == WEB_ID
    assert auftrag["benutzer"] == "admin" and auftrag["ref"].endswith(":v2.1.0")
    # Ein zweiter Klick legt keinen zweiten Auftrag an.
    assert "läuft bereits" in client.post(f"/container/{WEB_ID}/update", headers=hx(csrf)).text
    assert len(auftraege(tmp_path)) == 1


def test_container_update_geschuetzt_und_ohne_compose(client, tmp_path):
    updater_bereit(tmp_path)
    csrf = eingeloggt(client)
    assert "ist geschützt" in client.post("/container/prot/update", headers=hx(csrf)).text
    assert "keinem Compose-Projekt" in client.post("/container/sick/update", headers=hx(csrf)).text
    assert auftraege(tmp_path) == []


def test_fertiger_auftrag_wird_gemeldet(client, app, tmp_path):
    updater_bereit(tmp_path)
    csrf = eingeloggt(client)
    jobs = tmp_path / "updater" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "1.json").write_text(json.dumps({
        "id": "1", "phase": "fertig", "meldung": "„immich-server“ wurde aktualisiert.",
        "container": WEB_ID, "name": "immich-server", "ref": "x:1", "benutzer": "admin", "zeit": jetzt()}))
    r = client.get("/partials/liste", headers=hx(csrf))
    assert "wurde aktualisiert" in r.text
    assert "wurde aktualisiert" not in client.get("/partials/liste", headers=hx(csrf)).text  # nur einmal
    eintrag = app.state.action_log.entries(limit=1)[0]
    assert eintrag["aktion"] == "update" and eintrag["ok"]


def test_dashboard_update_ablauf(client, tmp_path):
    csrf = eingeloggt(client)
    assert "nicht eingerichtet" in client.post("/updates/dashboard/pruefen", headers=hx(csrf)).text

    updater_bereit(tmp_path)
    r = client.post("/updates/dashboard/pruefen", headers=hx(csrf))
    assert "Suche nach einer neuen Version" in r.text
    assert auftraege(tmp_path)[-1]["aktion"] == "vorbereiten"
    # Solange ein Auftrag offen ist, kein weiterer.
    assert "beschäftigt" in client.post("/updates/dashboard/umschalten", headers=hx(csrf)).text

    for p in (tmp_path / "updater" / "auftraege").glob("*.json"):
        p.unlink()
    assert "keine neue Version" in client.post("/updates/dashboard/umschalten", headers=hx(csrf)).text

    updater_bereit(tmp_path, phase="bereit", bereit="b" * 40,
                   commits=[{"commit": "b" * 40, "message": "Neue Funktion"}])
    seite = client.get("/updates").text
    assert "Neue Funktion" in seite and "Jetzt aktualisieren" in seite
    client.post("/updates/dashboard/umschalten", headers=hx(csrf))
    assert auftraege(tmp_path)[-1]["aktion"] == "umschalten"


def test_veralteter_dienst_gilt_als_nicht_verfuegbar(client, tmp_path):
    ordner = tmp_path / "updater"
    ordner.mkdir()
    (ordner / "status.json").write_text(json.dumps({"phase": "aktuell", "zeit": "2020-01-01T00:00:00Z"}))
    eingeloggt(client)
    assert "meldet sich nicht mehr" in client.get("/updates").text


def test_aktionslog_leeren(client, app):
    csrf = eingeloggt(client)
    client.post("/container/web/restart", headers=hx(csrf))
    client.post("/einstellungen/aktionslog-leeren", headers=hx(csrf))
    eintraege = app.state.action_log.entries()
    assert len(eintraege) == 1 and eintraege[0]["aktion"] == "aktionslog"


def test_post_ohne_csrf_ueberall_abgelehnt(client):
    eingeloggt(client)
    for url in ("/einstellungen/allgemein", "/einstellungen/zugang", "/einstellungen/container",
                "/einstellungen/aktionslog-leeren", "/updates/dashboard/pruefen",
                "/updates/images/pruefen", f"/container/{WEB_ID}/update"):
        assert client.post(url, headers={"HX-Request": "true"}).status_code == 403, url


def test_app_startet_ohne_beschreibbaren_datenordner(tmp_path):
    ordner = tmp_path / "ro"
    ordner.mkdir()
    ordner.chmod(0o500)
    try:
        s = replace(BASE_SETTINGS, data_dir=str(ordner), session_secret="")
        create_app(s, client=object(), background=False)  # darf nicht abstürzen
    finally:
        ordner.chmod(0o700)


def test_container_auftrag_blockiert_dashboard_karte_nicht(client, tmp_path):
    updater_bereit(tmp_path)
    csrf = eingeloggt(client)
    app_bridge = client.app.state.bridge
    app_bridge.request("container", "admin", container=WEB_ID, name="immich-server")
    assert app_bridge.status()["auftrag_offen"] is False
    assert "Suche nach einer neuen Version" in client.post("/updates/dashboard/pruefen", headers=hx(csrf)).text
