from app.docker_service import PortInfo, parse_ports, safe_url
from tests.conftest import FakeContainer, csrf_from, hex_id, hx, login


def test_ports_auslesen_und_doppelte_zusammenfassen():
    attrs = {"NetworkSettings": {"Ports": {
        "2283/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2283"}, {"HostIp": "::", "HostPort": "2283"}],
        "53/udp": [{"HostIp": "", "HostPort": "53"}],
        "5432/tcp": None,  # nur EXPOSE
        "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9443"}],
    }}}
    ports = parse_ports(attrs)
    assert [(p.host_port, p.proto, p.host_ip) for p in ports] == [
        (53, "udp", ""), (2283, "tcp", ""), (9443, "tcp", "127.0.0.1")]


def test_gestoppter_container_nutzt_konfiguration():
    attrs = {"NetworkSettings": {"Ports": {}},
             "HostConfig": {"PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}}}
    assert parse_ports(attrs)[0].host_port == 8080


def test_link_bauen():
    assert PortInfo("", 2283, 2283, "tcp").url("nas.tail1234.ts.net") == "http://nas.tail1234.ts.net:2283"
    assert PortInfo("", 443, 443, "tcp").url("192.168.1.5") == "https://192.168.1.5"
    assert PortInfo("", 9443, 8443, "tcp").url("h") == "https://h:9443"
    assert PortInfo("", 80, 80, "tcp").url("h") == "http://h"
    assert PortInfo("192.168.1.10", 81, 80, "tcp").url("andere") == "http://192.168.1.10:81"
    assert PortInfo("fd00::1", 81, 80, "tcp").url("h") == "http://[fd00::1]:81"
    assert PortInfo("127.0.0.1", 81, 80, "tcp").url("h") is None   # nur lokal
    assert PortInfo("", 53, 53, "udp").url("h") is None


def test_nur_http_links_erlaubt():
    assert safe_url("https://fotos.example.lan") == "https://fotos.example.lan"
    assert safe_url("HTTP://x:8080/pfad") == "HTTP://x:8080/pfad"
    for boese in ("javascript:alert(1)", "data:text/html,x", "//x", "ftp://x", "https://", " ", None):
        assert safe_url(boese) is None, boese


def _mit_ports(fake_client):
    neu = []
    neu.append(FakeContainer(
        "foto", "fotos", labels={"com.docker.compose.project": "fotos",
                                 "dashboard.url": "https://fotos.example.lan"},
        ports={"2283/tcp": [{"HostIp": "0.0.0.0", "HostPort": "2283"}]}))
    neu.append(FakeContainer(
        "aus", "gestoppt", status="exited",
        ports={"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8081"}]}))
    neu.append(FakeContainer(
        "boese", "boese", labels={"dashboard.url": "javascript:alert(1)"}))
    for c in neu:
        fake_client.containers.items[c.id] = c


def test_ports_in_uebersicht_und_detail(client, fake_client):
    _mit_ports(fake_client)
    login(client)
    html = client.get("/").text
    assert 'href="http://testserver:2283"' in html
    assert 'href="https://fotos.example.lan"' in html and "Öffnen ↗" in html
    assert 'id="ports-anzeigen"' in html
    # Gestoppter Container: Port sichtbar, aber nicht anklickbar
    assert 'href="http://testserver:8081"' not in html and ">8081<" in html
    assert "javascript:" not in html
    detail = client.get(f"/container/{hex_id('foto')}").text
    assert "Ports und Links" in detail and "ports-immer" in detail


def test_link_in_einstellungen(client, fake_client, app):
    _mit_ports(fake_client)
    login(client)
    csrf = csrf_from(client.get("/").text)
    r = client.post("/einstellungen/container", headers=hx(csrf),
                    data={"name": "fotos", "link": "javascript:alert(1)"})
    assert "http:// oder https://" in r.text and "fotos" not in app.state.store.links

    client.post("/einstellungen/container", headers=hx(csrf),
                data={"name": "fotos", "link": "http://192.168.1.5:2283"})
    assert app.state.store.links == {"fotos": "http://192.168.1.5:2283"}
    assert 'href="http://192.168.1.5:2283"' in client.get("/").text  # Einstellung vor Label

    client.post("/einstellungen/container", headers=hx(csrf), data={"name": "fotos", "link": ""})
    assert app.state.store.links == {}
    assert 'href="https://fotos.example.lan"' in client.get("/").text  # wieder das Label
    seite = client.get("/einstellungen").text
    assert 'placeholder="https://fotos.example.lan"' in seite


def test_checkbox_aenderung_laesst_link_stehen(client, fake_client, app):
    _mit_ports(fake_client)
    login(client)
    csrf = csrf_from(client.get("/").text)
    client.post("/einstellungen/container", headers=hx(csrf),
                data={"name": "fotos", "link": "http://x:1"})
    # Ein Formular ohne Linkfeld (ältere Seite im Browser) löscht den Link nicht.
    client.post("/einstellungen/container", headers=hx(csrf), data={"name": "fotos", "geschuetzt": "on"})
    assert app.state.store.links == {"fotos": "http://x:1"}
