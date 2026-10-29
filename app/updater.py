"""Verbindung zum Update-Dienst auf dem Docker-Server (updater/dashboard-updater.sh).

Die App legt Aufträge als Dateien in DATA_DIR/updater/auftraege/ ab. Der Dienst arbeitet sie
ab und schreibt den Stand nach status.json (Dashboard-Version) bzw. jobs/<id>.json
(Container-Updates). Der Container braucht dafür keinerlei Rechte am Docker-Host.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import fmt
from .jsonfile import read_json

log = logging.getLogger("dashboard")

STALE_AFTER = timedelta(hours=3)  # Dienst läuft stündlich – länger still = nicht verbunden
BUSY = ("vorbereiten", "umschalten")
JOB_FERTIG = ("fertig", "fehler")
CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")


def _version_file() -> Path:
    return Path(__file__).resolve().parent / "version.json"


def running_version() -> dict[str, str]:
    """Vom Docker-Build geschriebene Version (commit, branch, datum)."""
    daten = read_json(_version_file(), {})
    return daten if isinstance(daten, dict) else {}


class UpdaterBridge:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "updater"
        self.auftraege = self.dir / "auftraege"
        self.jobs_dir = self.dir / "jobs"
        self._lock = threading.Lock()
        # Beim Start bereits fertige Aufträge gelten als gemeldet.
        self._gemeldet: set[str] = {j["id"] for j in self.jobs() if j.get("phase") in JOB_FERTIG}

    # -------------------------------------------------------------- Zustand
    def status(self) -> dict[str, Any]:
        daten = read_json(self.dir / "status.json", None)
        if not isinstance(daten, dict):
            return {"verfuegbar": False, "phase": None, "commits": []}
        gesehen = fmt.parse_docker_time(daten.get("zeit"))
        veraltet = not gesehen or datetime.now(timezone.utc) - gesehen > STALE_AFTER
        return {
            **daten,
            "commits": daten.get("commits") or [],
            "verfuegbar": not veraltet or daten.get("phase") in BUSY,
            "veraltet": veraltet,
            "beschaeftigt": daten.get("phase") in BUSY,
            # Nur Aufträge fürs Dashboard selbst – Container-Updates laufen unabhängig davon.
            "auftrag_offen": self.open_requests(("vorbereiten", "umschalten")) > 0,
        }

    def open_requests(self, aktionen: tuple[str, ...] | None = None) -> int:
        try:
            dateien = list(self.auftraege.glob("*.json"))
        except OSError:
            return 0
        if aktionen is None:
            return len(dateien)
        return sum(1 for p in dateien if (read_json(p, {}) or {}).get("aktion") in aktionen)

    def jobs(self, limit: int | None = None) -> list[dict[str, Any]]:
        ergebnis = []
        try:
            dateien = list(self.jobs_dir.glob("*.json"))
        except OSError:
            dateien = []
        for pfad in dateien:
            job = read_json(pfad, None)
            if isinstance(job, dict) and job.get("id"):
                ergebnis.append(job)
        ergebnis.sort(key=lambda j: j.get("zeit") or "", reverse=True)
        return ergebnis[:limit] if limit else ergebnis

    def pending_containers(self) -> set[str]:
        """Container-IDs mit offenem oder laufendem Update."""
        ids = set()
        try:
            for pfad in self.auftraege.glob("*.json"):
                auftrag = read_json(pfad, {})
                if isinstance(auftrag, dict) and auftrag.get("container"):
                    ids.add(auftrag["container"])
        except OSError:
            pass
        for job in self.jobs(limit=50):
            if job.get("phase") not in JOB_FERTIG and job.get("container"):
                ids.add(job["container"])
        return ids

    def new_finished(self) -> list[dict[str, Any]]:
        """Seit dem letzten Aufruf fertig gewordene Container-Updates (für Hinweis und Aktionslog)."""
        neu = []
        with self._lock:
            for job in self.jobs(limit=50):
                if job.get("phase") in JOB_FERTIG and job["id"] not in self._gemeldet:
                    self._gemeldet.add(job["id"])
                    neu.append(job)
        return neu

    # -------------------------------------------------------------- Aufträge
    def request(self, aktion: str, benutzer: str, **extra: str) -> str:
        """Auftrag ablegen. Gibt die Auftrags-ID zurück."""
        if aktion == "container" and not CONTAINER_ID.match(extra.get("container", "")):
            raise ValueError("ungültige Container-ID")
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        auftrag = {"id": job_id, "aktion": aktion, "benutzer": benutzer,
                   "zeit": datetime.now(timezone.utc).isoformat(timespec="seconds"), **extra}
        self.auftraege.mkdir(parents=True, exist_ok=True)
        # Erst unter anderem Namen schreiben, dann umbenennen: der Dienst reagiert auf *.json
        # und soll nie eine halbe Datei lesen.
        tmp = self.auftraege / f".{job_id}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(auftrag, f, ensure_ascii=False)
        os.replace(tmp, self.auftraege / f"{job_id}.json")
        log.info("Auftrag an den Update-Dienst: %s %s", aktion, extra or "")
        return job_id
