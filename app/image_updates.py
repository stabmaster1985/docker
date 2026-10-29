"""Prüft im Hintergrund, ob es für die Images der Container neuere Versionen gibt.

Ergebnis je Image-Referenz (z. B. „nginx:1.27“) in data/image-updates.json:
lokaler Image-Stand (ID und Digests) und Digest des Tags in der Registry.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import fmt
from .jsonfile import read_json, write_json
from .registry import RegistryError, normalize_repo, remote_digest

log = logging.getLogger("dashboard")


@dataclass(frozen=True)
class ImageStatus:
    code: str
    text: str
    geprueft: str | None = None

    @property
    def hat_update(self) -> bool:
        return self.code in ("update", "neu")


UNGEPRUEFT = ImageStatus("offen", "noch nicht geprüft")


class ImageUpdateChecker:
    def __init__(self, store, service, path: Path) -> None:
        self.store = store
        self.service = service
        self.path = path
        self._lock = threading.Lock()
        self._laeuft = threading.Lock()
        daten = read_json(path, {})
        self._refs: dict[str, dict] = daten.get("refs", {}) if isinstance(daten, dict) else {}
        self.letzte_pruefung: str | None = daten.get("letzte_pruefung") if isinstance(daten, dict) else None
        self._lokal_aufgefrischt: dict[str, float] = {}
        self._stop = threading.Event()

    # ---------------------------------------------------------------- Abfrage
    @property
    def laeuft(self) -> bool:
        return self._laeuft.locked()

    def status(self, ref: str, container_image_id: str) -> ImageStatus:
        if not ref or ref.startswith("sha256:"):
            return ImageStatus("lokal", "ohne Tag – nicht prüfbar")
        if "@" in ref:
            return ImageStatus("fest", "per Digest festgelegt")
        with self._lock:
            eintrag = self._refs.get(ref)
        if eintrag is None:
            return UNGEPRUEFT
        if eintrag.get("lokal_id") and container_image_id != eintrag["lokal_id"]:
            # Der Tag zeigt evtl. schon auf ein neueres Image (z. B. nach „docker compose pull“).
            # Lokalen Stand höchstens einmal pro Minute nachladen.
            if time.monotonic() - self._lokal_aufgefrischt.get(ref, 0) > 60:
                self._lokal_aufgefrischt[ref] = time.monotonic()
                self._refresh_local(ref)
                with self._lock:
                    eintrag = self._refs.get(ref, eintrag)
        geprueft = eintrag.get("geprueft")
        if eintrag.get("lokal_id") and container_image_id != eintrag["lokal_id"]:
            return ImageStatus("neu", "neues Image geladen – Neustart nötig", geprueft)
        if eintrag.get("fehler"):
            return ImageStatus("fehler", eintrag["fehler"], geprueft)
        if not eintrag.get("lokal_digests"):
            return ImageStatus("lokal", "lokal gebaut – nicht prüfbar", geprueft)
        if eintrag.get("remote") in eintrag["lokal_digests"]:
            return ImageStatus("aktuell", "aktuell", geprueft)
        return ImageStatus("update", "Update verfügbar", geprueft)

    # ----------------------------------------------------------------- Prüfen
    def _local(self, ref: str) -> tuple[str | None, list[str]]:
        image = self.service.image_attrs(ref)
        if image is None:
            return None, []
        repo = normalize_repo(ref)
        digests = [d.split("@", 1)[1] for d in image.get("RepoDigests") or []
                   if "@" in d and normalize_repo(d.split("@", 1)[0]) == repo]
        return image.get("Id"), digests

    def _refresh_local(self, ref: str) -> None:
        try:
            lokal_id, digests = self._local(ref)
        except Exception as exc:  # noqa: BLE001
            log.debug("Lokaler Stand von %s nicht lesbar: %s", ref, exc)
            return
        with self._lock:
            eintrag = dict(self._refs.get(ref, {}))
            eintrag.update(lokal_id=lokal_id, lokal_digests=digests)
            self._refs[ref] = eintrag
            self._save()

    def refresh_locals(self) -> None:
        """Nur den lokalen Stand aller bekannten Images neu lesen (ohne Registry).

        So fällt schnell auf, wenn jemand ein neues Image von Hand geladen hat
        („docker compose pull“), der Container aber noch mit dem alten läuft.
        """
        with self._lock:
            refs = list(self._refs)
        for ref in refs:
            self._refresh_local(ref)

    def check_ref(self, ref: str) -> None:
        eintrag: dict = {"geprueft": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            eintrag["lokal_id"], eintrag["lokal_digests"] = self._local(ref)
            if eintrag["lokal_digests"]:
                eintrag["remote"] = remote_digest(ref)
        except RegistryError as exc:
            eintrag["fehler"] = str(exc)
        except Exception as exc:  # noqa: BLE001 – eine kaputte Referenz darf den Lauf nicht beenden
            log.warning("Update-Prüfung für %s fehlgeschlagen: %s", ref, exc)
            eintrag["fehler"] = "Prüfung fehlgeschlagen"
        with self._lock:
            self._refs[ref] = eintrag
            self._save()

    def check_all(self) -> int:
        if not self._laeuft.acquire(blocking=False):
            return 0
        try:
            refs = sorted({c.image_ref for c in self.service.list_containers(with_stats=False)
                           if c.image_ref and "@" not in c.image_ref and not c.image_ref.startswith("sha256:")})
            for ref in refs:
                self.check_ref(ref)
            with self._lock:
                # Nicht mehr genutzte Images vergessen.
                self._refs = {r: e for r, e in self._refs.items() if r in refs}
                self.letzte_pruefung = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self._save()
            log.info("Update-Prüfung: %d Images geprüft", len(refs))
            return len(refs)
        except Exception as exc:  # noqa: BLE001
            log.warning("Update-Prüfung abgebrochen: %s", exc)
            return 0
        finally:
            self._laeuft.release()

    def check_in_background(self, ref: str | None = None) -> None:
        ziel = (lambda: self.check_ref(ref)) if ref else self.check_all
        threading.Thread(target=ziel, name="update-check", daemon=True).start()

    def _save(self) -> None:
        try:
            write_json(self.path, {"letzte_pruefung": self.letzte_pruefung, "refs": self._refs})
        except OSError as exc:
            log.error("Update-Stand nicht speicherbar: %s", exc)

    # ------------------------------------------------------------ Zeitplan
    def faellig(self, jetzt: datetime | None = None) -> bool:
        s = self.store.s
        if not s.update_check:
            return False
        letzte = fmt.parse_docker_time(self.letzte_pruefung)
        if letzte is None:
            return True
        jetzt = jetzt or datetime.now(timezone.utc)
        return jetzt - letzte >= timedelta(hours=s.update_check_hours)

    def start(self) -> None:
        def schleife() -> None:
            # Kurz warten, damit der Start des Dashboards nicht mit der Prüfung konkurriert.
            if self._stop.wait(30):
                return
            runde = 0
            while not self._stop.is_set():
                if self.faellig():
                    self.check_all()
                elif runde % 5 == 0 and not self.laeuft:
                    self.refresh_locals()
                runde += 1
                self._stop.wait(60)

        threading.Thread(target=schleife, name="update-zeitplan", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
