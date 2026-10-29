"""Aktionslog als JSON-Datei (wer hat wann was gestartet/gestoppt)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("dashboard")


class ActionLog:
    def __init__(self, path: Path, max_entries: int = 1000) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        try:
            with self.path.open(encoding="utf-8") as f:
                daten = json.load(f)
            return daten if isinstance(daten, list) else []
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Aktionslog %s nicht lesbar: %s", self.path, exc)
            return []

    def entries(self, limit: int | None = None) -> list[dict]:
        """Neueste Einträge zuerst."""
        with self._lock:
            daten = self._read()
        daten.reverse()
        return daten[:limit] if limit else daten

    def add(self, user: str, action: str, container: str, ok: bool, message: str) -> dict:
        eintrag = {
            "zeit": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "benutzer": user,
            "aktion": action,
            "container": container,
            "ok": ok,
            "meldung": message,
        }
        with self._lock:
            daten = self._read()
            daten.append(eintrag)
            daten = daten[-self.max_entries :]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomar schreiben, damit ein Absturz die Datei nie halb zurücklässt.
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".aktionen-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(daten, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return eintrag
