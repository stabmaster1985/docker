"""Gefälschter Docker-Client für Tests – kein echter Docker nötig."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from docker.errors import NotFound
from fastapi.testclient import TestClient

from app.config import Settings
from app.docker_service import DockerService
from app.main import create_app


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class FakeContainer:
    def __init__(self, cid, name, status="running", labels=None, image="nginx:1.27",
                 health=None, restart_count=0, exit_code=0, started_ago=3600, hostname=None):
        jetzt = datetime.now(timezone.utc)
        self.id = cid.ljust(64, "0")
        self.name = name
        self.labels = labels or {}
        self.calls: list[str] = []
        state = {
            "Status": status,
            "ExitCode": exit_code,
            "StartedAt": iso(jetzt - timedelta(seconds=started_ago)),
            "FinishedAt": iso(jetzt - timedelta(seconds=60)) if status != "running" else "0001-01-01T00:00:00Z",
        }
        if health:
            state["Health"] = {"Status": health}
        self.attrs = {
            "Id": self.id,
            "Name": "/" + name,
            "State": state,
            "RestartCount": restart_count,
            "Image": "sha256:img-" + image,
            "Config": {"Image": image, "Labels": self.labels, "Hostname": hostname or self.id[:12]},
        }

    def start(self):
        self.calls.append("start")

    def stop(self, timeout=10):
        self.calls.append("stop")

    def restart(self, timeout=10):
        self.calls.append("restart")

    def stats(self, stream=False):
        return {
            "cpu_stats": {"cpu_usage": {"total_usage": 2_000}, "system_cpu_usage": 100_000, "online_cpus": 4},
            "precpu_stats": {"cpu_usage": {"total_usage": 1_000}, "system_cpu_usage": 90_000},
            "memory_stats": {"usage": 300 * 2**20, "limit": 1024 * 2**20, "stats": {"inactive_file": 44 * 2**20}},
        }

    def logs(self, **kwargs):
        return (
            b"2026-10-29T12:00:00.000000000Z Server gestartet\n"
            b"2026-10-29T12:00:01.000000000Z \x1b[31mERROR\x1b[0m Datenbank weg\n"
        )


class FakeImage:
    def __init__(self, created):
        self.attrs = {"Created": created, "RepoTags": []}


class FakeContainers:
    def __init__(self, items):
        self.items = {c.id: c for c in items}

    def list(self, all=False):
        return list(self.items.values())

    def get(self, cid):
        for c in self.items.values():
            if c.id == cid or c.id.startswith(cid) or c.name == cid:
                return c
        raise NotFound("nicht da")


class FakeImages:
    def get(self, image_id):
        return FakeImage(iso(datetime.now(timezone.utc) - timedelta(days=95)))


class FakeClient:
    def __init__(self, items):
        self.containers = FakeContainers(items)
        self.images = FakeImages()

    def info(self):
        return {}


def default_containers():
    return [
        FakeContainer("dash", "docker-dashboard-dashboard-1",
                      labels={"com.docker.compose.project": "docker-dashboard",
                              "com.docker.compose.service": "dashboard"},
                      image="docker-dashboard:latest"),
        FakeContainer("proxy", "docker-dashboard-socket-proxy-1",
                      labels={"com.docker.compose.project": "docker-dashboard"},
                      image="tecnativa/docker-socket-proxy:latest"),
        FakeContainer("web", "immich-server",
                      labels={"com.docker.compose.project": "immich"}, image="ghcr.io/immich-app/immich-server:v2.1.0"),
        FakeContainer("db", "immich-db", status="exited", exit_code=1,
                      labels={"com.docker.compose.project": "immich"}, image="postgres:16"),
        FakeContainer("sick", "paperless", health="unhealthy",
                      labels={"com.docker.compose.project": "paperless"}),
        FakeContainer("secret", "geheim", labels={"dashboard.hide": "true"}),
        FakeContainer("prot", "router", labels={"dashboard.protect": "true"}),
        FakeContainer("loop", "flapper", status="restarting", restart_count=7),
    ]


BASE_SETTINGS = Settings(
    username="admin", password="geheim123", session_secret="test-secret",
    session_max_age=3600, cookie_secure=False, docker_host="tcp://x:2375",
    docker_api_version="1.44", docker_timeout=5, data_dir="/tmp", action_log_max=100,
    refresh_seconds=15, log_tail=200, restart_loop_threshold=3, restart_loop_window=600,
    ignore_exit_zero=False, login_max_attempts=3, login_lock_seconds=60, stats_workers=2,
)


@pytest.fixture
def settings(tmp_path):
    return replace(BASE_SETTINGS, data_dir=str(tmp_path))


@pytest.fixture
def containers():
    return default_containers()


@pytest.fixture
def service(settings, containers):
    # Hostname = Kurz-ID des Dashboard-Containers, wie in Docker üblich.
    return DockerService(settings, client=FakeClient(containers), hostname="dash00000000")


@pytest.fixture
def app(settings, service):
    return create_app(settings, service)


@pytest.fixture
def client(app):
    return TestClient(app)


def login(client: TestClient, user="admin", password="geheim123"):
    seite = client.get("/login")
    csrf = seite.text.split('name="csrf" value="')[1].split('"')[0]
    return client.post("/login", data={"username": user, "password": password, "csrf": csrf},
                       follow_redirects=False)


def csrf_from(html: str) -> str:
    return html.split('"X-CSRF-Token": "')[1].split('"')[0]
