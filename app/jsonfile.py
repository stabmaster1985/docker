"""JSON-Dateien im Datenverzeichnis lesen und atomar schreiben."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("dashboard")


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.error("%s nicht lesbar: %s", path, exc)
        return default


def write_json(path: Path, data: Any, mode: int = 0o644) -> None:
    """Atomar schreiben, damit ein Absturz die Datei nie halb zurücklässt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
