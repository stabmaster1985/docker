"""Zugriff auf Docker über den Socket-Proxy und Aufbereitung für die Anzeige."""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

import docker
from docker.errors import APIError, DockerException, NotFound

from . import fmt

log = logging.getLogger("dashboard")

LABEL_PROJECT = "com.docker.compose.project"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_HIDE = "dashboard.hide"
LABEL_PROTECT = "dashboard.protect"
LABEL_URL = "dashboard.url"

ACTIONS = {"start": "Start", "stop": "Stopp", "restart": "Neustart"}
# Bezeichnungen fürs Aktionslog (Container-Aktionen und alles andere)
LOG_ACTIONS = {
    **ACTIONS,
    "update": "Update",
    "update-angefordert": "Update angefordert",
    "dashboard-update": "Dashboard-Update",
    "update-pruefung": "Update-Prüfung",
    "einstellungen": "Einstellungen",
    "container-einstellung": "Ausblenden/Schützen",
    "zugang": "Zugangsdaten",
    "aktionslog": "Aktionslog",
}

STATUS_TEXT = {
    "running": "läuft",
    "exited": "beendet",
    "restarting": "startet neu",
    "paused": "pausiert",
    "created": "erstellt",
    "dead": "tot",
    "removing": "wird entfernt",
}
HEALTH_TEXT = {"healthy": "gesund", "unhealthy": "ungesund", "starting": "prüft …"}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _truthy(wert: str | None) -> bool:
    return (wert or "").strip().lower() in {"1", "true", "yes", "ja", "on"}


def split_image(ref: str) -> tuple[str, str]:
    """„ghcr.io:443/foo/bar:1.2“ -> („ghcr.io:443/foo/bar“, „1.2“)."""
    if "@" in ref:
        name, digest = ref.split("@", 1)
        name, tag = split_image(name) if ":" in name.rsplit("/", 1)[-1] else (name, "")
        return name, tag or digest[:19]
    letzter = ref.rsplit("/", 1)[-1]
    if ":" in letzter:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest"


def safe_url(wert: str | None) -> str | None:
    """Nur http(s)-Links zulassen – nie „javascript:“ o. Ä. aus Labels oder Formularen."""
    wert = (wert or "").strip()
    if len(wert) > 500 or not re.match(r"^https?://[^\s/?#]+", wert, re.IGNORECASE):
        return None
    return wert


WILDCARD_IPS = ("", "0.0.0.0", "::")
HTTPS_PORTS = (443, 8443)


@dataclass(frozen=True)
class PortInfo:
    host_ip: str  # "" = alle Adressen
    host_port: int
    container_port: int
    proto: str

    @property
    def nur_lokal(self) -> bool:
        return self.host_ip in ("127.0.0.1", "::1") or self.host_ip.startswith("127.")

    @property
    def titel(self) -> str:
        wo = f"{self.host_ip}:" if self.host_ip else ""
        text = f"Host {wo}{self.host_port} → Container {self.container_port}/{self.proto}"
        return text + (" (nur lokal auf dem Server)" if self.nur_lokal else "")

    def url(self, host: str | None) -> str | None:
        """Link mit der Adresse, über die das Dashboard gerade aufgerufen wird."""
        if self.proto != "tcp" or self.nur_lokal:
            return None
        ziel = self.host_ip or host
        if not ziel:
            return None
        if ":" in ziel and not ziel.startswith("["):
            ziel = f"[{ziel}]"  # IPv6
        https = self.container_port in HTTPS_PORTS or self.host_port in HTTPS_PORTS
        schema = "https" if https else "http"
        port = "" if self.host_port == (443 if https else 80) else f":{self.host_port}"
        return f"{schema}://{ziel}{port}"


def parse_ports(attrs: dict) -> list[PortInfo]:
    """Veröffentlichte Ports; bei gestoppten Containern aus der Konfiguration."""
    ports = (attrs.get("NetworkSettings") or {}).get("Ports") or \
        (attrs.get("HostConfig") or {}).get("PortBindings") or {}
    gesehen: set[tuple] = set()
    ergebnis = []
    for schluessel, bindungen in ports.items():
        if not bindungen:
            continue  # nur EXPOSE, nicht veröffentlicht
        cport, _, proto = schluessel.partition("/")
        for b in bindungen:
            hport = str(b.get("HostPort") or "")
            if not hport.isdigit() or not cport.isdigit():
                continue
            ip = b.get("HostIp") or ""
            ip = "" if ip in WILDCARD_IPS else ip
            key = (ip, int(hport), proto or "tcp")
            if key in gesehen:  # 0.0.0.0 und :: sind derselbe Port
                continue
            gesehen.add(key)
            ergebnis.append(PortInfo(ip, int(hport), int(cport), proto or "tcp"))
    ergebnis.sort(key=lambda p: (p.host_port, p.proto, p.host_ip))
    return ergebnis


class DashboardError(Exception):
    """Fehler mit Meldung für die Oberfläche."""


@dataclass
class ContainerInfo:
    id: str
    name: str
    project: str
    service: str
    status: str
    exit_code: int | None
    health: str | None
    started_at: datetime | None
    finished_at: datetime | None
    restart_count: int
    image_name: str
    image_tag: str
    image_created: datetime | None
    image_ref: str = ""
    image_id: str = ""
    compose_dir: str = ""
    protected_reason: str | None = None
    protected_fixed: bool = False
    hidden_reason: str | None = None
    update: object | None = None  # ImageStatus, wird von main.py gesetzt
    update_pending: bool = False
    ports: list[PortInfo] = field(default_factory=list)
    link: str | None = None
    link_label: str | None = None  # aus dashboard.url, für die Einstellungsseite
    cpu_percent: float | None = None
    mem_usage: int | None = None
    mem_limit: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def running(self) -> bool:
        return self.status in ("running", "restarting")

    @property
    def protected(self) -> bool:
        return self.protected_reason is not None

    @property
    def auffaellig(self) -> bool:
        return bool(self.problems)

    @property
    def can_update(self) -> bool:
        """Aktualisieren geht nur für Compose-Container und nie für geschützte."""
        return bool(self.project and self.service and self.compose_dir) and not self.protected

    @property
    def status_text(self) -> str:
        text = STATUS_TEXT.get(self.status, self.status)
        if self.status == "exited" and self.exit_code is not None:
            text += f" (Code {self.exit_code})"
        return text

    @property
    def health_text(self) -> str:
        return HEALTH_TEXT.get(self.health or "", "–")

    @property
    def laufzeit(self) -> str:
        if self.status == "running":
            return fmt.dauer(fmt.seit(self.started_at))
        if self.finished_at:
            return "aus seit " + fmt.dauer(fmt.seit(self.finished_at))
        return "–"

    @property
    def image_alter(self) -> str:
        return fmt.alter(self.image_created)

    @property
    def mem_percent(self) -> float | None:
        if self.mem_usage is None or not self.mem_limit:
            return None
        return self.mem_usage / self.mem_limit * 100

    @property
    def ampel(self) -> str:
        """CSS-Klasse für den Status-Punkt."""
        if self.auffaellig:
            return "rot"
        if self.status == "running":
            return "gelb" if self.health == "starting" else "gruen"
        return "grau"


@dataclass
class Group:
    name: str
    containers: list[ContainerInfo]

    @property
    def problem_count(self) -> int:
        return sum(1 for c in self.containers if c.auffaellig)

    @property
    def running_count(self) -> int:
        return sum(1 for c in self.containers if c.status == "running")


class RestartTracker:
    """Merkt sich Restart-Zähler über die Zeit, um Restart-Schleifen zu erkennen."""

    def __init__(self) -> None:
        self._seen: dict[str, list[tuple[float, int]]] = defaultdict(list)
        self._lock = threading.Lock()

    def observe(self, container_id: str, restart_count: int, window: int,
                now: float | None = None) -> int:
        """Speichert eine Beobachtung, gibt die Restarts im Zeitfenster zurück."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            verlauf = [(t, n) for t, n in self._seen[container_id] if now - t <= window]
            # Zähler kann durch manuellen Start auf 0 fallen – dann neu beginnen.
            if verlauf and restart_count < verlauf[-1][1]:
                verlauf = []
            verlauf.append((now, restart_count))
            self._seen[container_id] = verlauf
            return restart_count - verlauf[0][1]

    def is_loop(self, info: ContainerInfo, threshold: int, window: int,
                now: float | None = None) -> bool:
        if info.status == "restarting":
            return True
        if self.observe(info.id, info.restart_count, window, now) >= threshold:
            return True
        # Ohne Verlauf (z. B. direkt nach Start des Dashboards): viele Restarts
        # und erst kurz wieder oben gilt ebenfalls als Schleife.
        laufzeit = fmt.seit(info.started_at)
        return (
            info.restart_count >= threshold
            and laufzeit is not None
            and laufzeit < window
        )

    def forget_missing(self, ids: set[str]) -> None:
        with self._lock:
            for cid in list(self._seen):
                if cid not in ids:
                    del self._seen[cid]


class DockerService:
    def __init__(self, store, client: docker.DockerClient | None = None,
                 hostname: str | None = None) -> None:
        self.store = store
        self._client = client
        self._client_lock = threading.Lock()
        self.hostname = hostname or os.environ.get("HOSTNAME") or socket.gethostname()
        self.restarts = RestartTracker()
        self._image_cache: dict[str, tuple[str | None, list[str]]] = {}
        self._pool = ThreadPoolExecutor(max_workers=max(1, store.base.stats_workers),
                                        thread_name_prefix="stats")
        self.own_id: str | None = None
        self.own_project: str | None = None
        self._self_checked = False

    # ------------------------------------------------------------------ Client
    @property
    def client(self) -> docker.DockerClient:
        with self._client_lock:
            if self._client is None:
                s = self.store.base
                self._client = docker.DockerClient(
                    base_url=s.docker_host,
                    version=s.docker_api_version,
                    timeout=s.docker_timeout,
                    max_pool_size=s.stats_workers + 4,
                )
            return self._client

    def ping(self) -> bool:
        # /_ping ist im Proxy gesperrt, /info (INFO=1) ist erlaubt.
        try:
            self.client.info()
            return True
        except Exception:  # noqa: BLE001 – Healthcheck soll nie werfen
            return False

    # ------------------------------------------------------------- Auslesen
    def _image_meta(self, image_id: str) -> tuple[str | None, list[str]]:
        if image_id not in self._image_cache:
            try:
                attrs = self.client.images.get(image_id).attrs
                self._image_cache[image_id] = (attrs.get("Created"), attrs.get("RepoTags") or [])
            except (NotFound, APIError):
                self._image_cache[image_id] = (None, [])
        return self._image_cache[image_id]

    def image_attrs(self, ref: str) -> dict | None:
        """Lokales Image zu einer Referenz (für die Update-Prüfung)."""
        try:
            return self.client.images.get(ref).attrs
        except NotFound:
            return None

    def _protection(self, attrs: dict, labels: dict, image_ref: str,
                    name: str) -> tuple[str | None, bool]:
        """(Grund, fest) – „fest“ heißt: in der Oberfläche nicht abschaltbar."""
        if attrs["Id"] == self.own_id:
            return "das Dashboard selbst", True
        if self.own_project and labels.get(LABEL_PROJECT) == self.own_project:
            return "Teil des Dashboard-Stacks", True
        if "docker-socket-proxy" in image_ref:
            return "Socket-Proxy", True
        if _truthy(labels.get(LABEL_PROTECT)):
            return "per Label geschützt", True
        if name in self.store.protected_names:
            return "in den Einstellungen geschützt", False
        return None, False

    def _hidden(self, c) -> str | None:
        if _truthy((c.labels or {}).get(LABEL_HIDE)):
            return "per Label"
        if self._name(c) in self.store.hidden_names:
            return "in den Einstellungen"
        return None

    @staticmethod
    def _name(c) -> str:
        return (c.attrs.get("Name") or c.name or c.id[:12]).lstrip("/")

    def _detect_self(self, containers: list) -> None:
        self._self_checked = True
        for c in containers:
            if c.id.startswith(self.hostname) or c.attrs.get("Config", {}).get("Hostname") == self.hostname:
                self.own_id = c.id
                self.own_project = (c.labels or {}).get(LABEL_PROJECT)
                return

    def _to_info(self, c) -> ContainerInfo:
        attrs = c.attrs
        state = attrs.get("State", {})
        labels = c.labels or {}
        image_ref = attrs.get("Config", {}).get("Image") or ""
        created, repo_tags = self._image_meta(attrs.get("Image", ""))
        anzeige_ref = image_ref
        if not anzeige_ref or anzeige_ref.startswith("sha256:"):
            anzeige_ref = repo_tags[0] if repo_tags else anzeige_ref[:19]
        image_name, image_tag = split_image(anzeige_ref)
        name = self._name(c)
        health = (state.get("Health") or {}).get("Status")
        schutz, schutz_fest = self._protection(attrs, labels, image_ref, name)
        link_label = safe_url(labels.get(LABEL_URL))

        info = ContainerInfo(
            id=c.id,
            name=name,
            project=labels.get(LABEL_PROJECT, ""),
            service=labels.get(LABEL_SERVICE, ""),
            status=state.get("Status", "unknown"),
            exit_code=state.get("ExitCode"),
            health=health,
            started_at=fmt.parse_docker_time(state.get("StartedAt")),
            finished_at=fmt.parse_docker_time(state.get("FinishedAt")),
            restart_count=int(attrs.get("RestartCount") or 0),
            image_name=image_name,
            image_tag=image_tag,
            image_created=fmt.parse_docker_time(created),
            image_ref=image_ref,
            image_id=attrs.get("Image", ""),
            compose_dir=labels.get(LABEL_WORKDIR, ""),
            protected_reason=schutz,
            protected_fixed=schutz_fest,
            hidden_reason=self._hidden(c),
            ports=parse_ports(attrs),
            # Link aus den Einstellungen geht vor dem Label.
            link=safe_url(self.store.links.get(name)) or link_label,
            link_label=link_label,
        )
        info.problems = self._problems(info)
        return info

    def _problems(self, info: ContainerInfo) -> list[str]:
        s = self.store.s
        probleme = []
        if self.restarts.is_loop(info, s.restart_loop_threshold, s.restart_loop_window):
            probleme.append("Restart-Schleife")
        if info.status == "dead":
            probleme.append("tot")
        if info.status == "exited" and not (s.ignore_exit_zero and info.exit_code == 0):
            probleme.append(f"beendet (Code {info.exit_code})")
        if info.health == "unhealthy":
            probleme.append("ungesund")
        return probleme

    def _stats(self, c) -> tuple[float | None, int | None, int | None]:
        try:
            s = c.stats(stream=False)
        except (APIError, DockerException, OSError) as exc:
            log.debug("Stats für %s nicht verfügbar: %s", c.name, exc)
            return None, None, None
        return compute_cpu(s), *compute_mem(s)

    def list_containers(self, with_stats: bool = True,
                        include_hidden: bool = False) -> list[ContainerInfo]:
        try:
            raw = self.client.containers.list(all=True)
        except (APIError, DockerException, OSError) as exc:
            raise DashboardError(f"Docker nicht erreichbar: {exc}") from exc

        if not self._self_checked:
            self._detect_self(raw)
        sichtbar = raw if include_hidden else [c for c in raw if not self._hidden(c)]
        infos = [self._to_info(c) for c in sichtbar]
        self.restarts.forget_missing({c.id for c in raw})

        if with_stats:
            laufend = [(i, c) for i, c in zip(infos, sichtbar) if i.status == "running"]
            for (info, _), (cpu, mem, limit) in zip(
                laufend, self._pool.map(lambda pair: self._stats(pair[1]), laufend)
            ):
                info.cpu_percent, info.mem_usage, info.mem_limit = cpu, mem, limit
        return infos

    def _get_visible(self, container_id: str):
        try:
            c = self.client.containers.get(container_id)
        except NotFound as exc:
            raise DashboardError("Container nicht gefunden.") from exc
        except (APIError, DockerException, OSError) as exc:
            raise DashboardError(f"Docker nicht erreichbar: {exc}") from exc
        if self._hidden(c):
            raise DashboardError("Container nicht gefunden.")
        return c

    def get(self, container_id: str, with_stats: bool = True) -> ContainerInfo:
        if not self._self_checked:
            self.list_containers(with_stats=False)
        c = self._get_visible(container_id)
        info = self._to_info(c)
        if with_stats and info.status == "running":
            info.cpu_percent, info.mem_usage, info.mem_limit = self._stats(c)
        return info

    def logs(self, container_id: str, tail: int) -> list[dict]:
        c = self._get_visible(container_id)
        try:
            roh = c.logs(stdout=True, stderr=True, tail=tail, timestamps=True)
        except (APIError, DockerException, OSError) as exc:
            raise DashboardError(f"Logs nicht abrufbar: {exc}") from exc
        zeilen = []
        for zeile in roh.decode("utf-8", errors="replace").splitlines():
            zeile = ANSI_RE.sub("", zeile)
            ts, _, text = zeile.partition(" ")
            dt = fmt.parse_docker_time(ts)
            if dt is None:
                ts, text = "", zeile
            else:
                ts = dt.astimezone().strftime("%d.%m. %H:%M:%S")
            zeilen.append({"zeit": ts, "text": text})
        return zeilen

    # -------------------------------------------------------------- Aktionen
    def run_action(self, container_id: str, action: str) -> tuple[str, str]:
        """Führt die Aktion aus. Gibt (Containername, Meldung) zurück."""
        if action not in ACTIONS:
            raise DashboardError("Unbekannte Aktion.")
        # Schutz immer serverseitig prüfen, nie nur in der Oberfläche.
        if not self._self_checked:
            self.list_containers(with_stats=False)
        c = self._get_visible(container_id)
        info = self._to_info(c)
        if info.protected:
            raise DashboardError(f"„{info.name}“ ist geschützt ({info.protected_reason}).")
        try:
            if action == "start":
                c.start()
                meldung = f"„{info.name}“ wurde gestartet."
            elif action == "stop":
                c.stop(timeout=10)
                meldung = f"„{info.name}“ wurde gestoppt."
            else:
                c.restart(timeout=10)
                meldung = f"„{info.name}“ wurde neu gestartet."
        except (APIError, DockerException, OSError) as exc:
            grund = getattr(exc, "explanation", None) or str(exc)
            raise DashboardError(f"{ACTIONS[action]} von „{info.name}“ fehlgeschlagen: {grund}") from exc
        return info.name, meldung


# ---------------------------------------------------------------- Hilfen
def compute_cpu(s: dict) -> float | None:
    try:
        cpu = s["cpu_stats"]
        pre = s.get("precpu_stats") or {}
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        cpus = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
    except (KeyError, TypeError):
        return None
    if sys_delta <= 0 or cpu_delta < 0 or not pre.get("system_cpu_usage"):
        return None
    return cpu_delta / sys_delta * cpus * 100


def compute_mem(s: dict) -> tuple[int | None, int | None]:
    mem = s.get("memory_stats") or {}
    usage = mem.get("usage")
    if usage is None:
        return None, None
    stats = mem.get("stats") or {}
    # Wie `docker stats`: Page-Cache abziehen (cgroup v2: inactive_file, v1: total_inactive_file).
    cache = stats.get("inactive_file", stats.get("total_inactive_file", 0))
    if cache < usage:
        usage -= cache
    return usage, mem.get("limit")


def group_containers(infos: list[ContainerInfo]) -> list[Group]:
    gruppen: dict[str, list[ContainerInfo]] = defaultdict(list)
    for info in infos:
        gruppen[info.project].append(info)
    ergebnis = [
        Group(name or "Ohne Compose-Projekt", sorted(cs, key=lambda c: (c.service or c.name).lower()))
        for name, cs in gruppen.items()
    ]
    # Projekte alphabetisch, Einzelcontainer ans Ende.
    ergebnis.sort(key=lambda g: (g.name == "Ohne Compose-Projekt", g.name.lower()))
    return ergebnis
