"""Aktionslog als JSON-Datei (wer hat wann was gestartet/gestoppt)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .jsonfile import read_json, write_json


class ActionLog:
    def __init__(self, path: Path, max_entries: Callable[[], int]) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def _read(self) -> list[dict]:
        daten = read_json(self.path, [])
        return daten if isinstance(daten, list) else []

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
            write_json(self.path, daten[-self.max_entries():])
        return eintrag

    def clear(self) -> None:
        with self._lock:
            write_json(self.path, [])
