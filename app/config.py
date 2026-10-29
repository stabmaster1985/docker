"""Einstellungen aus Umgebungsvariablen (.env via docker compose)."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

log = logging.getLogger("dashboard")


def _bool(name: str, default: bool) -> bool:
    wert = os.environ.get(name)
    if wert is None or wert.strip() == "":
        return default
    return wert.strip().lower() in {"1", "true", "yes", "ja", "on"}


def _int(name: str, default: int) -> int:
    wert = os.environ.get(name, "").strip()
    return int(wert) if wert else default


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    session_secret: str
    session_max_age: int
    cookie_secure: bool
    docker_host: str
    docker_api_version: str
    docker_timeout: int
    data_dir: str
    action_log_max: int
    refresh_seconds: int
    log_tail: int
    restart_loop_threshold: int
    restart_loop_window: int
    ignore_exit_zero: bool
    login_max_attempts: int
    login_lock_seconds: int
    stats_workers: int


def load_settings() -> Settings:
    username = os.environ.get("DASHBOARD_USER", "").strip()
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "DASHBOARD_USER und DASHBOARD_PASSWORD müssen gesetzt sein (siehe .env.example)."
        )
    if password == "bitte-aendern":
        raise RuntimeError("Bitte DASHBOARD_PASSWORD in der .env ändern.")

    secret = os.environ.get("SESSION_SECRET", "").strip()
    if not secret:
        # Funktioniert, aber jede Anmeldung verfällt beim Neustart des Containers.
        log.warning("SESSION_SECRET nicht gesetzt – es wird ein zufälliger Schlüssel verwendet.")
        secret = secrets.token_urlsafe(48)

    return Settings(
        username=username,
        password=password,
        session_secret=secret,
        session_max_age=_int("SESSION_MAX_AGE", 12 * 3600),
        cookie_secure=_bool("COOKIE_SECURE", False),
        docker_host=os.environ.get("DOCKER_HOST", "tcp://socket-proxy:2375"),
        docker_api_version=os.environ.get("DOCKER_API_VERSION", "1.44"),
        docker_timeout=_int("DOCKER_TIMEOUT", 30),
        data_dir=os.environ.get("DATA_DIR", "/data"),
        action_log_max=_int("ACTION_LOG_MAX", 1000),
        refresh_seconds=_int("REFRESH_SECONDS", 15),
        log_tail=_int("LOG_TAIL", 200),
        restart_loop_threshold=_int("RESTART_LOOP_THRESHOLD", 3),
        restart_loop_window=_int("RESTART_LOOP_WINDOW", 600),
        ignore_exit_zero=_bool("IGNORE_EXIT_ZERO", False),
        login_max_attempts=_int("LOGIN_MAX_ATTEMPTS", 5),
        login_lock_seconds=_int("LOGIN_LOCK_SECONDS", 300),
        stats_workers=_int("STATS_WORKERS", 8),
    )
